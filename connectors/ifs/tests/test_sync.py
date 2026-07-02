"""Unit tests for IFS sync orchestration — no real API calls."""

import os
import sys
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402


SELECT = "WoNo,TaskSeq,ObjState,PlanningDate,FinishDate,RealTimeSta,RealTimeFinish,RowVersion"
BASE_URL = "https://x.ifs.cloud/main/ifsapp/data"


# --- build_odata_query ------------------------------------------------------
def test_query_uses_rowversion_filter_when_last_sync_provided():
    """Incremental query filters on RowVersion."""
    last_sync = datetime(2026, 1, 1, tzinfo=timezone.utc)
    url = connector.build_odata_query(BASE_URL, "WorkTask", SELECT, last_sync, 100, 0)
    assert "$filter=RowVersion gt " in url
    assert "SnapShotCreatedDate" not in url


def test_query_uses_snapshot_filter_when_last_sync_none():
    """Initial load filters on SnapShotCreatedDate."""
    url = connector.build_odata_query(BASE_URL, "WorkTask", SELECT, None, 100, 0)
    assert "$filter=SnapShotCreatedDate gt " in url
    assert "RowVersion gt" not in url


def test_query_always_orders_by_rowversion_asc():
    url_inc = connector.build_odata_query(BASE_URL, "WorkTask", SELECT,
                                          datetime(2026, 1, 1, tzinfo=timezone.utc), 100, 0)
    url_init = connector.build_odata_query(BASE_URL, "WorkTask", SELECT, None, 100, 0)
    assert "$orderby=RowVersion asc" in url_inc
    assert "$orderby=RowVersion asc" in url_init


def test_query_sets_top_and_skip():
    url = connector.build_odata_query(BASE_URL, "WorkTask", SELECT, None, 100, 200)
    assert "$top=100" in url
    assert "$skip=200" in url


def test_query_includes_select_and_entity():
    url = connector.build_odata_query(BASE_URL, "WorkTask", SELECT, None, 100, 0)
    assert url.startswith(f"{BASE_URL}/WorkTask?")
    assert f"$select={SELECT}" in url


# --- fetch_workorders -------------------------------------------------------
def _mock_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_increments_skip_across_pages():
    """$skip advances by page_size on each subsequent page."""
    full_page = {"value": [{"WoNo": i} for i in range(2)]}   # len == page_size (2)
    last_page = {"value": [{"WoNo": 99}]}                    # len < page_size
    with mock.patch.object(connector, "requests") as m:
        m.get.side_effect = [_mock_response(full_page), _mock_response(last_page)]
        rows = connector.fetch_workorders({"Authorization": "Bearer t"}, BASE_URL,
                                          "WorkTask", SELECT, None, page_size=2)
        assert len(rows) == 3
        assert m.get.call_count == 2
        urls = [call.args[0] for call in m.get.call_args_list]
        assert "$skip=0" in urls[0]
        assert "$skip=2" in urls[1]


def test_fetch_stops_when_result_less_than_page_size():
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"value": [{"WoNo": 1}]})
        rows = connector.fetch_workorders({"Authorization": "Bearer t"}, BASE_URL,
                                          "WorkTask", SELECT, None, page_size=100)
        assert len(rows) == 1
        assert m.get.call_count == 1


# --- get_auth_headers -------------------------------------------------------
def test_auth_headers_oauth_when_cloud():
    """deploymentType 'cloud' produces a Bearer OAuth header."""
    config = {
        "deploymentType": "cloud",
        "auth": {"tokenUrl": "https://x/idp/connect/token", "clientId": "cid",
                 "clientSecret": "sec", "scope": "ifsapp offline_access"},
    }
    with mock.patch.object(connector, "get_access_token", return_value="tok123") as gt:
        headers = connector.get_auth_headers(config)
        assert headers == {"Authorization": "Bearer tok123"}
        gt.assert_called_once()


def test_auth_headers_basic_when_onpremise():
    """deploymentType 'onpremise' produces a Basic auth header, no token call."""
    config = {
        "deploymentType": "onpremise",
        "auth": {"basicAuth": {"username": "u", "password": "p"}},
    }
    with mock.patch.object(connector, "get_access_token") as gt:
        headers = connector.get_auth_headers(config)
        assert headers["Authorization"].startswith("Basic ")
        gt.assert_not_called()


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
