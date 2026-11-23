"""Build the status summary and render the public status page (HTML)."""
from __future__ import annotations

import html
from datetime import datetime, timezone

from .config import Settings
from .store import Store

KIND_LABEL = {
    "dns_error": "DNS failure", "timeout": "Timeout", "tls_error": "TLS / certificate error",
    "connection_error": "Connection error", "http_4xx": "Client error (4xx)", "http_5xx": "Server error (5xx)",
    "unexpected_status": "Unexpected status", "content_mismatch": "Wrong content",
}


def build_status(settings: Settings, store: Store, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    services = []
    for m in settings.monitors:
        last = store.last_check(m.name)
        inc = store.open_incident(m.name)
        if last is None:
            state = "unknown"
        elif inc:
            state = "down"
        else:
            state = last["status"]   # up / degraded (a single failed check is shown as degraded)
            if state == "down":
                state = "degraded"
        services.append({
            "name": m.name, "label": m.label or m.name, "group": m.group, "url": m.url, "state": state,
            "last_checked": last["checked_at"] if last else None,
            "last_status_code": last["status_code"] if last else None,
            "last_latency_ms": last["latency_ms"] if last else None,
            "last_detail": last["detail"] if last else None,
            "cert_days_left": last["cert_days_left"] if last else None,
            "uptime_24h": store.uptime(m.name, 1, now), "uptime_7d": store.uptime(m.name, 7, now),
            "uptime_30d": store.uptime(m.name, 30, now), "avg_latency_24h": store.avg_latency(m.name, 1, now),
            "daily": store.daily_uptime(m.name, 90, now),
        })
    states = [s["state"] for s in services]
    overall = ("major_outage" if "down" in states else "degraded" if "degraded" in states
               else "operational" if states and all(s == "up" for s in states) else "unknown")
    return {"title": settings.title, "generated_at": now.replace(microsecond=0).isoformat(),
            "overall": overall, "services": services, "incidents": store.incidents(20)}


def _pct(v):
    return "–" if v is None else f"{v * 100:.2f}%"


def _bar_color(u):
    if u is None:
        return "#e3e7ec"
    if u >= 0.999:
        return "#2fb67c"
    if u >= 0.98:
        return "#9bd36a"
    if u >= 0.9:
        return "#f2b843"
    return "#e5534b"


def render_html(status: dict, footer_note: str = "") -> str:
    e = html.escape
    banner = {"operational": ("All systems operational", "#2fb67c"),
              "degraded": ("Some services are degraded", "#f2b843"),
              "major_outage": ("Service outage in progress", "#e5534b"),
              "unknown": ("Waiting for the first checks", "#8a94a3")}[status["overall"]]
    dot = {"up": "#2fb67c", "degraded": "#f2b843", "down": "#e5534b", "unknown": "#8a94a3"}
    word = {"up": "Operational", "degraded": "Degraded", "down": "Down", "unknown": "No data"}

    rows = []
    for s in status["services"]:
        bars = "".join(
            f'<span class="b" style="background:{_bar_color(d["uptime"])}" '
            f'title="{d["day"]}: {_pct(d["uptime"]) if d["uptime"] is not None else "no data"}"></span>'
            for d in s["daily"])
        meta = []
        if s["last_status_code"]:
            meta.append(f"HTTP {s['last_status_code']}")
        if s["last_latency_ms"] is not None:
            meta.append(f"{s['last_latency_ms']:.0f} ms")
        if s["cert_days_left"] is not None:
            meta.append(f"TLS cert {s['cert_days_left']:.0f} days left")
        detail = f'<div class="detail">{e(s["last_detail"])}</div>' if s["last_detail"] else ""
        rows.append(f'''
      <div class="svc">
        <div class="svc-top">
          <div><a class="name" href="{e(s["url"])}">{e(s["label"])}</a><span class="grp">{e(s["group"])}</span></div>
          <div class="state" style="color:{dot[s["state"]]}"><span class="dot" style="background:{dot[s["state"]]}"></span>{word[s["state"]]}</div>
        </div>
        <div class="bars">{bars}</div>
        <div class="svc-meta"><span>90 days ago</span><span>24h {_pct(s["uptime_24h"])} · 7d {_pct(s["uptime_7d"])} · 30d {_pct(s["uptime_30d"])}</span><span>Today</span></div>
        <div class="last">{" · ".join(meta)}</div>{detail}
      </div>''')

    incs = []
    for i in status["incidents"]:
        timeline = "".join(f'<li><b>{e(t["event"].capitalize())}</b> <span class="t">{e(t["at"].replace("T", " ")[:16])} UTC</span> — {e(t["detail"] or "")}</li>'
                           for t in i["timeline"])
        state = "Resolved" if i["closed_at"] else "Ongoing"
        incs.append(f'''
      <div class="inc">
        <div class="inc-top"><b>{e(i["monitor"])}</b> · {e(KIND_LABEL.get(i["kind"], i["kind"]))}
          <span class="pill {"ok" if i["closed_at"] else "bad"}">{state}</span></div>
        <ul>{timeline}</ul>
      </div>''')
    inc_html = "".join(incs) or '<p class="muted">No incidents recorded yet.</p>'

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(status["title"])} · Status</title>
<style>
 body{{margin:0;background:#f5f7fa;color:#1d2733;font:15px/1.5 "Segoe UI",Inter,system-ui,-apple-system,sans-serif}}
 .wrap{{max-width:860px;margin:0 auto;padding:34px 20px 50px}}
 h1{{font-size:24px;margin:0 0 4px}} .sub{{color:#6b7684;font-size:13.5px}}
 .banner{{color:#fff;border-radius:10px;padding:16px 20px;font-weight:600;font-size:17px;margin:20px 0}}
 .svc{{background:#fff;border:1px solid #e1e6ec;border-radius:10px;padding:14px 16px;margin:10px 0}}
 .svc-top{{display:flex;justify-content:space-between;align-items:center;gap:10px}}
 .name{{font-weight:600;color:#1d2733;text-decoration:none}} .name:hover{{text-decoration:underline}}
 .grp{{font-size:11.5px;color:#6b7684;background:#eef2f6;border-radius:99px;padding:1px 8px;margin-left:8px}}
 .state{{font-weight:600;font-size:14px;white-space:nowrap}} .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}}
 .bars{{display:flex;gap:2px;margin:12px 0 4px;height:30px}} .b{{flex:1;border-radius:2px}}
 .svc-meta{{display:flex;justify-content:space-between;font-size:12px;color:#6b7684}}
 .last{{font-size:12.5px;color:#4a5563;margin-top:6px;font-family:ui-monospace,Consolas,monospace}}
 .detail{{font-size:12.5px;color:#b5462f;margin-top:2px}}
 h2{{font-size:16px;margin:30px 0 8px}}
 .inc{{background:#fff;border:1px solid #e1e6ec;border-radius:10px;padding:12px 16px;margin:8px 0}}
 .inc ul{{margin:6px 0 0;padding-left:18px;font-size:13.5px}} .t{{color:#6b7684;font-size:12px}}
 .pill{{font-size:11.5px;border-radius:99px;padding:1px 9px;margin-left:6px}} .pill.ok{{background:#e3f6ec;color:#1f7a52}} .pill.bad{{background:#fde8e6;color:#b5462f}}
 .muted{{color:#6b7684}} footer{{color:#6b7684;font-size:12.5px;margin-top:30px}} footer a{{color:#3366cc}}
</style></head><body><div class="wrap">
 <h1>{e(status["title"])}</h1>
 <div class="sub">Updated {e(status["generated_at"].replace("T", " ")[:16])} UTC</div>
 <div class="banner" style="background:{banner[1]}">{banner[0]}</div>
 {"".join(rows)}
 <h2>Incident history</h2>
 {inc_html}
 <footer>{footer_note}</footer>
</div></body></html>'''
