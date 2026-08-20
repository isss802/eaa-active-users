"""Unit tests for the window-splitting collector and aggregation logic."""

import csv
import io

from eaa_active_users import (
    MIN_WINDOW_MS,
    FatalApiError,
    aggregate,
    build_unused_rows,
    classify_users,
    collect,
    parse_when,
    render_csv,
    render_unused_csv,
)


def rec(uid, ts, **extra):
    return {"uid": uid, "ts": ts, **extra}


class FakeApi:
    """Serves records from a fixed event list, honoring a server-side cap."""

    def __init__(self, events, server_cap):
        self.events = sorted(events, key=lambda r: r["ts"])
        self.server_cap = server_cap
        self.calls = 0

    def query(self, start_ms, end_ms):
        self.calls += 1
        hits = [r for r in self.events if start_ms <= r["ts"] <= end_ms]
        return hits[-self.server_cap:]  # newest wins, like a truncating server


def test_small_window_needs_no_split():
    api = FakeApi([rec("a", 1000), rec("b", 2000)], server_cap=250)
    records, complete = collect(api.query, 0, 10_000_000, cap=250, delay_s=0)
    assert complete
    assert api.calls == 1
    assert {r["uid"] for r in records} == {"a", "b"}


def test_split_recovers_all_records_beyond_cap():
    # 1000 events spread over ~12 days; server returns at most 250 per call.
    events = [rec(f"u{i % 20}", 1_000_000 + i * 1_000_000) for i in range(1000)]
    api = FakeApi(events, server_cap=250)
    records, complete = collect(api.query, 0, 1_100_000_000, cap=250, delay_s=0)
    assert complete
    assert len(records) == 1000
    assert len({r["uid"] for r in records}) == 20


def test_min_window_truncation_is_flagged_incomplete():
    # 300 events in the same minute: cannot be split below MIN_WINDOW_MS.
    events = [rec(f"u{i}", 5_000 + i * 100) for i in range(300)]
    api = FakeApi(events, server_cap=250)
    records, complete = collect(api.query, 0, MIN_WINDOW_MS, cap=250, delay_s=0)
    assert not complete
    assert len(records) == 250  # got what the server allowed, but flagged


def test_boundary_records_are_not_double_counted():
    # Sub-windows share their boundary millisecond; a record exactly on the
    # split boundary must be counted once. Split of [0, 10_000_000] lands on
    # mid=5_000_000 where "edge" sits.
    events = [rec("x", 100), rec("edge", 5_000_000), rec("y", 9_000_000)]
    api = FakeApi(events, server_cap=2)  # force splits
    records, complete = collect(api.query, 0, 10_000_000, cap=2, delay_s=0)
    assert complete
    uids = [r["uid"] for r in records]
    assert uids.count("edge") == 1
    assert len(records) == 3


def test_fatal_error_mid_scan_returns_partial_and_incomplete():
    calls = {"n": 0}

    def seq(start_ms, end_ms):
        calls["n"] += 1
        if calls["n"] == 1:  # cap hit -> split into two children
            return [rec("a", start_ms + i) for i in range(10)]
        if calls["n"] == 2:  # first child succeeds
            return [rec("b", start_ms + 1), rec("c", start_ms + 2)]
        raise FatalApiError("boom")  # second child dies

    records, complete = collect(seq, 0, 10_000_000, cap=10, delay_s=0)
    assert not complete
    assert {r["uid"] for r in records} == {"b", "c"}  # partial data preserved


def test_aggregate_first_last_and_counts():
    users = aggregate([
        rec("a", 100), rec("a", 300), rec("a", 200),
        rec("b", 50),
        rec(None, 1), rec("c", None),  # ignored
    ])
    assert users["a"] == {"records": 3, "first": 100, "last": 300}
    assert users["b"] == {"records": 1, "first": 50, "last": 50}
    assert set(users) == {"a", "b"}


def test_csv_escapes_commas_in_userid():
    users = aggregate([rec('last, first "quoted"', 1000)])
    out = render_csv(users)
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[1][0] == 'last, first "quoted"'
    assert len(rows) == 2


def test_timestamps_render_in_requested_timezone():
    from zoneinfo import ZoneInfo

    users = aggregate([rec("a", 1_767_225_600_000)])  # 2026-01-01T00:00:00Z
    out_utc = render_csv(users)
    assert out_utc.splitlines()[0] == "userid,access_count,first_access,last_access"
    assert "2026-01-01T00:00:00Z" in out_utc
    out_jst = render_csv(users, ZoneInfo("Asia/Tokyo"))
    assert "2026-01-01T09:00:00+09:00" in out_jst


def test_classify_users_matching_tiers():
    active = aggregate([
        rec("alice@example.com", 1000),
        rec("abe", 2000),
        rec("ghost@x.com", 3000),
        rec("stray@nowhere.io", 4000),
    ])
    dir_entries = [
        # exact email match, case-insensitive
        ("CloudDir", {"username": "alice", "email": "Alice@Example.com"}),
        # exact username match; AD user matched via top-level username
        ("AD", {"username": "abe",
                "normalized_attributes": {"user.userPrincipleName": "abe@corp.local"}}),
        # weak: username equals local part of an active email uid
        ("AD", {"username": "ghost"}),
        # no match at all
        ("AD", {"username": "bob", "email": "bob@example.com"}),
    ]
    rows, matched = classify_users(dir_entries, active)
    v = {(r["directory"], r["username"]): r for r in rows}
    assert v[("CloudDir", "alice")]["verdict"] == "active"
    assert v[("CloudDir", "alice")]["match_confidence"] == "exact:email"
    assert v[("AD", "abe")]["verdict"] == "active"
    assert v[("AD", "ghost")]["verdict"] == "needs_review"
    assert v[("AD", "ghost")]["matched_userid"] == "ghost@x.com"
    assert v[("AD", "bob")]["verdict"] == "unused_candidate"
    assert matched == {"alice@example.com", "abe", "ghost@x.com"}

    all_rows = build_unused_rows(rows, active, matched)
    # the stray active uid must not disappear from the report
    stray = [r for r in all_rows if r["verdict"] == "active_unmatched"]
    assert len(stray) == 1 and stray[0]["matched_userid"] == "stray@nowhere.io"
    # unused candidates sort first, and matched rows carry access stats
    assert all_rows[0]["verdict"] == "unused_candidate"
    alice = next(r for r in all_rows if r["username"] == "alice")
    assert alice["access_count"] == 1 and alice["last_access"].endswith("Z")
    out = render_unused_csv(all_rows)
    assert out.splitlines()[0].startswith("directory,username,email")
    assert len(out.splitlines()) == 1 + 5


def test_matching_uses_normalized_attributes():
    active = aggregate([rec("TARO.YAMADA@corp.example.jp", 1000)])
    dir_entries = [("AD", {
        "username": "yamadataro",
        "normalized_attributes": {"user.userPrincipleName": "taro.yamada@corp.example.jp"},
    })]
    rows, _matched = classify_users(dir_entries, active)
    assert rows[0]["verdict"] == "active"
    assert rows[0]["match_confidence"] == "exact:user.userPrincipleName"


def test_parse_when_accepts_epoch_and_iso():
    assert parse_when("1700000000") == 1_700_000_000_000
    assert parse_when("2026-01-01T00:00:00+00:00") == 1_767_225_600_000
    # naive datetimes are treated as UTC
    assert parse_when("2026-01-01T00:00:00") == 1_767_225_600_000


def test_lower_effective_cap_is_detected_and_recovered(capsys):
    # Server caps at 250 but the splitter assumes 500. Without verification
    # this would silently return 250 of 3000 records; the one-time
    # verification split must detect the real cap and recover everything.
    events = [rec(f"u{i}", 1_000_000 + i * 10_000) for i in range(3000)]
    api = FakeApi(events, server_cap=250)
    records, complete = collect(api.query, 0, 40_000_000, cap=500, delay_s=0)
    assert complete
    assert len(records) == 3000
    err = capsys.readouterr().err
    assert "effective per-call cap is ~250" in err
