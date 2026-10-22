# Smoke test

`scripts/smoke_test.py` checks a **running deployment** end to end: health, auth errors,
CRUD, the uniqueness race under concurrency, scopes, Server-Sent Events, WebSockets and
signed webhooks. Run it after every deploy.

```bash
# local server
python scripts/run_local.py &
python scripts/smoke_test.py --url http://127.0.0.1:8000 --jwt-secret "$JWT_SECRET" \
    --webhook-secret "$WEBHOOK_SECRET_ACME"

# a deployment whose JWT secret you hold
python scripts/smoke_test.py --url https://<app>.modal.run --jwt-secret "$JWT_SECRET" \
    --webhook-secret "$WEBHOOK_SECRET_ACME" --yes
```

It **writes data** — items named `smoke-<timestamp>-*` (deleted afterwards) plus the
events and webhook ids they cause (expired by retention) — so a non-local URL needs
`--yes`. Exit code 0 means every check passed (skips are fine), 1 means a check failed.

## Modal deployments

Modal keeps `JWT_SECRET` unreadable, so mint short-lived tokens *inside* Modal with the
deployment's own secret (only the tokens come back, written to a private file):

```bash
modal run --env feat scripts/modal_mint_token.py --out /tmp/tokens.json
python scripts/smoke_test.py --url https://<app>.modal.run --yes \
    --token "$(python -c 'import json;print(json.load(open("/tmp/tokens.json"))["full"])')" \
    --limited-token "$(python -c 'import json;print(json.load(open("/tmp/tokens.json"))["limited"])')" \
    --webhook-secret "$WEBHOOK_SECRET_ACME"
rm /tmp/tokens.json
```

| Option | Meaning |
|---|---|
| `--token`, `--limited-token` | tokens with `items:read items:write events:read webhooks:read`, and read-only `items:read events:read` |
| `--jwt-secret` | mint both locally instead (`SMOKE_JWT_SECRET` or `JWT_SECRET`) |
| `--webhook-secret`, `--provider` | enable the signed-webhook checks for `WEBHOOK_SECRET_<PROVIDER>` |
| `--only`, `--skip` | groups: `basic,scopes,race,sse,ws,webhooks` |
| `--concurrency` | simultaneous creates in the uniqueness race (default 30) |

Without a limited token the scope checks are skipped; without a webhook secret only the
unknown-provider check runs.

## What it deliberately does not check

The stream cap and rate limits are **per container**. With several containers behind one
URL a smoke test can show neither that they work nor that they do not, so they are covered
by the unit tests and, when you need a live check, by pinning `max_containers=1` for a run.
