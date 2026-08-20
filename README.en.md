# eaa-active-users

[日本語版 README](README.md)

List users who accessed [Akamai Enterprise Application Access (EAA)](https://techdocs.akamai.com/eaa/docs/welcome-guide) applications within a given time window — for access reviews, license cleanups, and periodic user inventories.

> **Disclaimer**: This is a personal project. It is not affiliated with, endorsed by, or supported by Akamai Technologies. Provided as-is, without warranty of any kind, on a best-effort basis (no SLA).

## Why

EAA keeps user access logs for 365 days, and the `{OPEN}` API endpoint
`GET /crux/v1/mgmt-pop/application-reports/ops/query` lets you query them. However, the API **silently caps the number of records returned per call** — the [API reference](https://techdocs.akamai.com/eaa-api/reference/get-application-reports) documents a `limit` maximum of 250, and an effective cap of 500 has been observed in practice (as of August 2026). No error or truncation marker is returned when the cap is hit.

Clients that query a long time range in one call therefore miss users without noticing. (The official [cli-eaa](https://github.com/akamai/cli-eaa) `report last_access` command is affected by this as of v0.7.x: it only subdivides the time range when a response reaches 5,000 records, which the server-side cap makes unreachable.)

This tool:

- recursively splits the time window until every sub-window fits under the cap, so **no records are silently dropped**,
- verifies the assumed cap at runtime (one extra split) and auto-corrects if the effective cap is lower,
- **loudly reports incompleteness** (warning + exit code `3`) in the one case where data is genuinely unreachable (more than *cap* records within a single minute),
- respects the API rate limit (25 requests/minute) and retries on 429/network errors.

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
userid,records,first_access_iso8601,last_access_iso8601
alice@example.com,128,2026-05-03T10:00:00Z,2026-07-30T15:30:00Z
bob,4,2026-06-15T08:00:00Z,2026-06-18T12:00:00Z
```

- `userid` is the API's `uid` field. It is usually the user's email address, but non-email values (e.g. Cloud Directory usernames) also occur.
- Unauthenticated hits (`anon-user`: internet scans, health checks) are **excluded by default**; the excluded count is printed to stderr. Use `--include-anonymous` to keep them.

Exit codes: `0` success · `1` usage/credentials error · `2` API error before any data · `3` **completed but INCOMPLETE** (see below).

## Limitations

- **The per-call record cap is undocumented behavior.** The default assumption (`--cap 250`) matches the documented `limit` maximum and is verified at runtime, but Akamai may change this behavior at any time.
- If more than *cap* records fall within a single minute, records beyond the cap in that minute cannot be retrieved through this endpoint. The tool warns and exits with code `3` instead of pretending completeness.
- EAA log retention is 365 days; windows older than that return nothing.
- The output contains personal data (usernames). Handle result files accordingly.
- API rate limit is 25 requests/minute; large tenants with long windows take time (the tool paces itself at ~24 requests/minute).

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

[Apache-2.0](LICENSE)
