"""Unit tests for Salesforce sync orchestration — no real API calls."""

import os
import sys
import json
import re
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connector  # noqa: E402
from shared import dataverse_upsert  # noqa: E402


SOQL_BASE = (
    "SELECT Id, WorkOrderNumber, Status, StartDate, EndDate, ActualStartTime, "
    "ActualEndTime, Subject, AccountId, AssetId, OwnerId, Duration, IsClosed, "
    "SlaExitDate, TotalPrice FROM WorkOrder"
)


# --- build_soql_query -------------------------------------------------------
def test_soql_includes_filter_when_last_sync_provided():
    """A LastModifiedDate > filter is added when last_sync is provided."""
    last_sync = datetime(2026, 1, 1, tzinfo=timezone.utc)
    soql = connector.build_soql_query(SOQL_BASE, last_sync, 200)
    assert "WHERE LastModifiedDate > 2026-01-01T00:00:00Z" in soql
    assert soql.endswith("ORDER BY LastModifiedDate ASC LIMIT 200")


def test_soql_omits_filter_when_last_sync_none():
    """No WHERE clause when last_sync is None (full initial sync)."""
    soql = connector.build_soql_query(SOQL_BASE, None, 200)
    assert "WHERE" not in soql
    assert soql.endswith("ORDER BY LastModifiedDate ASC LIMIT 200")


def test_soql_datetime_format_is_iso_with_z():
    """The SOQL datetime literal is ISO-8601 with a trailing Z and no quotes."""
    last_sync = datetime(2026, 3, 5, 14, 30, 0, tzinfo=timezone.utc)
    soql = connector.build_soql_query(SOQL_BASE, last_sync, 100)
    m = re.search(r"LastModifiedDate > (\S+)", soql)
    assert m is not None
    literal = m.group(1)
    assert literal == "2026-03-05T14:30:00Z"
    assert "'" not in literal  # datetime literals are unquoted in SOQL


def test_soql_datetime_converts_nonutc_last_sync():
    """A non-UTC last_sync is converted to UTC in the SOQL literal."""
    from datetime import timedelta
    tz = timezone(timedelta(hours=5))
    last_sync = datetime(2026, 3, 5, 14, 30, 0, tzinfo=tz)  # 09:30 UTC
    soql = connector.build_soql_query(SOQL_BASE, last_sync, 100)
    assert "LastModifiedDate > 2026-03-05T09:30:00Z" in soql


# --- sync state -------------------------------------------------------------
def test_get_last_sync_time_none_when_no_file(tmp_path):
    assert connector.get_last_sync_time(str(tmp_path / "nope.json")) is None


def test_get_last_sync_time_reads_datetime(tmp_path):
    state = str(tmp_path / "state.json")
    with open(state, "w") as f:
        json.dump({"last_sync": "2026-02-01T00:00:00+00:00"}, f)
    assert connector.get_last_sync_time(state) == datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_save_sync_time_writes_iso(tmp_path):
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


def test_fetch_follows_next_records_url():
    """Pagination follows nextRecordsUrl until exhausted."""
    page1 = {
        "records": [{"Id": "1"}],
        "nextRecordsUrl": "/services/data/v59.0/query/01g-2000",
    }
    page2 = {"records": [{"Id": "2"}]}
    with mock.patch.object(connector, "requests") as m:
        m.get.side_effect = [_mock_response(page1), _mock_response(page2)]
        rows = connector.fetch_workorders("tok", "https://na1.salesforce.com", "SELECT ...", "v59.0")
        assert [r["Id"] for r in rows] == ["1", "2"]
        assert m.get.call_count == 2
        # Second call targets instance_url + nextRecordsUrl.
        second_url = m.get.call_args_list[1][0][0]
        assert second_url == "https://na1.salesforce.com/services/data/v59.0/query/01g-2000"


def test_fetch_first_call_sends_soql_param():
    """The first query call passes the SOQL as the q parameter."""
    with mock.patch.object(connector, "requests") as m:
        m.get.return_value = _mock_response({"records": []})
        connector.fetch_workorders("tok", "https://na1.salesforce.com", "SELECT X", "v59.0")
        _, kwargs = m.get.call_args
        assert kwargs["params"] == {"q": "SELECT X"}


# --- upsert_workorder -------------------------------------------------------
def test_upsert_uses_patch_not_post():
    """Upsert must PATCH the alternate key via the shared utility, never POST."""
    record = {"fma_externalsourceid": "5WO-1", "fma_workordernumber": "1", "fma_client": "c"}
    with mock.patch.object(dataverse_upsert, "requests") as m:
        resp = mock.Mock()
        resp.status_code = 204
        resp.raise_for_status.return_value = None
        m.patch.return_value = resp
        result = connector.upsert_workorder(record, "https://target", "dv-tok")
        assert m.patch.called
        assert not m.post.called
        url = m.patch.call_args[0][0]
        assert "fma_workorders(fma_externalsourceid='5WO-1')" in url
        sent = json.loads(m.patch.call_args[1]["data"])
        assert "fma_client" not in sent  # helper-only key stripped
        assert result["created"] is False
