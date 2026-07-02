"""Unit tests for the Custom FSM gateway handler — Dataverse upsert is mocked."""

import os
import sys
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

# Make the handler module importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "handler")))

import azure.functions as func  # noqa: E402
import function_app  # noqa: E402


VALID_CLIENT_ID = "3f9a1c2e-7b44-4e8a-9c1d-2a6f5b0e91d7"


def _payload(**overrides):
    base = {
        "clientId": VALID_CLIENT_ID,
        "externalSourceId": "WO-2026-000148",
        "workOrderNumber": "000148",
        "status": "Completed",
        "scheduledStart": "2026-03-15T08:00:00Z",
        "actualEnd": "2026-03-15T10:47:00Z",
        "firstTimeFix": True,
        "laborMinutes": 155,
    }
    base.update(overrides)
    return base


def _make_request(body, route="workorders", correlation_id="corr-123"):
    """Construct a real func.HttpRequest with a JSON body."""
    return func.HttpRequest(
        method="POST",
        url=f"https://x/api/fma/v1/{route}",
        headers={"Content-Type": "application/json", "X-Correlation-Id": correlation_id},
        params={},
        body=json.dumps(body).encode("utf-8"),
    )


# --- validate_payload -------------------------------------------------------
def test_validate_valid_payload():
    ok, errors = function_app.validate_payload(_payload())
    assert ok is True
    assert errors == []


def test_validate_missing_client_id():
    ok, errors = function_app.validate_payload(_payload(clientId=None))
    assert ok is False
    assert any("clientId is required" in e for e in errors)


def test_validate_missing_external_source_id():
    p = _payload()
    del p["externalSourceId"]
    ok, errors = function_app.validate_payload(p)
    assert ok is False
    assert any("externalSourceId is required" in e for e in errors)


def test_validate_status_not_in_enum():
    ok, errors = function_app.validate_payload(_payload(status="Done"))
    assert ok is False
    assert any("status must be one of" in e for e in errors)


def test_validate_scheduled_start_not_iso8601():
    ok, errors = function_app.validate_payload(_payload(scheduledStart="15/03/2026 8am"))
    assert ok is False
    assert any("scheduledStart must be an ISO 8601 datetime" in e for e in errors)


def test_validate_client_id_not_uuid():
    ok, errors = function_app.validate_payload(_payload(clientId="not-a-uuid"))
    assert ok is False
    assert any("clientId must be a valid UUID" in e for e in errors)


# --- map_to_fma_record ------------------------------------------------------
def test_map_sets_source_system():
    rec = function_app.map_to_fma_record(_payload())
    assert rec["fma_sourcesystem"] == 100000004


def test_map_sets_synced_on_utc():
    rec = function_app.map_to_fma_record(_payload())
    parsed = datetime.fromisoformat(rec["fma_syncedon"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_map_required_fields():
    rec = function_app.map_to_fma_record(_payload())
    assert rec["fma_externalsourceid"] == "WO-2026-000148"
    assert rec["fma_workordernumber"] == "000148"
    assert rec["fma_status"] == "Completed"
    assert rec["fma_scheduledstart"] == "2026-03-15T08:00:00+00:00"
    assert rec["fma_client@odata.bind"] == f"/fma_clients({VALID_CLIENT_ID})"
    # Optional provided fields map through; absent ones are not present.
    assert rec["fma_laborminutes"] == 155
    assert "fma_travelminutes" not in rec


# --- get_error_response -----------------------------------------------------
def test_error_response_envelope_and_status():
    resp = function_app.get_error_response("validation_error", "bad input", "corr-9", 400)
    assert resp.status_code == 400
    body = json.loads(resp.get_body())
    assert body == {"error": {"code": "validation_error", "message": "bad input", "correlationId": "corr-9"}}


# --- ingest_workorder (single) ----------------------------------------------
def test_ingest_single_success():
    req = _make_request(_payload())
    with mock.patch.object(function_app, "get_dataverse_token", return_value="tok"), \
         mock.patch.object(function_app, "upsert_record", return_value={"created": True}) as up:
        resp = function_app.ingest_workorder(req)
    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert body["externalSourceId"] == "WO-2026-000148"
    assert body["created"] is True
    assert up.called


def test_ingest_single_validation_error_returns_400():
    req = _make_request(_payload(status="Nope"))
    with mock.patch.object(function_app, "get_dataverse_token") as tok, \
         mock.patch.object(function_app, "upsert_record") as up:
        resp = function_app.ingest_workorder(req)
    assert resp.status_code == 400
    body = json.loads(resp.get_body())
    assert body["error"]["code"] == "validation_error"
    tok.assert_not_called()
    up.assert_not_called()


# --- ingest_workorder_batch -------------------------------------------------
def test_batch_partial_success():
    """One valid record upserts; one invalid record is reported without blocking."""
    good = _payload(externalSourceId="WO-1")
    bad = _payload(externalSourceId="WO-2", status="Invalid")
    req = _make_request([good, bad], route="workorders/batch")
    with mock.patch.object(function_app, "get_dataverse_token", return_value="tok"), \
         mock.patch.object(function_app, "upsert_record", return_value={"created": True}):
        resp = function_app.ingest_workorder_batch(req)
    assert resp.status_code == 200
    summary = json.loads(resp.get_body())
    assert summary["processed"] == 2
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["errors"][0]["externalSourceId"] == "WO-2"


def test_batch_partial_success_on_upsert_exception():
    """A record that validates but fails the upsert is counted as failed, not fatal."""
    a = _payload(externalSourceId="WO-A")
    b = _payload(externalSourceId="WO-B")
    with mock.patch.object(function_app, "get_dataverse_token", return_value="tok"), \
         mock.patch.object(function_app, "upsert_record",
                           side_effect=[{"created": True}, RuntimeError("boom")]):
        resp = function_app.ingest_workorder_batch(_make_request([a, b], route="workorders/batch"))
    summary = json.loads(resp.get_body())
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["errors"][0]["externalSourceId"] == "WO-B"
    assert "boom" in summary["errors"][0]["error"]


def test_batch_rejects_more_than_100():
    big = [_payload(externalSourceId=f"WO-{i}") for i in range(101)]
    req = _make_request(big, route="workorders/batch")
    with mock.patch.object(function_app, "get_dataverse_token") as tok, \
         mock.patch.object(function_app, "upsert_record") as up:
        resp = function_app.ingest_workorder_batch(req)
    assert resp.status_code == 400
    body = json.loads(resp.get_body())
    assert body["error"]["code"] == "validation_error"
    assert "maximum of 100" in body["error"]["message"]
    tok.assert_not_called()
    up.assert_not_called()
