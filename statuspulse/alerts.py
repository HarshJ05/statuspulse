"""Webhook alerts.

Set ALERT_WEBHOOK_URL to a Slack or Discord incoming-webhook URL (or any endpoint
that accepts JSON). The payload carries both `text` (Slack) and `content`
(Discord), plus structured fields for anything else.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass

import httpx

log = logging.getLogger("statuspulse.alerts")
ICON = {"opened": "🔴", "resolved": "✅"}


@dataclass
class Alert:
    event: str          # opened / resolved
    monitor: str
    kind: str | None
    detail: str
    at: str
    incident_id: int

    def message(self) -> str:
        if self.event == "opened":
            return f"{ICON['opened']} {self.monitor} is DOWN ({self.kind}): {self.detail}"
        return f"{ICON['resolved']} {self.monitor} recovered: {self.detail}"

    def payload(self) -> dict:
        msg = self.message()
        return {"text": msg, "content": msg, **asdict(self)}


async def send(alerts: list[Alert], url: str | None = None, client: httpx.AsyncClient | None = None) -> int:
    """Send alerts; returns how many were delivered. Without a URL, alerts are only logged."""
    url = url if url is not None else os.environ.get("ALERT_WEBHOOK_URL")
    for a in alerts:
        log.warning(a.message())
    if not url or not alerts:
        return 0
    own = client is None
    client = client or httpx.AsyncClient(timeout=10)
    sent = 0
    try:
        for a in alerts:
            try:
                r = await client.post(url, json=a.payload())
                r.raise_for_status()
                sent += 1
            except httpx.HTTPError as e:
                log.error("alert delivery failed: %s", e)
    finally:
        if own:
            await client.aclose()
    return sent
