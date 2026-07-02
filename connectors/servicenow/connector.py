# connectors/servicenow/connector.py
# ServiceNow FSM → fma_workorder ingestion connector

import os
import sys
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("servicenow_connector")

# Make the sibling `shared` package importable and route all Dataverse writes
# through the single shared utility (see connectors/shared/dataverse_upsert.py).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared.dataverse_upsert import (  # noqa: E402
    get_dataverse_token,
    upsert_record,
)

# `requests` (used only for the ServiceNow Table API here) is imported lazily so
# the transform / query-builder / sync-state unit tests run without the HTTP stack.
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
# Field-value helpers
#
# With sysparm_display_value=all, EVERY field in a ServiceNow result is returned
# as an object like {"value": "...", "display_value": "...", "link": "..."}.
# These helpers pull the right piece out whether the field arrived as such an
# object or as a plain scalar (so the code also works without display_value=all).
# ---------------------------------------------------------------------------
def _scalar(field):
    """Return the underlying stored value from a ServiceNow field (dict or plain)."""
    if isinstance(field, dict):
        return field.get("value")
    return field


def _parse_sn_datetime(value) -> Optional[datetime]:
    """Parse a ServiceNow datetime ('YYYY-MM-DD HH:MM:SS' UTC, or ISO) to aware UTC."""
    value = _scalar(value)
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
        if "T" not in s and " " in s:
            s = s.replace(" ", "T")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Transform helpers (public — covered by unit tests)
# ---------------------------------------------------------------------------
def extract_display_value(field_value) -> Optional[str]:
    """
    ServiceNow reference fields return as
    {"value": "sys_id_guid", "display_value": "human readable name"} when
    sysparm_display_value=all is set. This extracts the display_value for
    human-readable storage, falling back to value if display_value is absent.
    Handles the dict format, a plain string, and None safely.
    """
    if field_value is None:
        return None
    if isinstance(field_value, dict):
        display = field_value.get("display_value")
        if display not in (None, ""):
            return display
        value = field_value.get("value")
        return value if value not in (None, "") else None
    if isinstance(field_value, str):
        return field_value if field_value != "" else None
    return str(field_value)


def duration_to_minutes(duration_str) -> Optional[int]:
    """
    ServiceNow stores durations as 'X days HH:MM:SS' style strings. Converts to
    total whole minutes.
      '0 00:30:00' → 30
      '1 02:00:00' → 1560
      '' or None   → None
    Returns None if the value is empty or cannot be parsed.
    """
    if duration_str in (None, ""):
        return None
    s = str(duration_str).strip()
    if s == "":
        return None
    try:
        parts = s.split()
        if len(parts) == 1:
            days, hms = 0, parts[0]
        else:
            # Leading token is the day count; the final token is HH:MM:SS.
            days, hms = int(parts[0]), parts[-1]
        hours, minutes, seconds = (int(x) for x in hms.split(":"))
        return days * 1440 + hours * 60 + minutes + seconds // 60
    except (ValueError, IndexError):
        return None


def infer_sla_compliance(record: dict) -> bool:
    """
    Infers SLA compliance from a ServiceNow work order:
      - True  if work_end is at/before sla_due (or sla_due is empty),
      - False if work_end is after sla_due,
      - False if work_end is missing.
    """
    work_end = _parse_sn_datetime(record.get("work_end"))
    if work_end is None:
        return False
    sla_due = _parse_sn_datetime(record.get("sla_due"))
    if sla_due is None:
        return True  # completed with no SLA target → treat as compliant
    return work_end <= sla_due


def _to_str(value):
    value = _scalar(value)
    if value is None:
        return None
    return str(value)


def _to_utc(value):
    dt = _parse_sn_datetime(value)
    return dt.isoformat() if dt is not None else None


def _map_state(value, state_map: dict):
    value = _scalar(value)
    if value in (None, ""):
        return None
    return state_map.get(str(value))


def _duration_field_to_minutes(field):
    """Extract the human 'X days HH:MM:SS' string from a duration field, then convert."""
    if isinstance(field, dict):
        s = field.get("display_value")
        if s in (None, ""):
            s = field.get("value")
    else:
        s = field
    return duration_to_minutes(s)


def _apply_transform(name: str, value, field_map: dict, raw: Optional[dict] = None):
    """Dispatch a single field value (or the whole record) through a transform."""
    if name == "none":
        return _scalar(value)
    if name == "toString":
        return _to_str(value)
    if name == "toUTC":
        return _to_utc(value)
    if name == "mapServiceNowState":
        return _map_state(value, field_map.get("stateMap", {}))
    if name == "durationToMinutes":
        return _duration_field_to_minutes(value)
    if name == "extractDisplayValue":
        return extract_display_value(value)
    if name == "inferSlaCompliance":
        return infer_sla_compliance(raw if raw is not None else {})
    raise ValueError(f"Unknown transform: {name}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_access_token(
    instance_url: str, client_id: str, client_secret: str, username: str, password: str
) -> str:
    """
    Authenticates to ServiceNow. When an OAuth client_id/client_secret are
    configured, uses the OAuth 2.0 Password Grant flow at
    {instance_url}/oauth_token.do and returns the bearer access token. When no
    OAuth client is configured, falls back to constructing a Basic auth
    credential ("Basic <base64>") for development — fetch_workorders detects and
    sends whichever form this returns. Called once per sync run.
    """
    if client_id and client_secret:
        _require_requests()
        token_url = f"{instance_url.rstrip('/')}/oauth_token.do"
        data = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        }
        resp = requests.post(token_url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["access_token"]

    # BasicAuth fallback (development only).
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {creds}"


def _auth_header(access_token: str) -> str:
    """Bearer for OAuth tokens; pass through a pre-built 'Basic ...' credential."""
    if access_token.startswith("Basic "):
        return access_token
    return f"Bearer {access_token}"


# ---------------------------------------------------------------------------
# Fetch from ServiceNow
# ---------------------------------------------------------------------------
def _format_sn_datetime(dt: datetime) -> str:
    """ServiceNow query datetime format: 'YYYY-MM-DD HH:MM:SS' in UTC."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def build_query_params(
    fields: str, last_sync: Optional[datetime], page_size: int, offset: int = 0
) -> dict:
    """
    Builds the ServiceNow Table API query-parameter dict:
      - sysparm_fields: the comma-separated field list from config,
      - sysparm_query: 'sys_updated_on>{last_sync}' (incremental) plus an
        ORDERBYsys_updated_on for stable offset paging; when last_sync is None
        only the ordering is included (full initial sync),
      - sysparm_limit / sysparm_offset: page size and page offset,
      - sysparm_display_value: always 'all' so reference fields return both the
        sys_id (value) and the human-readable display_value.
    """
    params = {
        "sysparm_fields": fields,
        "sysparm_limit": page_size,
        "sysparm_offset": offset,
        "sysparm_display_value": "all",
        "sysparm_exclude_reference_link": "true",
    }
    query_parts = []
    if last_sync is not None:
        query_parts.append(f"sys_updated_on>{_format_sn_datetime(last_sync)}")
    query_parts.append("ORDERBYsys_updated_on")
    params["sysparm_query"] = "^".join(query_parts)
    return params


def fetch_workorders(
    access_token: str,
    instance_url: str,
    table: str,
    fields: str,
    last_sync: Optional[datetime],
    page_size: int = 100,
) -> list[dict]:
    """
    Fetches work orders from the ServiceNow Table API with offset-based
    pagination: sysparm_offset advances by page_size each round and paging stops
    once a page returns fewer than page_size records. Reference fields arrive as
    {"value", "display_value"} objects (sysparm_display_value=all). Returns a
    flat list of raw ServiceNow wm_order record dicts.
    """
    _require_requests()
    url = f"{instance_url.rstrip('/')}/api/now/table/{table}"
    headers = {"Accept": "application/json", "Authorization": _auth_header(access_token)}

    results: list[dict] = []
    offset = 0
    while True:
        params = build_query_params(fields, last_sync, page_size, offset)
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        batch = resp.json().get("result", [])
        results.extend(batch)
        if len(batch) < page_size:
            break  # last page reached
        offset += page_size
    return results


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def transform_workorder(raw: dict, field_map: dict, client_id: str) -> dict:
    """
    Applies field-map.json mappings to convert a raw ServiceNow wm_order into a
    normalized fma_workorder record. Handles every transform type (toString,
    none, toUTC, mapServiceNowState, durationToMinutes, extractDisplayValue,
    inferSlaCompliance). Missing optional fields default to None (no KeyError).
    Also stamps fma_sourcesystem (100000002), fma_syncedon (UTC now), and binds
    fma_client to the provided client_id. Returns a dict ready to PATCH.
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
# Write to Dataverse (uses shared utility)
# ---------------------------------------------------------------------------
def upsert_workorder(record: dict, target_url: str, access_token: str) -> dict:
    """
    Upserts a normalized fma_workorder record into the target Dataverse org via
    the shared alternate-key PATCH utility, keyed on fma_externalsourceid. The
    access_token here is the Dataverse token, not the ServiceNow one.
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
def get_last_sync_time(state_file: str = "connectors/servicenow/.sync_state.json") -> Optional[datetime]:
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


def save_sync_time(sync_time: datetime, state_file: str = "connectors/servicenow/.sync_state.json"):
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
      1. Get a ServiceNow access token (OAuth password grant, or Basic fallback).
      2. Get a Dataverse access token via the shared utility.
      3. Load the field map.
      4. Read the last sync time (or fall back to initialSyncDaysBack).
      5. Fetch changed work orders from the ServiceNow Table API.
      6. Transform each raw record.
      7. Upsert each record to the target Dataverse.
      8. Save the new sync timestamp.
    Logs counts: fetched, transformed, upserted, errors.
    """
    auth = config["auth"]
    sync_cfg = config["sync"]
    target = config["target"]

    # 1. ServiceNow token.
    sn_token = get_access_token(
        auth["instanceUrl"], auth.get("clientId"), auth.get("clientSecret"),
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

    # 5. Fetch.
    raw_records = fetch_workorders(
        sn_token, auth["instanceUrl"], sync_cfg["table"], sync_cfg["fields"],
        last_sync, page_size=sync_cfg.get("pageSize", 100),
    )
    logger.info("Fetched %d work orders from ServiceNow.", len(raw_records))

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
            logger.exception("Transform failed for record %s", _scalar(raw.get("sys_id")))
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
    with open("connectors/servicenow/config.json") as f:
        config = json.load(f)
    run_sync(config, client_id=os.environ["FMA_CLIENT_ID"])
