# StatusPulse: API uptime and incident monitor

[![tests](https://github.com/HarshJ05/statuspulse/actions/workflows/tests.yml/badge.svg)](https://github.com/HarshJ05/statuspulse/actions/workflows/tests.yml)
[![monitor](https://github.com/HarshJ05/statuspulse/actions/workflows/monitor.yml/badge.svg)](https://github.com/HarshJ05/statuspulse/actions/workflows/monitor.yml)

**Live status page:** https://harshj05.github.io/statuspulse/

StatusPulse checks websites and REST APIs on a schedule, records what happened, turns repeated failures into
incidents with a timeline, sends webhook alerts, and publishes a public status page with uptime for each service.
It watches my own live projects: VaultAI, AdScout, the SpreadTrader and RegimeLab dashboards and their JSON feeds,
and two product prototypes.

## What each check does

1. **DNS:** resolves the host name. A failure here stops the check (`dns_error`).
2. **HTTP:** requests the URL, records the status code and latency, and optionally checks that the body contains
   an expected string (so a "200 OK" maintenance page still counts as a failure).
3. **TLS:** reads the certificate's expiry date; less than 14 days left marks the service as degraded, an expired
   certificate marks it as down.

Failures are classified, because each kind points to a different fix:

| Kind | Meaning | Usually means |
|---|---|---|
| `dns_error` | Name does not resolve | Domain expired or DNS misconfigured |
| `timeout` | No answer in time | Server overloaded, cold start, network issue |
| `tls_error` | Certificate invalid or expired | Renewal failed, wrong host name |
| `connection_error` | Connection refused or reset | Server down, port closed |
| `http_4xx` | 400–499 | Wrong URL, removed page, auth problem (client side) |
| `http_5xx` | 500–599 | Crash, bad deploy, overload (server side) |
| `content_mismatch` | 200, but not the expected content | Error page served with a 200 |

## Incidents and alerts

- An incident opens after **2 consecutive failed checks** (configurable), so a single blip does not alert anyone.
  Its start time is the first failed check.
- If the failure kind changes (for example `timeout` → `http_5xx`), the change is added to the incident timeline.
- The first successful check **closes the incident automatically** and records how long it lasted.
- Alerts go to `ALERT_WEBHOOK_URL` (Slack or Discord incoming webhook; the JSON has both `text` and `content`).
  Without a webhook, alerts are logged.

## How it runs

Two ways, same code:

- **Hosted, free:** `.github/workflows/monitor.yml` runs every 15 minutes on GitHub Actions. It restores the SQLite
  history from the `data` branch, runs the checks, saves the database back (as a single commit, so the repo does not
  grow) and publishes the status page to GitHub Pages.
- **As a service:** a FastAPI app with a built-in scheduler.

```bash
pip install -r requirements.txt
RUN_SCHEDULER=1 uvicorn statuspulse.api:app      # status page at http://localhost:8000
# or
docker build -t statuspulse . && docker run -p 8000:8000 -v $PWD/data:/app/data statuspulse
```

| Endpoint | Returns |
|---|---|
| `GET /` | Status page (HTML) |
| `GET /api/health` | Service health |
| `GET /api/status` | Current state, uptime (24h / 7d / 30d), latency, 90-day history per service |
| `GET /api/monitors/{name}/checks` | Recent checks for one service |
| `GET /api/incidents` | Incidents with timelines |
| `POST /api/run` | Run checks now (needs `Authorization: Bearer $RUN_TOKEN`) |

One-off from the command line: `python -m statuspulse run-once --site site`.

## Configuration

Monitors live in [`monitors.toml`](monitors.toml):

```toml
[[monitor]]
name = "vaultai-api"
url = "https://vaultai-five.vercel.app/api/health"
expect_text = "\"status\":\"ok\""
slow_ms = 6000
```

## Tests

22 pytest tests, using mocked HTTP responses, resolvers and certificates so they run without a network: every
failure kind, slow and degraded responses, certificate expiry, incident opening after the threshold, timeline
changes, automatic resolution with correct duration, uptime and daily history, webhook payloads, the full cycle,
the status page, and the API endpoints. CI also builds the Docker image.

## Project layout

| File | Purpose |
|---|---|
| `statuspulse/checks.py` | DNS, HTTP and TLS checks, failure classification |
| `statuspulse/incidents.py` | Incident state machine |
| `statuspulse/store.py` | SQLite storage, uptime calculations |
| `statuspulse/alerts.py` | Webhook alerts |
| `statuspulse/page.py` | Status summary and status page |
| `statuspulse/api.py` | FastAPI service and scheduler |
| `statuspulse/__main__.py` | Command line (`run-once`, `render`) |

## License

MIT
