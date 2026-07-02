# connectors/d365/connector.py
# D365 Field Service → fma_workorder ingestion connector

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("d365_connector")

# `requests` is imported lazily so the pure-Python transform and sync-state
# logic (and their unit tests) can run in environments where the HTTP stack
# is not installed. Only the functions that actually make network calls need it.
try:
    import requests
except ImportError:  # pragma: no cover - exercised only when requests is absent
    requests = None


def _require_requests():
    """Guard used by network functions: fail loudly if `requests` is missing."""
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required for network operations. "
            "Install dependencies with: pip install -r requirements.txt"
        )


# ---------------------------------------------------------------------------
# Transform helpers
# ---------------------------------------------------------------------------
def _to_str(value):
    """Coerce a value to string, but leave None as None (no 'None' strings)."""
    if value is None:
        return None
    return str(value)


def _to_utc(value):
    """
    Normalize a datetime (or ISO-8601 string) to a UTC ISO-8601 string.
    Naive datetimes are assumed to already be UTC. None passes through as None.
    Uses datetime.fromisoformat so no third-party parser is needed at transform time.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        # D365 emits a trailing 'Z' for UTC; fromisoformat wants an explicit offset.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _to_currency(value):
    """Coerce a value to float (Dataverse currency). None passes through as None."""
    if value is None:
        return None
    return float(value)


def _map_d365_status(value, status_map: dict):
    """
    Translate a raw D365 systemstatus option-set code into our normalized
    fma_status string using the statusMap from field-map.json.
    Unknown / missing codes return None rather than raising.
    """
    if value is None:
        return None
    return status_map.get(str(value))


def _apply_transform(name: str, value, field_map: dict):
    """Dispatch a single field value through the named transform."""
    if name == "none":
        return value
    if name == "toString":
        return _to_str(value)
    if name == "toUTC":
        return _to_utc(value)
    if name == "toCurrency":
        return _to_currency(value)
    if name == "mapD365Status":
        return _map_d365_status(value, field_map.get("statusMap", {}))
    raise ValueError(f"Unknown transform: {name}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_access_token(tenant_id: str, client_id: str, client_secret: str, resource: str) -> str:
    """
    Obtains an OAuth 2.0 access token using the client credentials flow.
    Uses the Microsoft identity platform token endpoint. Called once per sync
    run — the returned token is reused for all API calls in that run.
    """
    _require_requests()
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        # Dataverse/D365 expects the org URL as the scope with the .default suffix.
        "scope": f"{resource.rstrip('/')}/.default",
    }
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Fetch from D365
# ---------------------------------------------------------------------------
def _select_fields(field_map: dict) -> list[str]:
    """Return the list of D365 source fields we actually need (for $select)."""
    return [m["source"] for m in field_map.get("mappings", [])]


def fetch_workorders(
    access_token: str,
    endpoint: str,
    last_sync: Optional[datetime],
    page_size: int = 100,
    field_map: Optional[dict] = None,
) -> list[dict]:
    """
    Fetches work orders from D365 Field Service using an OData query.
    - Uses $select to pull only mapped fields (bandwidth efficiency).
    - Uses $filter on `modifiedon gt last_sync` for incremental sync; when
      last_sync is None the filter is omitted entirely (full initial sync).
    - Honors server-driven paging by following @odata.nextLink automatically.
    Returns a flat list of raw D365 work order dicts.
    """
    _require_requests()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        # Ask D365 to page at our configured size.
        "Prefer": f"odata.maxpagesize={page_size}",
    }

    params = {}
    if field_map:
        params["$select"] = ",".join(_select_fields(field_map))
    if last_sync is not None:
        # Incremental: only records changed since the last successful sync.
        params["$filter"] = f"modifiedon gt {_to_utc(last_sync)}"

    results: list[dict] = []
    # First page uses params; subsequent pages use the fully-formed nextLink URL.
    resp = requests.get(endpoint, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    results.extend(payload.get("value", []))

    next_link = payload.get("@odata.nextLink")
    while next_link:
        resp = requests.get(next_link, headers=headers, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        results.extend(payload.get("value", []))
        next_link = payload.get("@odata.nextLink")

    return results


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def transform_workorder(raw: dict, field_map: dict, client_id: str) -> dict:
    """
    Applies field-map.json mappings to convert a raw D365 work order into a
    normalized fma_workorder record. Handles every transform type
    (toString, none, toUTC, toCurrency, mapD365Status). Missing optional
    source fields default to None rather than raising KeyError. Also:
      - stamps fma_sourcesystem from the static D365 value,
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
        record[target] = _apply_transform(transform, raw_value, field_map)

    # Static values (e.g. fma_sourcesystem = D365 option value).
    for key, value in field_map.get("staticValues", {}).items():
        record[key] = value

    # Always stamp when this record was synced.
    record["fma_syncedon"] = datetime.now(timezone.utc).isoformat()

    # Bind the client lookup. The @odata.bind form is what the Web API expects;
    # the plain id is kept too for convenience/testing.
    record["fma_client"] = client_id
    record["fma_client@odata.bind"] = f"/fma_clients({client_id})"

    return record


# ---------------------------------------------------------------------------
# Write to target Dataverse
# ---------------------------------------------------------------------------
def upsert_workorder(record: dict, target_url: str, access_token: str) -> dict:
    """
    Upserts a normalized fma_workorder record into the target Dataverse org.
    Uses PATCH against the alternate key
    fma_workorders(fma_externalsourceid='...') so that re-running the connector
    updates the existing row instead of creating a duplicate. Returns a dict
    describing the API result.
    """
    _require_requests()
    external_id = record.get("fma_externalsourceid")

    # The plain-id fma_client helper key is not a real column; strip it before send.
    payload = {k: v for k, v in record.items() if k != "fma_client"}

    url = (
        f"{target_url.rstrip('/')}/api/data/v9.2/"
        f"fma_workorders(fma_externalsourceid='{external_id}')"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "If-Match": "*",  # allow update; combined with PATCH this is an upsert
    }
    resp = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=60)
    resp.raise_for_status()
    return {
        "status_code": resp.status_code,
        "external_id": external_id,
        # 201 = created, 204 = updated (no content)
        "created": resp.status_code == 201,
    }


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------
def get_last_sync_time(state_file: str = "connectors/d365/.sync_state.json") -> Optional[datetime]:
    """
    Reads the last successful sync timestamp from a local state file.
    Returns None if the file does not exist (which triggers a full initial sync).
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


def save_sync_time(sync_time: datetime, state_file: str = "connectors/d365/.sync_state.json"):
    """
    Writes the current sync timestamp to the state file (ISO-8601) after a
    successful run. This is how incremental sync knows where to resume from.
    """
    os.makedirs(os.path.dirname(state_file), exist_ok=True) if os.path.dirname(state_file) else None
    with open(state_file, "w") as f:
        json.dump({"last_sync": sync_time.isoformat()}, f)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def run_sync(config: dict, client_id: str):
    """
    Main entry point. Orchestrates the full sync cycle:
      1. Get an access token (client credentials).
      2. Load the field map.
      3. Read the last sync time (or fall back to initialSyncDaysBack).
      4. Fetch changed work orders from D365.
      5. Transform each raw record.
      6. Upsert each record into the target Dataverse.
      7. Save the new sync timestamp.
    Logs counts: fetched, transformed, upserted, errors.
    """
    auth = config["auth"]
    sync_cfg = config["sync"]
    target = config["target"]

    # 1. Token
    access_token = get_access_token(
        auth["tenantId"], auth["clientId"], auth["clientSecret"], auth["resource"]
    )

    # 2. Field map (co-located with this module).
    field_map_path = os.path.join(os.path.dirname(__file__), "field-map.json")
    with open(field_map_path) as f:
        field_map = json.load(f)

    # 3. Determine incremental window.
    last_sync = get_last_sync_time()
    if last_sync is None:
        days_back = sync_cfg.get("initialSyncDaysBack", 90)
        last_sync = datetime.now(timezone.utc) - timedelta(days=days_back)
        logger.info("No sync state found — initial sync going back %s days.", days_back)
    else:
        logger.info("Incremental sync since %s.", last_sync.isoformat())

    # Capture the run start BEFORE fetching so we never miss records modified
    # during the run on the next pass.
    run_started = datetime.now(timezone.utc)

    # 4. Fetch
    raw_records = fetch_workorders(
        access_token,
        sync_cfg["endpoint"],
        last_sync,
        page_size=sync_cfg.get("pageSize", 100),
        field_map=field_map,
    )
    logger.info("Fetched %d work orders from D365.", len(raw_records))

    # 5 + 6. Transform and upsert.
    transformed = 0
    upserted = 0
    errors = 0
    for raw in raw_records:
        try:
            record = transform_workorder(raw, field_map, client_id)
            transformed += 1
        except Exception:  # noqa: BLE001 - log and continue on bad rows
            errors += 1
            logger.exception("Transform failed for record %s", raw.get("msdyn_workorderid"))
            continue
        try:
            upsert_workorder(record, target["dataverseUrl"], access_token)
            upserted += 1
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("Upsert failed for %s", record.get("fma_externalsourceid"))

    # 7. Persist new watermark only after the run completes.
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
    with open("connectors/d365/config.json") as f:
        config = json.load(f)
    run_sync(config, client_id=os.environ["FMA_CLIENT_ID"])
