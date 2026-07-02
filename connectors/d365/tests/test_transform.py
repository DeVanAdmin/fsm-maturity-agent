"""Unit tests for transform_workorder — no real API calls."""

import os
import sys
from datetime import datetime, timezone

import pytest

# Make the connector module importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402


# The field map used by the real connector, inlined here so the tests are
# self-contained and independent of file layout.
FIELD_MAP = {
    "mappings": [
        {"source": "msdyn_workorderid", "target": "fma_externalsourceid", "transform": "toString"},
        {"source": "msdyn_name", "target": "fma_workordernumber", "transform": "none"},
        {"source": "msdyn_systemstatus", "target": "fma_status", "transform": "mapD365Status"},
        {"source": "msdyn_timewindowstart", "target": "fma_scheduledstart", "transform": "toUTC"},
        {"source": "msdyn_timewindowend", "target": "fma_scheduledend", "transform": "toUTC"},
        {"source": "msdyn_actualstarttime", "target": "fma_actualstart", "transform": "toUTC"},
        {"source": "msdyn_actualduration", "target": "fma_laborminutes", "transform": "none"},
        {"source": "_msdyn_primaryincidenttype_value", "target": "fma_assetid", "transform": "toString"},
        {"source": "_ownerid_value", "target": "fma_technicianid", "transform": "toString"},
        {"source": "msdyn_totalcost", "target": "fma_totalcost", "transform": "toCurrency"},
    ],
    "staticValues": {"fma_sourcesystem": 100000000},
    "statusMap": {
        "690970000": "Open",
        "690970001": "Open",
        "690970002": "Open",
        "690970003": "In Progress",
        "690970004": "In Progress",
        "690970005": "Completed",
        "690970006": "Cancelled",
    },
}

CLIENT_ID = "11111111-1111-1111-1111-111111111111"


def _raw(**overrides):
    base = {
        "msdyn_workorderid": "wo-guid-1",
        "msdyn_name": "WO-0001",
        "msdyn_systemstatus": 690970003,
        "msdyn_timewindowstart": "2026-01-15T10:00:00Z",
        "msdyn_timewindowend": "2026-01-15T12:00:00Z",
        "msdyn_actualstarttime": "2026-01-15T10:05:00Z",
        "msdyn_actualduration": 115,
        "_msdyn_primaryincidenttype_value": "asset-guid-1",
        "_ownerid_value": "tech-guid-1",
        "msdyn_totalcost": 250.5,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "code,expected",
    [
        (690970000, "Open"),
        (690970001, "Open"),
        (690970002, "Open"),
        (690970003, "In Progress"),
        (690970004, "In Progress"),
        (690970005, "Completed"),
        (690970006, "Cancelled"),
    ],
)
def test_status_mapping_every_code(code, expected):
    """Every D365 status code maps to the correct fma_status string."""
    rec = connector.transform_workorder(_raw(msdyn_systemstatus=code), FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] == expected


def test_unknown_status_maps_to_none():
    """An unrecognized status code yields None rather than raising."""
    rec = connector.transform_workorder(_raw(msdyn_systemstatus=999999999), FIELD_MAP, CLIENT_ID)
    assert rec["fma_status"] is None


def test_datetime_converted_to_utc():
    """A 'Z' UTC timestamp is normalized to an explicit +00:00 offset."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_scheduledstart"] == "2026-01-15T10:00:00+00:00"
    # Parsed value is genuinely UTC.
    parsed = datetime.fromisoformat(rec["fma_scheduledstart"])
    assert parsed.utcoffset().total_seconds() == 0


def test_naive_datetime_assumed_utc():
    """A datetime with no offset is treated as UTC."""
    rec = connector.transform_workorder(
        _raw(msdyn_timewindowstart="2026-03-01T08:30:00"), FIELD_MAP, CLIENT_ID
    )
    assert rec["fma_scheduledstart"] == "2026-03-01T08:30:00+00:00"


def test_currency_field_coerced_to_float():
    """Currency values are coerced to float."""
    rec = connector.transform_workorder(_raw(msdyn_totalcost="349.99"), FIELD_MAP, CLIENT_ID)
    assert rec["fma_totalcost"] == pytest.approx(349.99)
    assert isinstance(rec["fma_totalcost"], float)


def test_missing_optional_fields_default_to_none():
    """Missing optional source fields become None — no KeyError."""
    sparse = {"msdyn_workorderid": "wo-guid-9", "msdyn_systemstatus": 690970005}
    rec = connector.transform_workorder(sparse, FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "wo-guid-9"
    assert rec["fma_status"] == "Completed"
    assert rec["fma_workordernumber"] is None
    assert rec["fma_scheduledstart"] is None
    assert rec["fma_actualstart"] is None
    assert rec["fma_totalcost"] is None
    assert rec["fma_technicianid"] is None


def test_source_system_always_set():
    """fma_sourcesystem is always stamped with the D365 static value."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_sourcesystem"] == 100000000
    # Even when the source row is nearly empty.
    rec2 = connector.transform_workorder({"msdyn_workorderid": "x"}, FIELD_MAP, CLIENT_ID)
    assert rec2["fma_sourcesystem"] == 100000000


def test_synced_on_always_populated():
    """fma_syncedon is always populated with a parseable UTC timestamp."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec.get("fma_syncedon")
    parsed = datetime.fromisoformat(rec["fma_syncedon"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_client_binding_set():
    """fma_client is bound to the provided client id."""
    rec = connector.transform_workorder(_raw(), FIELD_MAP, CLIENT_ID)
    assert rec["fma_client"] == CLIENT_ID
    assert rec["fma_client@odata.bind"] == f"/fma_clients({CLIENT_ID})"


def test_external_source_id_preserved_as_string():
    """The originating D365 id is preserved as a string for traceability."""
    rec = connector.transform_workorder(_raw(msdyn_workorderid=12345), FIELD_MAP, CLIENT_ID)
    assert rec["fma_externalsourceid"] == "12345"
    assert isinstance(rec["fma_externalsourceid"], str)
