# connectors/salesforce/connector.py
# Salesforce Field Service → fma_workorder ingestion connector

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("salesforce_connector")

# Make the sibling `shared` package importable no matter where this is run from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# Writes to the target Dataverse are handled by the shared utility so every
# connector uses one tested upsert path (see connectors/shared/dataverse_upsert.py).
from shared.dataverse_upsert import (  # noqa: E402
    get_dataverse_token as _shared_get_dataverse_token,
    upsert_record,
)

# `requests` (used only for the Salesforce REST API here) is imported lazily so
# the transform / SOQL / sync-state unit tests run without the HTTP stack.
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def _require_requests():
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required for network operations. "
            "Install dependencies with: pip install -r requirements.txt"
        )


# ---------------------------------------------------------------------------
# Transform helpers
# ---------------------------------------------------------------------------
def _to_str(value):
    """Coerce to string, leaving None as None (avoids literal 'None' strings)."""
    if value is None:
        return None
    return str(value)


def _to_utc(value):
    """
    Normalize a datetime (or ISO-8601 string) to a UTC ISO-8601 string.
    Salesforce emits timestamps like '2026-01-15T10:00:00.000+0000'; both
    'Z' and '+HHMM'/'+HH:MM' offsets are handled. Naive values are assumed UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Salesforce sometimes returns a '+0000' offset with no colon; fromisoformat
        # on 3.9 wants '+00:00', so normalize a trailing 4-digit offset.
        elif len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _to_currency(value):
    """Coerce to float (Dataverse currency); None passes through."""
    if value is None:
        return None
    return float(value)


def _map_salesforce_status(value, status_map: dict):
    """Translate a Salesforce Status string to our normalized fma_status."""
    if value is None:
        return None
    return status_map.get(str(value))


def infer_first_time_fix(record: dict) -> bool:
    """
    Salesforce does not have a native FirstTimeFix boolean, so we infer it.
    Logic:
      - If the work order is not closed (IsClosed missing/False), it cannot yet
        be a first-time fix → False.
      - If it IS closed and we have repeat-visit data (a visit count), it's a
        first-time fix only when there was a single visit.
      - If it IS closed and we have no repeat data available, we treat it as a
        first-time fix (True) as the best available inference.
    This heuristic will be refined as we observe real service-appointment data.
    """
    if not record.get("IsClosed"):
        return False
    visit_count = record.get("VisitCount")
    if visit_count is None:
        visit_count = record.get("fma_visitcount")
    if visit_count is not None:
        try:
            return int(visit_count) <= 1
        except (TypeError, ValueError):
            return True
    # Closed, but no repeat-visit signal available.
    return True


def _apply_transform(name: str, value, field_map: dict, raw: Optional[dict] = None):
    """Dispatch a single field value (or the whole record) through a transform."""
    if name == "none":
        return value
    if name == "toString":
        return _to_str(value)
    if name == "toUTC":
        return _to_utc(value)
    if name == "toCurrency":
        return _to_currency(value)
    if name == "mapSalesforceStatus":
        return _map_salesforce_status(value, field_map.get("statusMap", {}))
    if name == "inferFirstTimeFix":
        # Needs the full record, not just the single source field.
        return infer_first_time_fix(raw if raw is not None else {})
    raise ValueError(f"Unknown transform: {name}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_access_token(
    login_url: str, client_id: str, client_secret: str, username: str, password: str
) -> tuple[str, str]:
    """
    Authenticates to Salesforce using the OAuth 2.0 Username-Password flow.
    Returns a tuple of (access_token, instance_url). The instance_url is the
    per-org base URL for all subsequent API calls and must never be hardcoded —
    Salesforce returns the correct one at login. Called once per sync run.
    """
    _require_requests()
    token_url = f"{login_url.rstrip('/')}/services/oauth2/token"
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,  # service-account password + security token
    }
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return body["access_token"], body["instance_url"]


def get_dataverse_token(tenant_id: str, client_id: str, client_secret: str, resource: str) -> str:
    """
    Gets a Dataverse-specific OAuth token for writing to the target org. This is
    a separate auth context from Salesforce — delegates to the shared utility so
    all connectors obtain their write token the same way.
    """
    return _shared_get_dataverse_token(tenant_id, client_id, client_secret, resource)


# ---------------------------------------------------------------------------
# Fetch from Salesforce
# ---------------------------------------------------------------------------
def _format_soql_datetime(dt: datetime) -> str:
    """Salesforce SOQL datetime literal format: 2026-01-15T00:00:00Z (UTC, no quotes)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_soql_query(soql_base: str, last_sync: Optional[datetime], page_size: int) -> str:
    """
    Builds a SOQL query string. When last_sync is provided, adds
    `WHERE LastModifiedDate > {ts}` for incremental sync (ts in Salesforce's
    ISO-8601 'Z' format). Always appends `ORDER BY LastModifiedDate ASC` and
    `LIMIT {page_size}`. When last_sync is None, no WHERE clause is added
    (full initial sync).
    """
    query = soql_base.strip()
    if last_sync is not None:
        query += f" WHERE LastModifiedDate > {_format_soql_datetime(last_sync)}"
    query += " ORDER BY LastModifiedDate ASC"
    query += f" LIMIT {page_size}"
    return query


def fetch_workorders(access_token: str, instance_url: str, soql: str, api_version: str) -> list[dict]:
    """
    Executes a SOQL query against the Salesforce REST API at
    {instance_url}/services/data/{api_version}/query?q={soql} and follows
    `nextRecordsUrl` until all pages are retrieved. Returns a flat list of raw
    Salesforce WorkOrder record dicts (the per-record `attributes` envelope is
    left intact).
    """
    _require_requests()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    base = instance_url.rstrip("/")
    url = f"{base}/services/data/{api_version}/query"

    resp = requests.get(url, headers=headers, params={"q": soql}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    records: list[dict] = list(payload.get("records", []))
    next_records_url = payload.get("nextRecordsUrl")
    while next_records_url:
        # nextRecordsUrl is a path relative to the instance host.
        resp = requests.get(f"{base}{next_records_url}", headers=headers, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        records.extend(payload.get("records", []))
        next_records_url = payload.get("nextRecordsUrl")

    return records


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def transform_workorder(raw: dict, field_map: dict, client_id: str) -> dict:
    """
    Applies field-map.json mappings to convert a raw Salesforce WorkOrder into a
    normalized fma_workorder record. Handles every transform type (toString,
    none, toUTC, toCurrency, mapSalesforceStatus, inferFirstTimeFix). Missing
    optional source fields default to None (no KeyError). Also:
      - stamps fma_sourcesystem from the static Salesforce value (100000001),
      - stamps fma_syncedon with the current UTC timestamp,
      - binds fma_client to the provided client_id.
    Returns a dict ready to PATCH to the target Dataverse Web API.
    """
    record: dict = {}

    for mapping in field_map.get("mappings", []):
        source = mapping["source"]
        target = mapping["target"]
        transform = mapping.get("transform", "none")
        raw_value = raw.get(source)  # missing → None, never KeyError
        record[target] = _apply_transform(transform, raw_value, field_map, raw=raw)

    for key, value in field_map.get("staticValues", {}).items():
        record[key] = value

    record["fma_syncedon"] = datetime.now(timezone.utc).isoformat()
    record["fma_client"] = client_id
    record["fma_client@odata.bind"] = f"/fma_clients({client_id})"

    return record


# ---------------------------------------------------------------------------
# Write to target Dataverse
# ---------------------------------------------------------------------------
def upsert_workorder(record: dict, target_url: str, access_token: str) -> dict:
    """
    Upserts a normalized fma_workorder record into the target Dataverse org via
    the shared alternate-key PATCH utility (keyed on fma_externalsourceid). The
    access_token here is the Dataverse token, not the Salesforce one. Returns
    the shared utility's response dict.
    """
    # Strip the helper-only fma_client key; the real binding is fma_client@odata.bind.
    payload = {k: v for k, v in record.items() if k != "fma_client"}
    return upsert_record(
        payload,
        "fma_workorders",
        "fma_externalsourceid",
        record.get("fma_externalsourceid"),
        target_url,
        access_token,
    )


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------
def get_last_sync_time(state_file: str = "connectors/salesforce/.sync_state.json") -> Optional[datetime]:
    """
    Reads the last successful sync timestamp from a local state file. Returns
    None if the file does not exist (which triggers a full initial sync).
    """
    if not os.path.exists(state_file):
        return None
    with open(state_file) as f:
        state = json.load(f)
    raw = state.get("last_sync")
    if not raw:
        return None
    s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    return datetime.fromisoformat(s)


def save_sync_time(sync_time: datetime, state_file: str = "connectors/salesforce/.sync_state.json"):
    """
    Writes the current sync timestamp (ISO-8601) after a successful run so the
    next incremental run knows where to resume from.
    """
    if os.path.dirname(state_file):
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump({"last_sync": sync_time.isoformat()}, f)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def run_sync(config: dict, client_id: str):
    """
    Main entry point. Orchestrates the full sync cycle:
      1. Get Salesforce access token + instance URL.
      2. Get a separate Dataverse access token.
      3. Load the field map.
      4. Read the last sync time (or fall back to initialSyncDaysBack).
      5. Build the SOQL query with the incremental date filter.
      6. Fetch changed work orders from Salesforce.
      7. Transform each raw record.
      8. Upsert each record to the target Dataverse.
      9. Save the new sync timestamp.
    Logs counts: fetched, transformed, upserted, errors.
    """
    auth = config["auth"]
    sync_cfg = config["sync"]
    target = config["target"]

    # 1. Salesforce token + instance URL.
    sf_token, instance_url = get_access_token(
        auth["loginUrl"], auth["clientId"], auth["clientSecret"],
        auth["username"], auth["password"],
    )

    # 2. Dataverse write token (separate auth context, creds from env).
    dv_token = get_dataverse_token(
        os.environ["FMA_DATAVERSE_TENANT_ID"],
        os.environ["FMA_DATAVERSE_CLIENT_ID"],
        os.environ["FMA_DATAVERSE_CLIENT_SECRET"],
        target["dataverseUrl"],
    )

    # 3. Field map (co-located with this module).
    field_map_path = os.path.join(os.path.dirname(__file__), "field-map.json")
    with open(field_map_path) as f:
        field_map = json.load(f)

    # 4. Incremental window.
    last_sync = get_last_sync_time()
    if last_sync is None:
        days_back = sync_cfg.get("initialSyncDaysBack", 90)
        last_sync = datetime.now(timezone.utc) - timedelta(days=days_back)
        logger.info("No sync state found — initial sync going back %s days.", days_back)
    else:
        logger.info("Incremental sync since %s.", last_sync.isoformat())

    run_started = datetime.now(timezone.utc)

    # 5. SOQL.
    soql = build_soql_query(sync_cfg["soqlBase"], last_sync, sync_cfg.get("pageSize", 200))

    # 6. Fetch.
    raw_records = fetch_workorders(sf_token, instance_url, soql, sync_cfg["apiVersion"])
    logger.info("Fetched %d work orders from Salesforce.", len(raw_records))

    # 7 + 8. Transform and upsert.
    transformed = 0
    upserted = 0
    errors = 0
    for raw in raw_records:
        try:
            record = transform_workorder(raw, field_map, client_id)
            transformed += 1
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("Transform failed for record %s", raw.get("Id"))
            continue
        try:
            upsert_workorder(record, target["dataverseUrl"], dv_token)
            upserted += 1
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("Upsert failed for %s", record.get("fma_externalsourceid"))

    # 9. Persist watermark only after the run completes.
    save_sync_time(run_started)

    logger.info(
        "Sync complete. fetched=%d transformed=%d upserted=%d errors=%d",
        len(raw_records), transformed, upserted, errors,
    )
    return {
        "fetched": len(raw_records),
        "transformed": transformed,
        "upserted": upserted,
        "errors": errors,
    }


if __name__ == "__main__":
    with open("connectors/salesforce/config.json") as f:
        config = json.load(f)
    run_sync(config, client_id=os.environ["FMA_CLIENT_ID"])
