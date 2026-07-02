# connectors/ifs/connector.py
# IFS Field Service → fma_workorder ingestion connector
# IFS exposes OData v4 — similar query pattern to D365 but different
# auth (IFS Identity Server) and different entity structure (WorkTask)

import os
import sys
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ifs_connector")

# Make the sibling `shared` package importable and route all Dataverse writes
# through the single shared utility (see connectors/shared/dataverse_upsert.py).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared.dataverse_upsert import (  # noqa: E402
    get_dataverse_token,
    upsert_record,
)

# `requests` (used only for the IFS OData API here) is imported lazily so the
# transform / query-builder / sync-state unit tests run without the HTTP stack.
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# Default look-back window (days) for the initial SnapShotCreatedDate load when
# no RowVersion watermark exists yet. Mirrors config.sync.initialSyncDaysBack.
INITIAL_SYNC_DAYS_BACK = 90


def _require_requests():
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required for network operations. "
            "Install dependencies with: pip install -r requirements.txt"
        )


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------
def _parse_dt(value) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string (or datetime) to aware UTC; None-safe."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s == "":
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_odata_dt(dt: datetime) -> str:
    """OData v4 Edm.DateTimeOffset literal in UTC: 2026-01-15T00:00:00Z (unquoted)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_access_token(token_url: str, client_id: str, client_secret: str, scope: str) -> str:
    """
    Authenticates to IFS Cloud using the OAuth 2.0 Client Credentials flow
    against the IFS Identity Server (NOT the Microsoft identity platform).
    Token endpoint: {instance}.ifs.cloud/idp/connect/token. Scope is typically
    'ifsapp offline_access'. Returns the access token string.
    """
    _require_requests()
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_basic_auth_header(username: str, password: str) -> dict:
    """
    Constructs a Basic Auth header for on-premise IFS deployments. Returns a dict
    with the Authorization header ready to merge into request headers. Used when
    deploymentType is 'onpremise' in config.
    """
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def get_auth_headers(config: dict) -> dict:
    """
    Router — returns the correct auth headers based on deploymentType:
      'cloud'     → OAuth client credentials via get_access_token (Bearer token),
      'onpremise' → Basic Auth via get_basic_auth_header.
    This abstraction means the rest of the connector never needs to know which
    deployment type it is talking to.
    """
    deployment_type = config.get("deploymentType", "cloud")
    auth = config["auth"]

    if deployment_type == "onpremise":
        basic = auth.get("basicAuth", {})
        return get_basic_auth_header(basic.get("username"), basic.get("password"))

    token = get_access_token(
        auth["tokenUrl"], auth["clientId"], auth["clientSecret"], auth.get("scope", "")
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fetch from IFS
# ---------------------------------------------------------------------------
def build_odata_query(
    base_url: str,
    entity: str,
    select_fields: str,
    last_sync: Optional[datetime],
    page_size: int,
    skip: int = 0,
) -> str:
    """
    Builds an IFS OData v4 query URL:
      {base_url}/{entity}?$select=...&$filter=...&$top=...&$skip=...&$orderby=RowVersion asc

    - Incremental (last_sync provided): $filter=RowVersion gt {watermark}. RowVersion
      is IFS's per-row change stamp — we persist the last successful watermark and
      only pull rows changed since.
    - Initial load (last_sync is None): $filter=SnapShotCreatedDate gt {now - N days},
      because no RowVersion watermark exists yet.
    - Always $orderby=RowVersion asc so pages are processed in change order.
    - IFS OData v4 uses $skip for offset pagination (not $skiptoken).
    """
    url = f"{base_url.rstrip('/')}/{entity}"
    parts = [f"$select={select_fields}"]

    if last_sync is not None:
        parts.append(f"$filter=RowVersion gt {_format_odata_dt(last_sync)}")
    else:
        threshold = datetime.now(timezone.utc) - timedelta(days=INITIAL_SYNC_DAYS_BACK)
        parts.append(f"$filter=SnapShotCreatedDate gt {_format_odata_dt(threshold)}")

    parts.append(f"$top={page_size}")
    parts.append(f"$skip={skip}")
    parts.append("$orderby=RowVersion asc")
    return url + "?" + "&".join(parts)


def fetch_workorders(
    auth_headers: dict,
    base_url: str,
    entity: str,
    select_fields: str,
    last_sync: Optional[datetime],
    page_size: int = 100,
) -> list[dict]:
    """
    Fetches WorkTask records from the IFS OData v4 API with $skip-based
    pagination — skip advances by page_size each page. IFS returns records in the
    'value' array of the JSON response; paging stops once a page returns fewer
    than page_size records. Returns a flat list of raw IFS WorkTask dicts.
    """
    _require_requests()
    headers = {"Accept": "application/json", **auth_headers}

    results: list[dict] = []
    skip = 0
    while True:
        url = build_odata_query(base_url, entity, select_fields, last_sync, page_size, skip)
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        batch = resp.json().get("value", [])
        results.extend(batch)
        if len(batch) < page_size:
            break  # last page reached
        skip += page_size
    return results


# ---------------------------------------------------------------------------
# Transform helpers (public — covered by unit tests)
# ---------------------------------------------------------------------------
def calculate_labor_minutes(
    real_time_sta: Optional[str], real_time_finish: Optional[str]
) -> Optional[int]:
    """
    IFS WorkTask has no direct labor-minutes field, so we derive it from the
    RealTimeSta → RealTimeFinish delta (both ISO-8601 datetime strings). Returns
    total whole minutes as an integer, or None if either value is missing or
    unparseable.
    """
    start = _parse_dt(real_time_sta)
    finish = _parse_dt(real_time_finish)
    if start is None or finish is None:
        return None
    return int((finish - start).total_seconds() // 60)


def infer_ifs_sla_compliance(record: dict) -> bool:
    """
    Infers SLA compliance for an IFS work order. Simple inference: if
    RealTimeFinish <= FinishDate (scheduled end) then the SLA was met.
      - RealTimeFinish missing → False (cannot confirm it was met),
      - FinishDate missing     → True (no SLA target defined = not measured),
      - otherwise RealTimeFinish <= FinishDate.
    """
    real_finish = _parse_dt(record.get("RealTimeFinish"))
    if real_finish is None:
        return False
    scheduled_finish = _parse_dt(record.get("FinishDate"))
    if scheduled_finish is None:
        return True
    return real_finish <= scheduled_finish


def map_ifs_state(state_str: str, state_map: dict) -> str:
    """
    Maps an IFS ObjState string (Released, Started, WorkDone, Reported, Finished,
    Cancelled, Rejected) to an fma status choice. Returns 'Open' as the default
    when the state is not found in the map.
    """
    return state_map.get(str(state_str), "Open")


def _to_str(value):
    if value is None:
        return None
    return str(value)


def _to_utc(value):
    dt = _parse_dt(value)
    return dt.isoformat() if dt is not None else None


def _to_currency(value):
    if value is None:
        return None
    return float(value)


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
    if name == "mapIFSState":
        return map_ifs_state(value, field_map.get("stateMap", {}))
    if name == "inferIFSSlaCCompliance":
        return infer_ifs_sla_compliance(raw if raw is not None else {})
    raise ValueError(f"Unknown transform: {name}")


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def _composite_external_id(raw: dict) -> Optional[str]:
    """
    Build the unique external source id from WoNo + TaskSeq. A single work order
    (WoNo) can have many tasks (TaskSeq 1, 2, 3...); WoNo alone would collide on
    upsert, so we concatenate as '{WoNo}_{TaskSeq}'.
    """
    wono = raw.get("WoNo")
    if wono is None:
        return None
    task_seq = raw.get("TaskSeq")
    if task_seq in (None, ""):
        return str(wono)
    return f"{wono}_{task_seq}"


def transform_workorder(raw: dict, field_map: dict, client_id: str) -> dict:
    """
    Applies field-map.json mappings to convert a raw IFS WorkTask into a
    normalized fma_workorder record, plus IFS-specific handling:
      - fma_laborminutes is CALCULATED from the RealTimeSta/RealTimeFinish delta
        (not a direct field mapping), applied after the field-map loop,
      - fma_externalsourceid is built from WoNo + TaskSeq for uniqueness,
      - fma_firsttimefix is inferred (ObjState Reported/Finished on TaskSeq 1).
    Also stamps fma_sourcesystem (100000003), fma_syncedon (UTC now), and binds
    fma_client to the provided client_id. Missing optional fields default to None.
    """
    record: dict = {}

    for mapping in field_map.get("mappings", []):
        source = mapping["source"]
        target = mapping["target"]
        transform = mapping.get("transform", "none")
        raw_value = raw.get(source)  # missing → None, never KeyError
        record[target] = _apply_transform(transform, raw_value, field_map, raw=raw)

    # Labor minutes: calculated, not mapped.
    record["fma_laborminutes"] = calculate_labor_minutes(
        raw.get("RealTimeSta"), raw.get("RealTimeFinish")
    )

    # Composite external source id (overrides the plain WoNo set by the field map).
    record["fma_externalsourceid"] = _composite_external_id(raw)

    # First-time fix: completed on the first task of the work order.
    obj_state = raw.get("ObjState")
    task_seq = raw.get("TaskSeq")
    record["fma_firsttimefix"] = (
        str(obj_state) in ("Reported", "Finished") and str(task_seq) == "1"
    )

    for key, value in field_map.get("staticValues", {}).items():
        record[key] = value

    record["fma_syncedon"] = datetime.now(timezone.utc).isoformat()
    record["fma_client"] = client_id
    record["fma_client@odata.bind"] = f"/fma_clients({client_id})"

    return record


# ---------------------------------------------------------------------------
# Write to Dataverse (uses shared utility)
# ---------------------------------------------------------------------------
def upsert_workorder(record: dict, target_url: str, access_token: str) -> dict:
    """
    Upserts a normalized fma_workorder record into the target Dataverse org via
    the shared alternate-key PATCH utility, keyed on fma_externalsourceid. The
    access_token here is the Dataverse token, not the IFS one.
    """
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
def get_last_sync_time(state_file: str = "connectors/ifs/.sync_state.json") -> Optional[datetime]:
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


def save_sync_time(sync_time: datetime, state_file: str = "connectors/ifs/.sync_state.json"):
    """Writes the current sync timestamp (ISO-8601) after a successful run."""
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
      1. Get auth headers (cloud OAuth or on-premise BasicAuth) via the router.
      2. Get a Dataverse access token via the shared utility.
      3. Load the field map.
      4. Read the last sync time (None on first run → initial SnapShotCreatedDate load).
      5. Fetch changed WorkTask records from the IFS OData API.
      6. Transform each record (including labor-minutes calculation).
      7. Upsert each record to the target Dataverse.
      8. Save the new sync timestamp.
    Logs counts: fetched, transformed, upserted, errors.
    """
    sync_cfg = config["sync"]
    target = config["target"]

    # 1. IFS auth headers (deployment-agnostic).
    auth_headers = get_auth_headers(config)

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

    # 4. Incremental watermark (None → build_odata_query does the initial load).
    last_sync = get_last_sync_time()
    if last_sync is None:
        logger.info("No sync state found — initial SnapShotCreatedDate load.")
    else:
        logger.info("Incremental sync since %s.", last_sync.isoformat())

    run_started = datetime.now(timezone.utc)

    # 5. Fetch.
    raw_records = fetch_workorders(
        auth_headers, sync_cfg["baseUrl"], sync_cfg["entity"], sync_cfg["select"],
        last_sync, page_size=sync_cfg.get("pageSize", 100),
    )
    logger.info("Fetched %d WorkTask records from IFS.", len(raw_records))

    # 6 + 7. Transform and upsert.
    transformed = 0
    upserted = 0
    errors = 0
    for raw in raw_records:
        try:
            record = transform_workorder(raw, field_map, client_id)
            transformed += 1
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("Transform failed for WoNo %s", raw.get("WoNo"))
            continue
        try:
            upsert_workorder(record, target["dataverseUrl"], dv_token)
            upserted += 1
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("Upsert failed for %s", record.get("fma_externalsourceid"))

    # 8. Persist watermark only after the run completes.
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
    with open("connectors/ifs/config.json") as f:
        config = json.load(f)
    run_sync(config, client_id=os.environ["FMA_CLIENT_ID"])
