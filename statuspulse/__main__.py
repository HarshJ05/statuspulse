"""Command line.

    python -m statuspulse run-once [--site site]   # one cycle, then write the static status page
    python -m statuspulse render  --site site      # only re-render the page
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from . import config
from .page import build_status, render_html
from .runner import run_cycle
from .store import Store

FOOTER = ('Checks run about every 15 minutes from GitHub Actions. '
          '<a href="https://github.com/HarshJ05/statuspulse">Source on GitHub</a> · '
          '<a href="status.json">status.json</a>')


def write_site(settings, store, site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    status = build_status(settings, store)
    (site / "index.html").write_text(render_html(status, footer_note=FOOTER))
    (site / "status.json").write_text(json.dumps(status, indent=1))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="statuspulse")
    ap.add_argument("command", choices=["run-once", "render"])
    ap.add_argument("--config", default="monitors.toml")
    ap.add_argument("--db", default="data/statuspulse.db")
    ap.add_argument("--site", default=None, help="write the static status page to this folder")
    a = ap.parse_args()

    settings = config.load(a.config)
    store = Store(a.db)
    if a.command == "run-once":
        results, alerts = asyncio.run(run_cycle(settings, store))
        for r in results:
            code = r.status_code if r.status_code is not None else "-"
            lat = f"{r.latency_ms:.0f}ms" if r.latency_ms is not None else "-"
            print(f"{r.status:9} {r.monitor:24} {code!s:>4} {lat:>7}  {r.error_kind or ''} {r.detail}")
        print(f"{len(alerts)} alert(s)")
        store.prune()
    if a.site:
        write_site(settings, store, Path(a.site))


if __name__ == "__main__":
    main()
