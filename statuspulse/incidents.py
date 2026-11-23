"""Turn individual check results into incidents.

Rules:
  * An incident opens after `fail_threshold` consecutive failed checks, so a single
    network blip does not page anyone.
  * While open, a change in failure kind (e.g. timeout -> HTTP 503) is added to the
    incident's timeline.
  * The first successful check closes the incident automatically.
Alerts are sent when an incident opens and when it resolves.
"""
from __future__ import annotations

from datetime import datetime

from .alerts import Alert
from .checks import DOWN, CheckResult


def _duration(start: str, end: str) -> str:
    secs = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    h, rem = divmod(max(secs, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


def process(store, result: CheckResult, fail_threshold: int = 2) -> list[Alert]:
    """Record a check and update incidents. Returns alerts to send."""
    store.add_check(result)
    alerts: list[Alert] = []
    incident = store.open_incident(result.monitor)

    if result.status == DOWN:
        fails = store.consecutive_failures(result.monitor)
        if incident is None and fails >= fail_threshold:
            # The outage began at the first failed check, not when the threshold was reached.
            began = store.failure_streak_start(result.monitor) or result.checked_at
            iid = store.create_incident(result.monitor, result.error_kind, began,
                                        f"{result.error_kind}: {result.detail}")
            alerts.append(Alert("opened", result.monitor, result.error_kind, result.detail,
                                result.checked_at, iid))
        elif incident is not None and incident["kind"] != result.error_kind:
            store.add_event(incident["id"], result.checked_at, "changed",
                            f"{result.error_kind}: {result.detail}", kind=result.error_kind)
    elif incident is not None:
        took = _duration(incident["opened_at"], result.checked_at)
        detail = f"Recovered after {took}" + (f" (HTTP {result.status_code})" if result.status_code else "")
        store.close_incident(incident["id"], result.checked_at, detail)
        alerts.append(Alert("resolved", result.monitor, incident["kind"], detail, result.checked_at, incident["id"]))
    return alerts
