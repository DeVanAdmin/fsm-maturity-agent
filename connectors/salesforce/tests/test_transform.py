"""Unit tests for the Salesforce transform_workorder — no real API calls."""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402


FIELD_MAP = {
    "mappings": [
        {"source": "Id", "target": "fma_externalsourceid", "transform": "toString"},
        {"source": "WorkOrderNumber", "target": "fma_workordernumber", "transform": "none"},
        {"source": "Status", "target": "fma_status", "transform": "mapSalesforceStatus"},
        {"source": "StartDate", "target": "fma_scheduledstart", "transform": "toUTC"},
        {"source": "EndDate", "target": "fma_scheduledend", "transform": "toUTC"},
        {"source": "ActualStartTime", "target": "fma_actualstart", "transform": "toUTC"},
        {"source": "ActualEndTime", "target": "fma_actualend", "transform": "toUTC"},
        {"source": "Duration", "target": "fma_laborminutes", "transform": "none"},
        {"source": "AssetId", "target": "fma_assetid", "transform": "toString"},
        {"source": "OwnerId", "target": "fma_technicianid", "transform": "toString"},
        {"source": "TotalPrice", "target": "fma_totalcost", "transform": "toCurrency"},
        {"source": "IsClosed", "target": "fma_firsttimefix", "transform": "inferFirstTimeFix"},
    ],
    "staticValues": {"fma_sourcesystem": 100000001},
    "statusMap": {
        "New": "Open",
        "In Progress": "In Progress",
        "On Hold": "In Progress",
        "Completed": "Completed",
        "Cannot Complete": "Cancelled",
        "Closed": "Completed",
        "Cancelled": "Cancelled",
    },
}

CLIENT_ID = "22222222-2222-2222-2222-222222222222"


def _raw(**overrides):
    base = {
        "Id": "5WO000000001",
        "WorkOrderNumber": "00001234",
        "Status": "In Progress",
        "StartDate": "2026-01-15T10:00:00.000+0000",
        "EndDate": "2026-01-15T12:00:00.000+0000",
        "ActualStartTime": "2026-01-15T10:05:00.000+0000",
        "ActualEndTime": "2026-01-15T11:50:00.000+0000",
        "Duration": 105,
        "AssetId": "02i000000000ABC",
        "OwnerId": "005000000000XYZ",
        "TotalPrice": 412.75,
        "IsClosed": False,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "sf_status,expected",
    [
        ("New", "Open"),
        ("In Progress", "In Progress"),
        ("On Hold", "In Progress"),
        ("Completed", "Completed"),
        ("Cannot Complete", "Cancelled"),
        ("Closed", "Completed"),
        ("Cancelled", "Cancelled"),
    ],
)
def test_status_mapping_every_value(sf_status, expected):
    """Every Salesforce status string maps to the correct fma_status."""
    rec = connector.transform_workorder(_raw(Status=sf_status), FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] == expected


def test_unknown_status_maps_to_none():
    rec = connector.transform_workorder(_raw(Status="Weird"), FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] is None


def test_datetime_converts_to_utc():
    """Salesforce '+0000' timestamps normalize to a UTC ISO string."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_scheduledstart"] == "2026-01-15T10:00:00+00:00"
    parsed = datetime.fromisoformat(rec["fma_scheduledstart"])
    assert parsed.utcoffset().total_seconds() == 0


def test_datetime_with_offset_normalized_to_utc():
    """A non-UTC offset is converted (not just stripped) to UTC."""
    rec = connector.transform_workorder(
        _raw(StartDate="2026-01-15T10:00:00.000+0500"), FIELD_MAP, CLIENT_ID
    )
    assert rec["fma_scheduledstart"] == "2026-01-15T05:00:00+00:00"


def test_infer_ftf_false_when_not_closed():
    """inferFirstTimeFix is False when IsClosed is False."""
    rec = connector.transform_workorder(_raw(IsClosed=False), FIELD_MAP, CLIENT_ID)
    assert rec["fma_firsttimefix"] is False


def test_infer_ftf_true_when_closed_no_repeat_data():
    """inferFirstTimeFix is True when IsClosed is True and no repeat data exists."""
    rec = connector.transform_workorder(_raw(IsClosed=True), FIELD_MAP, CLIENT_ID)
    assert rec["fma_firsttimefix"] is True


def test_infer_ftf_false_when_closed_but_multiple_visits():
    """When visit data shows repeats, it's not a first-time fix."""
    rec = connector.transform_workorder(
        _raw(IsClosed=True, VisitCount=3), FIELD_MAP, CLIENT_ID
    )
    assert rec["fma_firsttimefix"] is False


def test_missing_optional_fields_default_to_none():
    sparse = {"Id": "5WO000000009", "Status": "Completed", "IsClosed": True}
    rec = connector.transform_workorder(sparse, FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "5WO000000009"
    assert rec["fma_status"] == "Completed"
    assert rec["fma_workordernumber"] is None
    assert rec["fma_scheduledstart"] is None
    assert rec["fma_actualend"] is None
    assert rec["fma_totalcost"] is None
    assert rec["fma_technicianid"] is None


def test_source_system_always_salesforce_static():
    """fma_sourcesystem is always the Salesforce static value 100000001."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_sourcesystem"] == 100000001
    rec2 = connector.transform_workorder({"Id": "x"}, FIELD_MAP, CLIENT_ID)
    assert rec2["fma_sourcesystem"] == 100000001


def test_synced_on_always_populated():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec.get("fma_syncedon")
    parsed = datetime.fromisoformat(rec["fma_syncedon"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_client_binding_set():
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_client"] == CLIENT_ID
    assert rec["fma_client@odata.bind"] == f"/fma_clients({CLIENT_ID})"
