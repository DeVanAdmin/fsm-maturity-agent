"""Unit tests for ServiceNow sync orchestration — no real API calls."""

import os
import sys
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402
from shared import dataverse_upsert  # noqa: E402


FIELDS = "sys_id,number,state,opened_at,work_end,assigned_to,cmdb_ci,total_effort,sla_due"


# --- build_query_params -----------------------------------------------------
def test_query_includes_filter_when_last_sync_provided():
    """sys_updated_on> filter is present when last_sync is provided."""
    last_sync = datetime(2026, 1, 1, tzinfo=timezone.utc)
    params = connector.build_query_params(FIELDS, last_sync, 100, offset=0)
    assert "sys_updated_on>" in params["sysparm_query"]


def test_query_omits_filter_when_last_sync_none():
    """No sys_updated_on> filter when last_sync is None (full sync)."""
    params = connector.build_query_params(FIELDS, None, 100, offset=0)
    assert "sys_updated_on>" not in params["sysparm_query"]


def test_query_datetime_format():
    """The query datetime is 'YYYY-MM-DD HH:MM:SS' (space-separated, no T/Z)."""
    last_sync = datetime(2026, 3, 5, 14, 30, 0, tzinfo=timezone.utc)
    params = connector.build_query_params(FIELDS, last_sync, 100)
    assert "sys_updated_on>2026-03-05 14:30:00" in params["sysparm_query"]


def test_query_datetime_converts_nonutc():
    from datetime import timedelta
    tz = timezone(timedelta(hours=5))
    last_sync = datetime(2026, 3, 5, 14, 30, 0, tzinfo=tz)  # 09:30 UTC
    params = connector.build_query_params(FIELDS, last_sync, 100)
    assert "sys_updated_on>2026-03-05 09:30:00" in params["sysparm_query"]


def test_display_value_always_all():
    """sysparm_display_value is always 'all', with or without a filter."""
    p1 = connector.build_query_params(FIELDS, None, 100)
    p2 = connector.build_query_params(FIELDS, datetime(2026, 1, 1, tzinfo=timezone.utc), 100)
    assert p1["sysparm_display_value"] == "all"
    assert p2["sysparm_display_value"] == "all"


def test_query_carries_offset_and_limit():
    params = connector.build_query_params(FIELDS, None, 100, offset=200)
    assert params["sysparm_limit"] == 100
    assert params["sysparm_offset"] == 200
    assert params["sysparm_fields"] == FIELDS


# --- fetch_workorders -------------------------------------------------------
def _mock_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_increments_offset_across_pages():
    """Offset advances by page_size on each subsequent page."""
    full_page = {"result": [{"sys_id": str(i)} for i in range(2)]}   # len == page_size (2)
    last_page = {"result": [{"sys_id": "x"}]}                        # len < page_size
    with mock.patch.object(connector, "requests") as m:
        m.get.side_effect = [_mock_response(full_page), _mock_response(last_page)]
        rows = connector.fetch_workorders("tok", "https://x.service-now.com",
                                          "wm_order", FIELDS, None, page_size=2)
        assert len(rows) == 3
        assert m.get.call_count == 2
        offsets = [call.kwargs["params"]["sysparm_offset"] for call in m.get.call_args_list]
        assert offsets == [0, 2]


def test_fetch_stops_when_result_less_than_page_size():
    """A single short page ends pagination immediately (one call)."""
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"result": [{"sys_id": "1"}]})
        rows = connector.fetch_workorders("tok", "https://x.service-now.com",
                                          "wm_order", FIELDS, None, page_size=100)
        assert len(rows) == 1
        assert m.get.call_count == 1


def test_fetch_basic_auth_header_passed_through():
    """A 'Basic ...' token is sent as-is; an OAuth token is sent as Bearer."""
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"result": []})
        connector.fetch_workorders("Basic abc==", "https://x.service-now.com",
                                   "wm_order", FIELDS, None, page_size=100)
        assert m.get.call_args.kwargs["headers"]["Authorization"] == "Basic abc=="
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"result": []})
        connector.fetch_workorders("oauthtoken", "https://x.service-now.com",
                                   "wm_order", FIELDS, None, page_size=100)
        assert m.get.call_args.kwargs["headers"]["Authorization"] == "Bearer oauthtoken"


# --- sync state -------------------------------------------------------------
def test_get_last_sync_time_none_when_no_file(tmp_path):
    assert connector.get_last_sync_time(str(tmp_path / "nope.json")) is None


def test_get_last_sync_time_reads_datetime(tmp_path):
    state = str(tmp_path / "state.json")
    with open(state, "w") as f:
        json.dump({"last_sync": "2026-02-01T00:00:00+00:00"}, f)
    assert connector.get_last_sync_time(state) == datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_save_sync_time_round_trips(tmp_path):
    state = str(tmp_path / "state.json")
    ts = datetime(2026, 3, 15, 9, 30, tzinfo=timezone.utc)
    connector.save_sync_time(ts, state)
    assert connector.get_last_sync_time(state) == ts


# --- upsert_workorder -------------------------------------------------------
def test_upsert_calls_shared_with_alternate_key():
    """Upsert routes through the shared utility, PATCHing the fma_externalsourceid key."""
    record = {"fma_externalsourceid": "sysid-1", "fma_workordernumber": "WO1", "fma_client": "c"}
    with mock.patch.object(dataverse_upsert, "requests") as m:
        resp = mock.Mock()
        resp.status_code = 204
        resp.raise_for_status.return_value = None
        m.patch.return_value = resp
        result = connector.upsert_workorder(record, "https://target", "dv-tok")
        assert m.patch.called
        assert not m.post.called
        url = m.patch.call_args[0][0]
        assert "fma_workorders(fma_externalsourceid='sysid-1')" in url
        sent = json.loads(m.patch.call_args[1]["data"])
        assert "fma_client" not in sent
        assert result["key"] == "sysid-1"
