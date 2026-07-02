# connectors/custom/handler/function_app.py
# Azure Function — Custom FSM gateway handler
# Sits behind Azure API Management. Receives validated inbound
# work order payloads and writes them to fma_workorder in Dataverse.

import os
import sys
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

import azure.functions as func

# Route Dataverse writes through the single shared utility (connectors/shared).
# function_app.py lives at connectors/custom/handler/ — go up two levels to
# reach connectors/, which contains the `shared` package.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from shared.dataverse_upsert import get_dataverse_token, upsert_record  # noqa: E402

logger = logging.getLogger("custom_gateway")

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# Custom source-system option value (matches fma_sourcesystem in the data model).
CUSTOM_SOURCE_SYSTEM = 100000004

REQUIRED_FIELDS = ["clientId", "externalSourceId", "workOrderNumber", "status", "scheduledStart"]
VALID_STATUSES = ["Open", "In Progress", "Completed", "Cancelled"]
DATETIME_FIELDS = ["scheduledStart", "scheduledEnd", "actualStart", "actualEnd"]
MAX_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Validation & mapping helpers
# ---------------------------------------------------------------------------
def _is_iso8601(value: str) -> bool:
    """True if value parses as an ISO-8601 datetime (accepting a trailing 'Z')."""
    if not isinstance(value, str) or value.strip() == "":
        return False
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def _is_uuid(value: str) -> bool:
    """True if value is a syntactically valid UUID string."""
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _to_utc(value: Optional[str]) -> Optional[str]:
    """Normalize an ISO-8601 datetime string to a UTC ISO-8601 string; None-safe."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def validate_payload(payload: dict) -> tuple[bool, list[str]]:
    """
    Validates an inbound payload against required fields and value constraints —
    pure Python, no jsonschema dependency (keeps Azure Function cold starts fast).
    Checks: required fields present and non-empty; status is a valid enum value;
    datetime fields are valid ISO-8601; clientId is a valid UUID.
    Returns (is_valid, errors) where errors is a list of human-readable messages.
    """
    errors: list[str] = []

    if not isinstance(payload, dict):
        return False, ["payload must be a JSON object"]

    # Required fields present and non-empty.
    for field in REQUIRED_FIELDS:
        value = payload.get(field)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            errors.append(f"{field} is required")

    # clientId must be a valid UUID (only if present).
    if payload.get("clientId") is not None and not _is_uuid(payload.get("clientId")):
        errors.append("clientId must be a valid UUID")

    # status must be one of the allowed enum values (only if present).
    if payload.get("status") is not None and payload.get("status") not in VALID_STATUSES:
        errors.append("status must be one of: " + ", ".join(VALID_STATUSES))

    # Any provided datetime field must be valid ISO-8601.
    for field in DATETIME_FIELDS:
        if payload.get(field) is not None and not _is_iso8601(payload.get(field)):
            errors.append(f"{field} must be an ISO 8601 datetime (e.g. 2026-03-15T14:30:00Z)")

    return (len(errors) == 0, errors)


def map_to_fma_record(payload: dict) -> dict:
    """
    Maps a validated inbound payload to a fma_workorder Dataverse record. The
    inbound schema was designed to match our data model, so fields map 1:1 — no
    complex transform layer. Sets fma_sourcesystem to 100000004 (Custom) and
    fma_syncedon to the current UTC timestamp. Optional fields are included only
    when present so absent fields are never sent as nulls.
    """
    record: dict = {
        "fma_externalsourceid": str(payload["externalSourceId"]),
        "fma_workordernumber": payload.get("workOrderNumber"),
        "fma_status": payload.get("status"),
        "fma_scheduledstart": _to_utc(payload.get("scheduledStart")),
    }

    optional_datetimes = {
        "scheduledEnd": "fma_scheduledend",
        "actualStart": "fma_actualstart",
        "actualEnd": "fma_actualend",
    }
    for src, tgt in optional_datetimes.items():
        if payload.get(src) is not None:
            record[tgt] = _to_utc(payload.get(src))

    optional_scalars = {
        "firstTimeFix": "fma_firsttimefix",
        "visitCount": "fma_visitcount",
        "technicianId": "fma_technicianid",
        "assetId": "fma_assetid",
        "slaCompliant": "fma_slacompliant",
        "laborMinutes": "fma_laborminutes",
        "travelMinutes": "fma_travelminutes",
        "totalCost": "fma_totalcost",
    }
    for src, tgt in optional_scalars.items():
        if payload.get(src) is not None:
            record[tgt] = payload.get(src)

    # Client lookup binding.
    record["fma_client@odata.bind"] = f"/fma_clients({payload['clientId']})"

    record["fma_sourcesystem"] = CUSTOM_SOURCE_SYSTEM
    record["fma_syncedon"] = datetime.now(timezone.utc).isoformat()
    return record


def get_error_response(
    code: str, message: str, correlation_id: str, status_code: int
) -> func.HttpResponse:
    """
    Builds a consistent JSON error response with the shared envelope:
    { "error": { "code": str, "message": str, "correlationId": str } }.
    """
    body = {"error": {"code": code, "message": message, "correlationId": correlation_id}}
    return func.HttpResponse(
        body=json.dumps(body), status_code=status_code, mimetype="application/json"
    )


# ---------------------------------------------------------------------------
# Dataverse write helpers
# ---------------------------------------------------------------------------
def _dataverse_url() -> str:
    return os.environ.get("FMA_DATAVERSE_URL", "")


def _get_dataverse_token() -> str:
    """Acquire a Dataverse write token from environment-provided credentials."""
    return get_dataverse_token(
        os.environ.get("FMA_DATAVERSE_TENANT_ID", ""),
        os.environ.get("FMA_DATAVERSE_CLIENT_ID", ""),
        os.environ.get("FMA_DATAVERSE_CLIENT_SECRET", ""),
        _dataverse_url(),
    )


def _upsert(record: dict, token: str) -> dict:
    """Upsert a single mapped record to fma_workorder via the shared utility."""
    return upsert_record(
        record,
        "fma_workorders",
        "fma_externalsourceid",
        record["fma_externalsourceid"],
        _dataverse_url(),
        token,
    )


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
def ingest_workorder(req: func.HttpRequest) -> func.HttpResponse:
    """
    Single work order ingest endpoint.
      1. Parses and validates the request body against the inbound schema.
      2. Maps the inbound payload to an fma_workorder record.
      3. Gets a Dataverse token.
      4. Upserts to fma_workorder via the shared utility.
      5. Returns 200 with the upsert result, or 400/500 with an error envelope.
    Logs the X-Correlation-Id header for tracing.
    """
    correlation_id = req.headers.get("X-Correlation-Id", "")

    try:
        payload = req.get_json()
    except (ValueError, TypeError):
        return get_error_response("validation_error", "Request body is not valid JSON",
                                  correlation_id, 400)

    is_valid, errors = validate_payload(payload)
    if not is_valid:
        logger.info("Validation failed [%s]: %s", correlation_id, errors)
        return get_error_response("validation_error", "; ".join(errors), correlation_id, 400)

    try:
        record = map_to_fma_record(payload)
        token = _get_dataverse_token()
        result = _upsert(record, token)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Upsert failed [%s]", correlation_id)
        return get_error_response("internal_error", str(exc), correlation_id, 500)

    body = {
        "status": "ok",
        "externalSourceId": record["fma_externalsourceid"],
        "created": bool(result.get("created")),
    }
    return func.HttpResponse(body=json.dumps(body), status_code=200, mimetype="application/json")


def ingest_workorder_batch(req: func.HttpRequest) -> func.HttpResponse:
    """
    Batch work order ingest endpoint. Accepts an array of up to 100 payloads and
    processes each individually — partial success is allowed, so one bad record
    does not block the rest. Returns a summary:
    { "processed": N, "succeeded": N, "failed": N, "errors": [{externalSourceId, error}] }.
    """
    correlation_id = req.headers.get("X-Correlation-Id", "")

    try:
        records = req.get_json()
    except (ValueError, TypeError):
        return get_error_response("validation_error", "Request body is not valid JSON",
                                  correlation_id, 400)

    if not isinstance(records, list):
        return get_error_response("validation_error", "Batch body must be a JSON array",
                                  correlation_id, 400)

    if len(records) > MAX_BATCH_SIZE:
        return get_error_response(
            "validation_error",
            f"Batch exceeds the maximum of {MAX_BATCH_SIZE} records (received {len(records)})",
            correlation_id, 400,
        )

    succeeded = 0
    failed = 0
    errors: list[dict] = []
    token: Optional[str] = None

    for item in records:
        ext_id = item.get("externalSourceId") if isinstance(item, dict) else None

        is_valid, verrors = validate_payload(item)
        if not is_valid:
            failed += 1
            errors.append({"externalSourceId": ext_id, "error": "; ".join(verrors)})
            continue

        try:
            record = map_to_fma_record(item)
            if token is None:  # acquire once, reuse for the whole batch
                token = _get_dataverse_token()
            _upsert(record, token)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append({"externalSourceId": ext_id, "error": str(exc)})

    summary = {
        "processed": len(records),
        "succeeded": succeeded,
        "failed": failed,
        "errors": errors,
    }
    logger.info("Batch processed [%s]: %s", correlation_id,
                {k: summary[k] for k in ("processed", "succeeded", "failed")})
    return func.HttpResponse(body=json.dumps(summary), status_code=200, mimetype="application/json")


# ---------------------------------------------------------------------------
# Route registration
#
# The handlers above are defined as plain functions (so they stay directly
# callable and unit-testable). We register them with the FunctionApp here — the
# side effect is identical to using @app.route as a decorator, but the module
# names remain bound to the underlying callables rather than to builder objects.
# ---------------------------------------------------------------------------
app.route(route="workorders", methods=["POST"])(ingest_workorder)
app.route(route="workorders/batch", methods=["POST"])(ingest_workorder_batch)
