"""Unit tests for IFS transform logic — no real API calls."""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402


FIELD_MAP = {
    "mappings": [
        {"source": "WoNo", "target": "fma_externalsourceid", "transform": "toString"},
        {"source": "WoNo", "target": "fma_workordernumber", "transform": "toString"},
        {"source": "ObjState", "target": "fma_status", "transform": "mapIFSState"},
        {"source": "PlanningDate", "target": "fma_scheduledstart", "transform": "toUTC"},
        {"source": "FinishDate", "target": "fma_scheduledend", "transform": "toUTC"},
        {"source": "RealTimeSta", "target": "fma_actualstart", "transform": "toUTC"},
        {"source": "RealTimeFinish", "target": "fma_actualend", "transform": "toUTC"},
        {"source": "EmpNo", "target": "fma_technicianid", "transform": "toString"},
        {"source": "MchNo", "target": "fma_assetid", "transform": "toString"},
        {"source": "ActualCost", "target": "fma_totalcost", "transform": "toCurrency"},
        {"source": "ContractId", "target": "fma_slacompliant", "transform": "inferIFSSlaCCompliance"},
    ],
    "staticValues": {"fma_sourcesystem": 100000003},
    "stateMap": {
        "Released": "Open",
        "Started": "In Progress",
        "WorkDone": "In Progress",
        "Reported": "Completed",
        "Finished": "Completed",
        "Cancelled": "Cancelled",
        "Rejected": "Cancelled",
    },
}

CLIENT_ID = "44444444-4444-4444-4444-444444444444"


def _raw(**overrides):
    base = {
        "WoNo": 1001,
        "TaskSeq": 1,
        "ObjState": "Started",
        "PlanningDate": "2026-01-15T10:00:00Z",
        "FinishDate": "2026-01-15T18:00:00Z",
        "RealTimeSta": "2026-01-15T10:05:00Z",
        "RealTimeFinish": "2026-01-15T11:50:00Z",
        "EmpNo": "EMP-42",
        "MchNo": "MCH-9",
        "ActualCost": 512.25,
        "ContractId": "CON-1",
    }
    base.update(overrides)
    return base


# --- state mapping ----------------------------------------------------------
@pytest.mark.parametrize(
    "state,expected",
    [
        ("Released", "Open"),
        ("Started", "In Progress"),
        ("WorkDone", "In Progress"),
        ("Reported", "Completed"),
        ("Finished", "Completed"),
        ("Cancelled", "Cancelled"),
        ("Rejected", "Cancelled"),
    ],
)
def test_every_state_maps(state, expected):
    """Every IFS ObjState string maps to the correct fma_status."""
    rec = connector.transform_workorder(_raw(ObjState=state), FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] == expected


def test_unknown_state_defaults_to_open():
    """An unrecognized ObjState defaults to 'Open'."""
    rec = connector.transform_workorder(_raw(ObjState="SomethingElse"), FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] == "Open"


# --- calculate_labor_minutes ------------------------------------------------
def test_labor_minutes_from_valid_pair():
    """105 minutes between 10:05 and 11:50."""
    mins = connector.calculate_labor_minutes("2026-01-15T10:05:00Z", "2026-01-15T11:50:00Z")
    assert mins == 105


def test_labor_minutes_none_when_start_missing():
    assert connector.calculate_labor_minutes(None, "2026-01-15T11:50:00Z") is None


def test_labor_minutes_none_when_finish_missing():
    assert connector.calculate_labor_minutes("2026-01-15T10:05:00Z", None) is None


def test_transform_calculates_labor_minutes():
    """The full transform derives fma_laborminutes from the actual start/finish delta."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_laborminutes"] == 105


# --- infer_ifs_sla_compliance -----------------------------------------------
def test_sla_true_when_real_finish_before_scheduled():
    rec = {"RealTimeFinish": "2026-01-15T16:00:00Z", "FinishDate": "2026-01-15T18:00:00Z"}
    assert connector.infer_ifs_sla_compliance(rec) is True


def test_sla_false_when_real_finish_after_scheduled():
    rec = {"RealTimeFinish": "2026-01-15T19:00:00Z", "FinishDate": "2026-01-15T18:00:00Z"}
    assert connector.infer_ifs_sla_compliance(rec) is False


def test_sla_false_when_real_finish_missing():
    assert connector.infer_ifs_sla_compliance({"FinishDate": "2026-01-15T18:00:00Z"}) is False


def test_sla_true_when_finish_date_missing():
    assert connector.infer_ifs_sla_compliance({"RealTimeFinish": "2026-01-15T16:00:00Z"}) is True


# --- composite external id --------------------------------------------------
def test_external_id_is_wono_plus_taskseq():
    """fma_externalsourceid is WoNo + '_' + TaskSeq."""
    rec = connector.transform_workorder(_raw(WoNo=1001, TaskSeq=2), FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "1001_2"
    # Work order number stays the bare WoNo.
    assert rec["fma_workordernumber"] == "1001"


def test_external_id_falls_back_to_wono_when_no_taskseq():
    rec = connector.transform_workorder(_raw(WoNo=1001, TaskSeq=None), FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "1001"


# --- first time fix ---------------------------------------------------------
def test_first_time_fix_true_on_first_task_completed():
    rec = connector.transform_workorder(_raw(ObjState="Finished", TaskSeq=1), FIELD_MAP, CLIENT_ID)
    assert rec["fma_firsttimefix"] is True


def test_first_time_fix_false_on_later_task():
    rec = connector.transform_workorder(_raw(ObjState="Finished", TaskSeq=2), FIELD_MAP, CLIENT_ID)
    assert rec["fma_firsttimefix"] is False


# --- stamping ---------------------------------------------------------------
def test_source_system_always_ifs_static():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_sourcesystem"] == 100000003
    rec2 = connector.transform_workorder({"WoNo": 9}, FIELD_MAP, CLIENT_ID)
    assert rec2["fma_sourcesystem"] == 100000003


def test_synced_on_always_populated():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec.get("fma_syncedon")
    parsed = datetime.fromisoformat(rec["fma_syncedon"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_datetime_normalized_to_utc():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_scheduledstart"] == "2026-01-15T10:00:00+00:00"


def test_missing_optional_fields_default_to_none():
    rec = connector.transform_workorder({"WoNo": 7, "ObjState": "Released"}, FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "7"
    assert rec["fma_status"] == "Open"
    assert rec["fma_scheduledstart"] is None
    assert rec["fma_totalcost"] is None
    assert rec["fma_laborminutes"] is None
