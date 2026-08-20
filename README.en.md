# eaa-active-users

[日本語版 README](README.md)

List users who accessed [Akamai Enterprise Application Access (EAA)](https://techdocs.akamai.com/eaa/docs/welcome-guide) applications within a given time window — for access reviews, license cleanups, and periodic user inventories. The time window is split automatically so that the API's per-call record cap cannot silently drop records (see [Notes on API behavior](#notes-on-api-behavior)).

> **Disclaimer**: This is a personal project. It is not affiliated with, endorsed by, or supported by Akamai Technologies. Provided as-is, without warranty of any kind, on a best-effort basis (no SLA).

## Install

Requires Python 3.10+. No clone needed:

```console
pipx install git+https://github.com/isss802/eaa-active-users
# or, one-off run without installing:
uvx --from git+https://github.com/isss802/eaa-active-users eaa-active-users --help
```

(Not published to PyPI yet — registry-name installs like `pipx install eaa-active-users` do not work.)

## Credentials

Create an `{OPEN}` API client in [Akamai Control Center](https://control.akamai.com/) (Identity and Access Management) with READ-WRITE access to the **Enterprise Application Access** API, then put the credentials in `~/.edgerc` together with your EAA contract ID:

```ini
[default]
host = akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx.luna.akamaiapis.net
client_token = akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx
client_secret = xxxx
access_token = akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx
contract_id = A-XXXXXXX
```

`contract_id` is shown in Enterprise Center, or can be listed via the [Contracts API](https://techdocs.akamai.com/eaa-api/reference/get-contracts).

## Usage

```console
# Users active in the last 90 days (default), as CSV on stdout
eaa-active-users

# Last 30 days, JSON, from a specific .edgerc section
eaa-active-users --days 30 --format json --section my-tenant

# Explicit window, filtered to one application/IdP, saved to a file
eaa-active-users --start 2026-05-01T00:00:00Z --end 2026-08-01T00:00:00Z \
    --app login.example.com -o active-users.csv
```

Output (CSV):

```csv
userid,access_count,first_access,last_access
alice@example.com,128,2026-05-03T10:00:00Z,2026-07-30T15:30:00Z
bob,4,2026-06-15T08:00:00Z,2026-06-18T12:00:00Z
```

Columns:

- `userid` — the API's `uid` field. Usually the user's email address, but non-email values (e.g. Cloud Directory usernames) also occur.
- `access_count` — number of access-log events recorded for the user within the window (activity volume).
- `first_access` / `last_access` — first and last access timestamps **within the requested window**, in ISO 8601. UTC (`Z` suffix) by default; with `--tz Asia/Tokyo` timestamps are rendered in that timezone with an offset (e.g. `+09:00`).
- Unauthenticated hits (`anon-user`: internet scans, health checks) are **excluded by default**; the excluded count is printed to stderr. Use `--include-anonymous` to keep them.

Exit codes: `0` success · `1` usage/credentials error · `2` API error before any data · `3` **completed but INCOMPLETE** (see below).

## Unused-user review (`--report unused`)

The inverse report for license cleanups: enumerates **every** directory in the tenant (Cloud Directory, Active Directory, LDAP), fetches all registered users, and diffs them against the active list.

```console
eaa-active-users --report unused --days 90 -o unused-review.csv
```

Output (CSV):

```csv
directory,username,email,display_name,created_at,verdict,match_confidence,matched_userid,access_count,first_access,last_access
Corp-AD,csuzuki,csuzuki@example.co.jp,Chika Suzuki,2023-04-01T09:00:00,unused_candidate,,,,,
Corp-AD,tsato,tsato@example.co.jp,Taro Sato,2022-10-15T09:00:00,needs_review,weak:email-localpart,tsato@example.com,12,2026-06-01T01:00:00Z,2026-08-15T09:00:00Z
Cloud Directory,alice@example.com,alice@example.com,Alice,2021-01-20T09:00:00,active,exact:username,alice@example.com,128,2026-05-22T00:30:00Z,2026-08-18T08:45:00Z
,,,,,active_unmatched,,olduser@example.com,3,2026-05-25T02:00:00Z,2026-05-26T00:00:00Z
```

Each row carries a `verdict`: `unused_candidate` (no EAA app-access record in the window — a candidate, **not proof of non-use**), `needs_review` (weak match only: username equals the local part of an active email uid — decide manually), `active` (exact field match, with the matching field in `match_confidence`), or `active_unmatched` (an active uid that matched no directory user — catches deleted users and spelling drift). Matching is case-insensitive over `username`, `email`, and AD-normalized attributes (`user.email`, `user.userPrincipleName`, `user.samAccountName`, …).

> **Do not deactivate `unused_candidate` rows mechanically.** Access events counted by this report include IdP (login portal) sign-ins as well as access to web, custom-domain, and tunnel-type client-access applications; `unused_candidate` means none of these were recorded in the window. Identity matching is not perfect — review `needs_review` (weak-match rows) and `active_unmatched` (active uids found in no directory) before acting.

## Limitations

- **The per-call record cap is undocumented behavior.** The default assumption (`--cap 250`) matches the documented `limit` maximum and is verified at runtime, but Akamai may change this behavior at any time.
- If more than *cap* records fall within a single minute, records beyond the cap in that minute cannot be retrieved through this endpoint. The tool warns and exits with code `3` instead of pretending completeness.
- Exhaustive coverage of application types recorded by this endpoint is not documented officially.
- EAA log retention is 365 days; windows older than that return nothing.
- The output contains personal data (usernames). Handle result files accordingly.
- API rate limit is 25 requests/minute; large tenants with long windows take time (the tool paces itself at ~24 requests/minute).

## Notes on API behavior

- `GET /crux/v1/mgmt-pop/application-reports/ops/query` caps the number of records returned per call. The [API reference](https://techdocs.akamai.com/eaa-api/reference/get-application-reports) documents a `limit` maximum of 250; an effective cap of 500 has been observed (as of August 2026). No error or truncation marker is returned when the cap is hit.
- This tool handles the cap by recursive window splitting with a one-time runtime cap verification, reports unreachable data via exit code `3`, and retries on 429/network errors.
- The official [cli-eaa](https://github.com/akamai/cli-eaa) `report last_access` (v0.7.x) subdivides the time range only when a response reaches 5,000 records, and is therefore affected by this cap.

## Development

```console
uv sync           # or: pip install -e . --group dev
pytest
ruff check .
```

## Acknowledgments

The window-splitting approach was inspired by reading the [cli-eaa](https://github.com/akamai/cli-eaa) source (Apache-2.0). This project contains no code copied from cli-eaa.

## Maintenance policy

Best-effort, no SLA. If the underlying API behavior is fixed/documented upstream (or cli-eaa's `report last_access` handles the cap correctly), this repository will be deprecated and archived in favor of the official tooling.

## License

[MIT](LICENSE)
