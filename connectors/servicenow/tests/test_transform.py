"""Unit tests for ServiceNow transform logic — no real API calls."""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402


FIELD_MAP = {
    "mappings": [
        {"source": "sys_id", "target": "fma_externalsourceid", "transform": "toString"},
        {"source": "number", "target": "fma_workordernumber", "transform": "none"},
        {"source": "state", "target": "fma_status", "transform": "mapServiceNowState"},
        {"source": "opened_at", "target": "fma_scheduledstart", "transform": "toUTC"},
        {"source": "sla_due", "target": "fma_scheduledend", "transform": "toUTC"},
        {"source": "work_start", "target": "fma_actualstart", "transform": "toUTC"},
        {"source": "work_end", "target": "fma_actualend", "transform": "toUTC"},
        {"source": "total_effort", "target": "fma_laborminutes", "transform": "durationToMinutes"},
        {"source": "cmdb_ci", "target": "fma_assetid", "transform": "extractDisplayValue"},
        {"source": "assigned_to", "target": "fma_technicianid", "transform": "extractDisplayValue"},
        {"source": "upon_approval", "target": "fma_slacompliant", "transform": "inferSlaCompliance"},
    ],
    "staticValues": {"fma_sourcesystem": 100000002},
    "stateMap": {
        "1": "Open", "2": "Open", "3": "In Progress", "4": "In Progress",
        "5": "In Progress", "6": "Completed", "7": "Cancelled", "8": "Cancelled",
    },
}

CLIENT_ID = "33333333-3333-3333-3333-333333333333"


# --- state mapping ----------------------------------------------------------
@pytest.mark.parametrize(
    "state,expected",
    [
        ("1", "Open"), ("2", "Open"), ("3", "In Progress"), ("4", "In Progress"),
        ("5", "In Progress"), ("6", "Completed"), ("7", "Cancelled"), ("8", "Cancelled"),
    ],
)
def test_every_state_maps(state, expected):
    """Every ServiceNow state integer maps to the correct fma_status."""
    raw = {"sys_id": "g", "state": state}
    rec = connector.transform_workorder(raw, FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] == expected


def test_state_maps_from_display_value_object():
    """A state field arriving as a {'value','display_value'} object still maps."""
    raw = {"sys_id": "g", "state": {"value": "6", "display_value": "Closed Complete"}}
    rec = connector.transform_workorder(raw, FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] == "Completed"


def test_unknown_state_maps_to_none():
    rec = connector.transform_workorder({"sys_id": "g", "state": "99"}, FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] is None


# --- extract_display_value --------------------------------------------------
def test_extract_display_value_dict_with_display():
    field = {"value": "abc123", "display_value": "Jane Tech"}
    assert connector.extract_display_value(field) == "Jane Tech"


def test_extract_display_value_dict_only_value():
    field = {"value": "abc123", "display_value": ""}
    assert connector.extract_display_value(field) == "abc123"


def test_extract_display_value_plain_string():
    assert connector.extract_display_value("Just A Name") == "Just A Name"


def test_extract_display_value_none():
    assert connector.extract_display_value(None) is None


# --- duration_to_minutes ----------------------------------------------------
def test_duration_half_hour():
    assert connector.duration_to_minutes("0 00:30:00") == 30


def test_duration_day_and_hours():
    assert connector.duration_to_minutes("1 02:00:00") == 1560


def test_duration_empty_string_none():
    assert connector.duration_to_minutes("") is None


def test_duration_none():
    assert connector.duration_to_minutes(None) is None


# --- infer_sla_compliance ---------------------------------------------------
def test_sla_true_when_work_end_before_due():
    rec = {"work_end": "2026-01-15 10:00:00", "sla_due": "2026-01-15 12:00:00"}
    assert connector.infer_sla_compliance(rec) is True


def test_sla_false_when_work_end_after_due():
    rec = {"work_end": "2026-01-15 13:00:00", "sla_due": "2026-01-15 12:00:00"}
    assert connector.infer_sla_compliance(rec) is False


def test_sla_false_when_work_end_missing():
    assert connector.infer_sla_compliance({"sla_due": "2026-01-15 12:00:00"}) is False


def test_sla_true_when_due_missing_but_completed():
    assert connector.infer_sla_compliance({"work_end": "2026-01-15 10:00:00"}) is True


# --- full record stamping ---------------------------------------------------
def _raw(**overrides):
    base = {
        "sys_id": {"value": "sysid-1", "display_value": "sysid-1"},
        "number": {"value": "WO0001001", "display_value": "WO0001001"},
        "state": {"value": "3", "display_value": "Work In Progress"},
        "opened_at": {"value": "2026-01-15 10:00:00", "display_value": "01/15/2026"},
        "sla_due": {"value": "2026-01-15 18:00:00", "display_value": "01/15/2026"},
        "work_start": {"value": "2026-01-15 10:05:00", "display_value": "01/15/2026"},
        "work_end": {"value": "2026-01-15 11:50:00", "display_value": "01/15/2026"},
        "total_effort": {"value": "1970-01-01 01:45:00", "display_value": "0 01:45:00"},
        "cmdb_ci": {"value": "ci-guid", "display_value": "Pump Station 4"},
        "assigned_to": {"value": "usr-guid", "display_value": "Jane Tech"},
        "upon_approval": {"value": "proceed", "display_value": "Proceed"},
    }
    base.update(overrides)
    return base


def test_source_system_always_servicenow_static():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_sourcesystem"] == 100000002
    rec2 = connector.transform_workorder({"sys_id": "x"}, FIELD_MAP, CLIENT_ID)
    assert rec2["fma_sourcesystem"] == 100000002


def test_synced_on_always_populated():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec.get("fma_syncedon")
    parsed = datetime.fromisoformat(rec["fma_syncedon"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_reference_fields_use_display_value():
    """cmdb_ci and assigned_to store the human-readable display value."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_assetid"] == "Pump Station 4"
    assert rec["fma_technicianid"] == "Jane Tech"


def test_duration_field_uses_display_value_string():
    """The duration field's 'X days HH:MM:SS' display value is converted to minutes."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_laborminutes"] == 105


def test_datetime_field_object_normalized_to_utc():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_scheduledstart"] == "2026-01-15T10:00:00+00:00"


def test_missing_optional_fields_default_to_none():
    rec = connector.transform_workorder({"sys_id": "only", "state": "6"}, FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "only"
    assert rec["fma_status"] == "Completed"
    assert rec["fma_workordernumber"] is None
    assert rec["fma_scheduledstart"] is None
    assert rec["fma_laborminutes"] is None
    assert rec["fma_assetid"] is None
