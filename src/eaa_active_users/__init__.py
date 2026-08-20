"""eaa-active-users — list users who accessed Akamai EAA in a given time window.

Queries the EAA {OPEN} API endpoint
``GET /crux/v1/mgmt-pop/application-reports/ops/query`` and aggregates, per
user, the number of access events and the first/last access timestamps.

Why this tool exists: the API silently caps the number of records returned by
a single call (250 according to the API reference; 500 observed in practice as
of 2026-08). Naive clients that request a long time range therefore miss
users without any error being raised. This tool splits the requested time
window recursively until every sub-window fits under the cap, and loudly
reports when completeness cannot be guaranteed.

This is a personal project. It is not affiliated with, endorsed by, or
supported by Akamai Technologies.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import io
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from akamai.edgegrid import EdgeGridAuth

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

__version__ = "0.3.0"

#: Records-per-call cap the splitter assumes. The API reference documents a
#: maximum ``limit`` of 250; the effective cap observed in practice is 500.
#: The conservative (lower) value is the safe default: assuming a cap that is
#: too low only costs extra API calls, assuming one too high loses data.
DEFAULT_CAP = 250

#: Do not split windows smaller than this (milliseconds).
MIN_WINDOW_MS = 60_000

MAX_RATE_LIMIT_RETRIES = 10
MAX_NETWORK_RETRIES = 5
RATE_LIMIT_WAIT_S = 60
NETWORK_RETRY_WAIT_S = 30
#: EAA {OPEN} API rate limit is 25 requests/minute; stay under it.
INTER_CALL_DELAY_S = 2.5

ANON_USER = "anon-user"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_API_ERROR = 2
EXIT_INCOMPLETE = 3


class FatalApiError(Exception):
    """Unrecoverable API failure (non-200 response or retries exhausted)."""


def log(msg: str, *, verbose_only: bool = False, verbose: bool = False) -> None:
    if verbose_only and not verbose:
        return
    print(msg, file=sys.stderr)


class ApiClient:
    """Thin wrapper around the application-reports query endpoint."""

    def __init__(self, host: str, auth: EdgeGridAuth, contract_id: str,
                 tz: str, app: str | None, cap: int, verbose: bool = False):
        self.host = host
        self.session = requests.Session()
        self.session.auth = auth
        self.contract_id = contract_id
        self.tz = tz
        self.app = app
        # Ask for more than the assumed cap so a higher effective cap still
        # returns as much as the server allows.
        self.limit = max(5000, cap)
        self.verbose = verbose
        self.api_calls = 0

    def get_json(self, path: str, params: dict) -> dict:
        url = f"https://{self.host}/crux/v1/{path}"
        rate_limit_retries = 0
        network_retries = 0
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout) as exc:
                network_retries += 1
                if network_retries > MAX_NETWORK_RETRIES:
                    raise FatalApiError(f"network error, retries exhausted: {exc}")
                log(f"network error ({exc.__class__.__name__}), "
                    f"retry {network_retries}/{MAX_NETWORK_RETRIES} "
                    f"in {NETWORK_RETRY_WAIT_S}s...")
                time.sleep(NETWORK_RETRY_WAIT_S)
                continue
            self.api_calls += 1
            if resp.status_code == 429:
                rate_limit_retries += 1
                if rate_limit_retries > MAX_RATE_LIMIT_RETRIES:
                    raise FatalApiError("rate limited (HTTP 429), retries exhausted")
                log(f"rate limited (HTTP 429), waiting {RATE_LIMIT_WAIT_S}s "
                    f"({rate_limit_retries}/{MAX_RATE_LIMIT_RETRIES})...")
                time.sleep(RATE_LIMIT_WAIT_S)
                continue
            if resp.status_code != 200:
                raise FatalApiError(
                    f"HTTP {resp.status_code} for {path}: {resp.text[:200]}")
            return resp.json()

    def query(self, start_ms: int, end_ms: int) -> list[dict]:
        params = {
            "start": start_ms,
            "end": end_ms,
            "tz": self.tz,
            "limit": self.limit,
            "contractId": self.contract_id,
        }
        if self.app:
            params["app"] = self.app
        return self.get_json("mgmt-pop/application-reports/ops/query",
                             params).get("data", [])

    def paged_objects(self, path: str, delay_s: float = INTER_CALL_DELAY_S):
        """Iterate a collection endpoint following meta.next pagination."""
        params = {"contractId": self.contract_id, "limit": 100}
        while True:
            body = self.get_json(path, params)
            yield from body.get("objects", [])
            nxt = (body.get("meta") or {}).get("next")
            if not nxt:
                return
            params = dict(urllib.parse.parse_qsl(nxt))
            params.setdefault("contractId", self.contract_id)
            if delay_s:
                time.sleep(delay_s)

    def directories(self) -> list[dict]:
        return list(self.paged_objects("mgmt-pop/directories"))

    def directory_users(self, directory_id: str) -> list[dict]:
        return list(self.paged_objects(
            f"mgmt-pop/directories/{directory_id}/users"))


#: A final window returning at least this many records triggers a one-time
#: verification split (guards against an effective cap lower than assumed).
VERIFY_THRESHOLD = 100


def collect(query, start_ms: int, end_ms: int, cap: int,
            *, delay_s: float = INTER_CALL_DELAY_S,
            verbose: bool = False) -> tuple[list[dict], bool]:
    """Fetch all records in [start_ms, end_ms], splitting windows that hit the cap.

    Adjacent sub-windows deliberately share their boundary millisecond (the API's
    boundary inclusivity is not documented); duplicates from that overlap are
    removed by exact record identity.

    Because the effective server-side cap is undocumented and may be lower than
    assumed, the first window that would be accepted with ``VERIFY_THRESHOLD``
    or more records is verified once by querying its two halves: if the halves
    contain records the parent response did not, the parent was silently capped
    — the assumed cap is lowered to the observed value and scanning continues.

    Returns ``(records, complete)``. ``complete`` is False when a window at the
    minimum size still returned ``cap`` or more records (data beyond the cap in
    that window is unavailable) or when a fatal API error interrupted the scan.
    """
    records: list[dict] = []
    seen: set[str] = set()
    complete = True
    cap_verified = False
    stack = [(start_ms, end_ms)]

    def accept(data: list[dict]) -> None:
        for rec in data:
            key = json.dumps(rec, sort_keys=True)
            if key not in seen:
                seen.add(key)
                records.append(rec)

    while stack:
        s, e = stack.pop()
        try:
            data = query(s, e)

            if len(data) < cap and not cap_verified \
                    and len(data) >= VERIFY_THRESHOLD and (e - s) > MIN_WINDOW_MS:
                # One-time check that the server did not silently cap this
                # response below our assumed cap.
                mid = (s + e) // 2
                halves = query(s, mid) + query(mid, e)
                parent_keys = {json.dumps(r, sort_keys=True) for r in data}
                extra = [r for r in halves
                         if json.dumps(r, sort_keys=True) not in parent_keys]
                cap_verified = True
                if extra:
                    observed = len(data)
                    log(f"WARNING: the server returned {observed} records for a "
                        f"window but its halves contain {len(extra)} more — the "
                        f"effective per-call cap is ~{observed}, lower than the "
                        f"assumed {cap}. Lowering the cap and continuing.")
                    cap = observed
                    stack.append((s, mid))
                    stack.append((mid, e))
                    continue
        except FatalApiError as exc:
            log(f"WARNING: aborting scan, results will be incomplete: {exc}")
            complete = False
            break

        if len(data) >= cap and (e - s) > MIN_WINDOW_MS:
            mid = (s + e) // 2
            stack.append((s, mid))
            stack.append((mid, e))
            log(f"window {s}-{e}: {len(data)} records (>= cap {cap}) -> split",
                verbose_only=True, verbose=verbose)
        else:
            if len(data) >= cap:
                complete = False
                log(f"WARNING: window {s}-{e} is at the minimum size "
                    f"({MIN_WINDOW_MS} ms) but still returned {len(data)} "
                    f"records (>= cap {cap}); records beyond the cap in this "
                    f"window CANNOT be retrieved. Results are INCOMPLETE.")
            accept(data)
            log(f"window {s}-{e}: {len(data)} records (final)",
                verbose_only=True, verbose=verbose)
        if stack and delay_s:
            time.sleep(delay_s)

    return records, complete


def aggregate(records: list[dict]) -> dict[str, dict]:
    """Aggregate per-user record count and first/last access timestamps."""
    users: dict[str, dict] = {}
    for rec in records:
        uid, ts = rec.get("uid"), rec.get("ts")
        if not uid or ts is None:
            continue
        u = users.setdefault(uid, {"records": 0, "first": ts, "last": ts})
        u["records"] += 1
        u["first"] = min(u["first"], ts)
        u["last"] = max(u["last"], ts)
    return users


def iso(ms: int, tzinfo=timezone.utc) -> str:
    """Render epoch ms as ISO 8601 in the given timezone; UTC uses the Z suffix."""
    s = datetime.fromtimestamp(ms / 1000, tz=tzinfo).isoformat(timespec="seconds")
    return s.replace("+00:00", "Z")


def render_csv(users: dict[str, dict], tzinfo=timezone.utc) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["userid", "access_count", "first_access", "last_access"])
    for uid in sorted(users, key=lambda u: users[u]["last"], reverse=True):
        u = users[uid]
        w.writerow([uid, u["records"], iso(u["first"], tzinfo), iso(u["last"], tzinfo)])
    return buf.getvalue()


def render_json(users: dict[str, dict], *, start_ms: int, end_ms: int,
                complete: bool, anonymous_excluded: int,
                tzinfo=timezone.utc) -> str:
    return json.dumps({
        "start": iso(start_ms, tzinfo),
        "end": iso(end_ms, tzinfo),
        "complete": complete,
        "anonymous_records_excluded": anonymous_excluded,
        "users": [
            {"userid": uid, "access_count": u["records"],
             "first_access": iso(u["first"], tzinfo),
             "last_access": iso(u["last"], tzinfo)}
            for uid, u in sorted(users.items(),
                                 key=lambda kv: kv[1]["last"], reverse=True)
        ],
    }, indent=2)


#: Directory-user fields whose value may equal an active `uid`, in match order.
DIR_MATCH_FIELDS = ("username", "email")
NORM_MATCH_FIELDS = ("user.email", "user.userName", "user.userPrincipleName",
                     "user.samAccountName", "eaa.userName")


def match_candidates(dir_user: dict):
    """Yield (field, value) pairs usable to match a directory user to an active uid."""
    for f in DIR_MATCH_FIELDS:
        v = dir_user.get(f)
        if v:
            yield f, str(v)
    norm = dir_user.get("normalized_attributes") or {}
    for f in NORM_MATCH_FIELDS:
        v = norm.get(f)
        if v:
            yield f, str(v)


def classify_users(dir_entries: list[tuple[str, dict]],
                   active_users: dict[str, dict]) -> tuple[list[dict], set[str]]:
    """Match directory users against active uids.

    dir_entries: (directory_name, user_object) pairs.
    Returns (rows, matched_uids). Each row carries a verdict:
      - active           — a field matches an active uid exactly (case-insensitive)
      - needs_review     — only a weak match (username == local part of an active
                           email uid); a human should decide
      - unused_candidate — no match; candidate for cleanup, NOT proof of non-use
    """
    active_lc = {uid.lower(): uid for uid in active_users}
    email_localparts = {uid.split("@", 1)[0].lower(): uid
                        for uid in active_users if "@" in uid}
    rows: list[dict] = []
    matched: set[str] = set()
    for dname, du in dir_entries:
        verdict, conf, muid = "unused_candidate", "", ""
        for field, val in match_candidates(du):
            hit = active_lc.get(val.lower())
            if hit:
                verdict, conf, muid = "active", f"exact:{field}", hit
                matched.add(hit)
                break
        if verdict == "unused_candidate":
            uname = str(du.get("username") or "").lower()
            hit = email_localparts.get(uname) if uname else None
            if hit:
                verdict, conf, muid = "needs_review", "weak:email-localpart", hit
                matched.add(hit)
        rows.append({
            "directory": dname,
            "username": du.get("username") or "",
            "email": du.get("email")
                     or (du.get("normalized_attributes") or {}).get("user.email")
                     or "",
            "display_name": du.get("display_name") or "",
            "created_at": du.get("created_at") or "",
            "verdict": verdict,
            "match_confidence": conf,
            "matched_userid": muid,
        })
    return rows, matched


VERDICT_ORDER = {"unused_candidate": 0, "needs_review": 1,
                 "active": 2, "active_unmatched": 3}


UNUSED_COLUMNS = ["directory", "username", "email", "display_name", "created_at",
                  "verdict", "match_confidence", "matched_userid",
                  "access_count", "first_access", "last_access"]


def build_unused_rows(rows: list[dict], active_users: dict[str, dict],
                      matched: set[str], tzinfo=timezone.utc) -> list[dict]:
    """Enrich diff rows with access stats. Unmatched active uids are appended
    as active_unmatched rows so no active identity silently disappears."""
    all_rows = [dict(r) for r in rows]
    for uid in sorted(set(active_users) - matched):
        all_rows.append({
            "directory": "", "username": "", "email": "", "display_name": "",
            "created_at": "", "verdict": "active_unmatched",
            "match_confidence": "", "matched_userid": uid,
        })
    for r in all_rows:
        u = active_users.get(r["matched_userid"])
        r["access_count"] = u["records"] if u else ""
        r["first_access"] = iso(u["first"], tzinfo) if u else ""
        r["last_access"] = iso(u["last"], tzinfo) if u else ""
    all_rows.sort(key=lambda r: (VERDICT_ORDER.get(r["verdict"], 9),
                                 r["directory"], r["username"]))
    return all_rows


def render_unused_csv(all_rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(UNUSED_COLUMNS)
    for r in all_rows:
        w.writerow([r[c] for c in UNUSED_COLUMNS])
    return buf.getvalue()


def render_unused_json(all_rows: list[dict], *, start_ms: int, end_ms: int,
                       complete: bool, tzinfo=timezone.utc) -> str:
    return json.dumps({
        "start": iso(start_ms, tzinfo),
        "end": iso(end_ms, tzinfo),
        "complete": complete,
        "rows": [{c: r[c] for c in UNUSED_COLUMNS} for r in all_rows],
    }, indent=2)


def parse_when(value: str) -> int:
    """Parse an epoch-seconds integer or an ISO 8601 datetime into epoch ms."""
    try:
        return int(value) * 1000
    except ValueError:
        pass
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eaa-active-users",
        description="List users who accessed Akamai EAA in a given time window.")
    p.add_argument("--report", choices=["active", "unused"], default="active",
                   help="active: users who accessed EAA in the window (default). "
                        "unused: diff every directory user against the active "
                        "list and report unused candidates")
    p.add_argument("--days", type=float, default=90,
                   help="look-back window in days (default: 90); "
                        "ignored when --start is given")
    p.add_argument("--start", type=parse_when,
                   help="window start (epoch seconds or ISO 8601)")
    p.add_argument("--end", type=parse_when,
                   help="window end (epoch seconds or ISO 8601, default: now)")
    p.add_argument("--edgerc", default=str(Path.home() / ".edgerc"),
                   help="path to the .edgerc credentials file (default: ~/.edgerc)")
    p.add_argument("--section", default="default",
                   help=".edgerc section to use (default: default)")
    p.add_argument("--app", help="filter by application/IdP hostname or UUID")
    p.add_argument("--tz", default="UTC",
                   help="tz database timezone (e.g. Asia/Tokyo). Used both for "
                        "the API query and for rendering output timestamps "
                        "(default: UTC)")
    p.add_argument("--cap", type=int, default=DEFAULT_CAP,
                   help="records-per-call cap the splitter assumes "
                        f"(default: {DEFAULT_CAP}; lower is safer, higher is faster)")
    p.add_argument("--format", choices=["csv", "json"], default="csv",
                   help="output format (default: csv)")
    p.add_argument("--output", "-o", help="write output to a file instead of stdout")
    p.add_argument("--include-anonymous", action="store_true",
                   help=f"include unauthenticated access ('{ANON_USER}') in the "
                        f"output (excluded by default)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="log every window to stderr")
    p.add_argument("--version", action="version", version=__version__)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.tz == "UTC":
        tzinfo = timezone.utc
    else:
        try:
            tzinfo = ZoneInfo(args.tz)
        except (KeyError, ValueError, OSError):
            print(f"error: unknown timezone: {args.tz}", file=sys.stderr)
            return EXIT_USAGE

    now_ms = int(time.time() * 1000)
    end_ms = args.end if args.end is not None else now_ms
    start_ms = args.start if args.start is not None \
        else end_ms - int(args.days * 86_400_000)
    if start_ms >= end_ms:
        print("error: start must be before end", file=sys.stderr)
        return EXIT_USAGE

    cfg = configparser.ConfigParser()
    if not cfg.read(args.edgerc):
        print(f"error: cannot read {args.edgerc}", file=sys.stderr)
        return EXIT_USAGE
    if args.section not in cfg:
        print(f"error: section [{args.section}] not found in {args.edgerc}",
              file=sys.stderr)
        return EXIT_USAGE
    sec = cfg[args.section]
    missing = [k for k in
               ("host", "client_token", "client_secret", "access_token",
                "contract_id") if k not in sec]
    if missing:
        print(f"error: [{args.section}] in {args.edgerc} is missing: "
              f"{', '.join(missing)}. The EAA API needs {{OPEN}} API "
              f"credentials plus contract_id.", file=sys.stderr)
        return EXIT_USAGE

    client = ApiClient(
        host=sec["host"],
        auth=EdgeGridAuth(client_token=sec["client_token"],
                          client_secret=sec["client_secret"],
                          access_token=sec["access_token"]),
        contract_id=sec["contract_id"],
        tz=args.tz, app=args.app, cap=args.cap, verbose=args.verbose)

    log(f"scanning {iso(start_ms, tzinfo)} .. {iso(end_ms, tzinfo)} "
        f"(cap {args.cap}, section [{args.section}])")
    try:
        records, complete = collect(client.query, start_ms, end_ms, args.cap,
                                    verbose=args.verbose)
    except FatalApiError as exc:  # first call failed, nothing collected
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_API_ERROR

    anonymous_excluded = 0
    if not args.include_anonymous or args.report == "unused":
        before = len(records)
        records = [r for r in records if r.get("uid") != ANON_USER]
        anonymous_excluded = before - len(records)
        if anonymous_excluded and args.report == "active":
            log(f"note: excluded {anonymous_excluded} unauthenticated "
                f"('{ANON_USER}') records; use --include-anonymous to keep them")

    users = aggregate(records)
    log(f"{len(records)} records, {len(users)} unique users, "
        f"{client.api_calls} API calls"
        + ("" if complete else " — INCOMPLETE"))

    if args.report == "unused":
        try:
            dir_entries: list[tuple[str, dict]] = []
            for d in client.directories():
                dname = d.get("name") or d.get("uuid_url") or "?"
                dir_users = client.directory_users(d.get("uuid_url"))
                log(f"directory '{dname}': {len(dir_users)} users")
                dir_entries.extend((dname, du) for du in dir_users)
        except FatalApiError as exc:
            print(f"error: fetching directory users failed: {exc}",
                  file=sys.stderr)
            return EXIT_API_ERROR
        rows, matched = classify_users(dir_entries, users)
        all_rows = build_unused_rows(rows, users, matched, tzinfo)
        counts = {}
        for r in all_rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        log("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            + ("" if complete else " — INCOMPLETE: unused verdicts unreliable"))
        log("note: 'unused_candidate' means no access via EAA app logs in the "
            "window — NOT proof of non-use. Review needs_review rows and see "
            "the README's limitations before acting.")
        out = render_unused_csv(all_rows) if args.format == "csv" else \
            render_unused_json(all_rows, start_ms=start_ms, end_ms=end_ms,
                               complete=complete, tzinfo=tzinfo)
    else:
        out = render_csv(users, tzinfo) if args.format == "csv" else render_json(
            users, start_ms=start_ms, end_ms=end_ms, complete=complete,
            anonymous_excluded=anonymous_excluded, tzinfo=tzinfo)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
    else:
        sys.stdout.write(out)

    return EXIT_OK if complete else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
