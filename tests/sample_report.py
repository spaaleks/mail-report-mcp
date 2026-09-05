REPORT_TITLE = "Embedded API — Round v7"
REPORT_SUBTITLE = "blind reassessment, 2026-09-01"

REPORT_MARKDOWN = """\
# Embedded API — Round v7

No Critical or High findings this round. The tenant boundary held under every probe that broke it in v5, and the two remaining issues are configuration drift rather than logic flaws. Diminishing returns are close: v8 should change the attack surface, not repeat this one.

| ID | Sev | Title |
|------------|----------|--------------------------------------|
| EMB-v7-001 | Medium | Stale CORS allowlist entry |
| EMB-v7-002 | Low | Verbose error body on malformed JWT |
| EMB-v7-003 | Info | Server header discloses build number |

---

## Findings

### EMB-v7-001 — Medium — Stale CORS allowlist entry

The allowlist still carries the old staging origin, decommissioned when staging moved in-house. Nothing serves that name today, but the record is registrable, so whoever claims it inherits credentialed cross-origin reads.

**Repro:**

```http
GET /v1/tenants/self HTTP/1.1
Host: api.example.com
Origin: https://staging.example.net

HTTP/1.1 200 OK
Access-Control-Allow-Origin: https://staging.example.net
Access-Control-Allow-Credentials: true
```

**Impact:** an attacker registering `staging.example.net` reads any authenticated response a victim's browser will make. Session cookies ride along because `Allow-Credentials` is `true`.

**Fix:** drop the entry from `config/cors.yaml`, and fail the deploy when an allowlist origin stops resolving. See the [CORS notes](https://example.com/docs/cors).

### EMB-v7-002 — Low — Verbose error body on malformed JWT

A malformed token returns the parser's internal message rather than a generic rejection.

**Repro:**

```
$ curl -s -H 'Authorization: Bearer aaa.bbb.ccc' https://api.example.com/v1/ping
{"error":"invalid signature: expected HS256, got none; kid=prod-2024-11"}
```

**Impact:** confirms the signing algorithm and leaks a live `kid`. Useful for narrowing an offline attack, not exploitable on its own.

**Fix:** return `401` with a fixed body. Keep the detail in the server log.

### EMB-v7-003 — Info — Server header discloses build number

`Server: edge/4.11.2-rc3` names an exact build. Costs nothing to remove.

---

## Severity scale

| Sev | Meaning | Acts on |
|---------------|-------------------------------------|-----------------|
| Critical | Boundary broken, no precondition | Page now |
| High | Boundary broken behind one condition | This sprint |
| Medium | Exploitable with a second weakness | Next release |
| Low | Leaks detail, not access | Backlog |
| Info | Hygiene only | Optional |
| Med | alias of Medium | — |
| Informational | alias of Info | — |

---

## Controls that held

- **Tenant isolation.** Fifty cross-tenant reads with a valid token for tenant A against tenant B's resources: `403` on all fifty, no timing separation above noise.
- **Rate limiting.** Sustained `1200 req/min` against `/v1/auth/token` shed load from request 61 onward and never returned a `5xx`.
- **Replay.** Captured requests replayed after nonce expiry were rejected. The window is `30s` and it is honoured to the second.
- **Path traversal.** `../` in every path segment, encoded and doubly encoded, produced `400` — never a filesystem read.

Probes retired this round, written with the asterisk marker the renderer also accepts:

* Token confusion against the `none` algorithm
* Header smuggling through `Transfer-Encoding`
* both chunked and identity spellings
* IDOR sweep across sequential tenant ids

> The boundary work from v5 is holding. What is left is hygiene, and the next
> round should target the provisioning flow instead.

## Cleanup

Created and removed: three probe tenants (`probe-a7`, `probe-b1`, `probe-c9`), one service account, and `41` synthetic records. Nothing persists. The staging origin from EMB-v7-001 was *not* registered.

## Net

Two fixable issues, neither reachable without a second precondition, and no path across the tenant boundary. Ship the CORS fix this week; the rest can ride the next release.
"""

EXPECTED_FRAGMENTS = {
    "h1 heading": "<h1",
    "h2 heading": "<h2",
    "h3 heading": "<h3",
    "bold": "<strong>",
    "inline code": "<code",
    "fenced code block": "<pre",
    "table": "<table",
    "table header cell": "<th",
    "table body cell": "<td",
    "bullet list": "<ul",
    "list item": "<li",
    "blockquote": "<blockquote",
    "horizontal rule": "<hr",
    "link": '<a href="https://example.com/docs/cors"',
    "severity pill": "border-radius:10px",
    "asterisk bullet list": "Token confusion",
}

EXPECTED_SEVERITY_PILLS = {
    "Critical": "#7b1020",
    "High": "#ec483b",
    "Medium": "#d35400",
    "Low": "#b7791f",
    "Info": "#5f6b7a",
}

EXPECTED_TEXT = (
    "Stale CORS allowlist entry",
    "Tenant isolation.",
    "probe-a7",
)

