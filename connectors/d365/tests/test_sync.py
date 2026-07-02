"""Unit tests for sync orchestration — no real API calls (requests is mocked)."""

import os
import sys
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402


FIELD_MAP = {
    "mappings": [
        {"source": "msdyn_workorderid", "target": "fma_externalsourceid", "transform": "toString"},
        {"source": "msdyn_name", "target": "fma_workordernumber", "transform": "none"},
    ],
    "staticValues": {"fma_sourcesystem": 100000000},
    "statusMap": {},
}


# --- get_last_sync_time -----------------------------------------------------
def test_get_last_sync_time_none_when_no_file(tmp_path):
    """Returns None when the state file does not exist (full sync)."""
    missing = str(tmp_path / "nope.json")
    assert connector.get_last_sync_time(missing) is None


def test_get_last_sync_time_reads_datetime(tmp_path):
    """Returns the correct datetime when a state file exists."""
    state = str(tmp_path / "state.json")
    with open(state, "w") as f:
        json.dump({"last_sync": "2026-02-01T00:00:00+00:00"}, f)
    result = connector.get_last_sync_time(state)
    assert result == datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_get_last_sync_time_handles_z_suffix(tmp_path):
    """A 'Z'-suffixed timestamp is parsed correctly."""
    state = str(tmp_path / "state.json")
    with open(state, "w") as f:
        json.dump({"last_sync": "2026-02-01T00:00:00Z"}, f)
    result = connector.get_last_sync_time(state)
    assert result == datetime(2026, 2, 1, tzinfo=timezone.utc)


# --- save_sync_time ---------------------------------------------------------
def test_save_sync_time_writes_iso(tmp_path):
    """Writes an ISO-8601 timestamp that round-trips back to the same value."""
    state = str(tmp_path / "state.json")
    ts = datetime(2026, 3, 15, 9, 30, tzinfo=timezone.utc)
    connector.save_sync_time(ts, state)
    with open(state) as f:
        written = json.load(f)
    assert written["last_sync"] == "2026-03-15T09:30:00+00:00"
    assert connector.get_last_sync_time(state) == ts


# --- fetch_workorders -------------------------------------------------------
def _mock_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_builds_filter_when_last_sync_provided():
    """When last_sync is given, the OData $filter uses modifiedon gt <ts>."""
    last_sync = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"value": []})
        connector.fetch_workorders("tok", "https://x/api/data/v9.2/msdyn_workorders",
                                   last_sync, page_size=50, field_map=FIELD_MAP)
        _, kwargs = m.get.call_args
        params = kwargs["params"]
        assert "$filter" in params
        assert params["$filter"] == "modifiedon gt 2026-01-01T00:00:00+00:00"
        # $select is limited to mapped source fields.
        assert params["$select"] == "msdyn_workorderid,msdyn_name"


def test_fetch_omits_filter_when_last_sync_none():
    """When last_sync is None, no $filter is sent (full initial sync)."""
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"value": []})
        connector.fetch_workorders("tok", "https://x/api/data/v9.2/msdyn_workorders",
                                   None, page_size=50, field_map=FIELD_MAP)
        _, kwargs = m.get.call_args
        assert "$filter" not in kwargs["params"]


def test_fetch_follows_next_link_pagination():
    """@odata.nextLink pages are followed until exhausted."""
    page1 = {"value": [{"msdyn_workorderid": "1"}], "@odata.nextLink": "https://x/next"}
    page2 = {"value": [{"msdyn_workorderid": "2"}]}
    with mock.patch.object(connector, "requests") as m:
        m.get.side_effect = [_mock_response(page1), _mock_response(page2)]
        rows = connector.fetch_workorders("tok", "https://x/api/data/v9.2/msdyn_workorders",
                                          None, field_map=FIELD_MAP)
        assert [r["msdyn_workorderid"] for r in rows] == ["1", "2"]
        assert m.get.call_count == 2


# --- upsert_workorder -------------------------------------------------------
def test_upsert_uses_patch_not_post():
    """Upsert must use PATCH (idempotent alternate-key upsert), never POST."""
    record = {"fma_externalsourceid": "wo-1", "fma_workordernumber": "WO-1", "fma_client": "c"}
    with mock.patch.object(connector, "requests") as m:
        resp = mock.Mock()
        resp.status_code = 204
        resp.raise_for_status.return_value = None
        m.patch.return_value = resp
        result = connector.upsert_workorder(record, "https://target", "tok")
        assert m.patch.called
        assert not m.post.called
        # Targets the alternate key and strips the helper-only fma_client field.
        url = m.patch.call_args[0][0]
        assert "fma_workorders(fma_externalsourceid='wo-1')" in url
        sent = json.loads(m.patch.call_args[1]["data"])
        assert "fma_client" not in sent
        assert result["created"] is False
