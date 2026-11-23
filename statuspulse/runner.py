"""One monitoring cycle: check everything, update incidents, send alerts."""
from __future__ import annotations

from .alerts import send
from .checks import check_all
from .config import Settings
from .incidents import process
from .store import Store


async def run_cycle(settings: Settings, store: Store, *, client=None, webhook_url=None, **check_kw):
    results = await check_all(settings.monitors, client=client, ssl_warn_days=settings.ssl_warn_days, **check_kw)
    alerts = []
    for r in results:
        alerts += process(store, r, settings.fail_threshold)
    await send(alerts, url=webhook_url)
    return results, alerts
