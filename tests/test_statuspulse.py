import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from statuspulse import checks as C
from statuspulse.alerts import Alert, send
from statuspulse.config import Monitor, Settings, load
from statuspulse.incidents import process
from statuspulse.page import build_status, render_html
from statuspulse.runner import run_cycle
from statuspulse.store import Store


# ---- helpers ------------------------------------------------------------------------

async def ok_resolver(host):
    return ["203.0.113.10"]


async def bad_resolver(host):
    raise OSError("Name or service not known")


def cert_in(days):
    async def lookup(host):
        return datetime.now(timezone.utc) + timedelta(days=days)
    return lookup


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def one_check(monitor, handler, resolver=ok_resolver, cert=cert_in(60), **kw):
    async def go():
        async with client_for(handler) as c:
            return await C.check(monitor, c, resolver=resolver, cert_lookup=cert, **kw)
    return run(go())


M = Monitor(name="api", url="https://example.test/health")


def result(monitor="api", status="up", kind=None, at=None, code=200):
    return C.CheckResult(monitor=monitor, checked_at=at or C.now_iso(), status=status,
                         status_code=code, latency_ms=50, error_kind=kind, detail=kind or "")


# ---- checks & classification --------------------------------------------------------

def test_healthy_endpoint_is_up_with_latency_and_cert():
    r = one_check(M, lambda req: httpx.Response(200, text="ok"))
    assert r.status == C.UP and r.status_code == 200
    assert r.latency_ms is not None and r.cert_days_left == pytest.approx(60, abs=0.1)


@pytest.mark.parametrize("code,kind", [(404, C.HTTP_4XX), (401, C.HTTP_4XX), (503, C.HTTP_5XX),
                                       (500, C.HTTP_5XX), (302, C.UNEXPECTED_STATUS)])
def test_status_codes_are_classified(code, kind):
    r = one_check(M, lambda req: httpx.Response(code, headers={"location": "https://example.test/x"} if code == 302 else None))
    if code == 302:
        # redirects are followed; the mock keeps redirecting, so check the classifier directly
        assert C.classify_status(302, (200,)) == C.UNEXPECTED_STATUS
    else:
        assert r.status == C.DOWN and r.error_kind == kind and r.status_code == code


def test_timeout_is_classified():
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)
    r = one_check(M, handler)
    assert r.status == C.DOWN and r.error_kind == C.TIMEOUT


def test_tls_and_connection_errors_are_told_apart():
    def tls(req):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=req)

    def refused(req):
        raise httpx.ConnectError("[Errno 111] Connection refused", request=req)
    assert one_check(M, tls).error_kind == C.TLS_ERROR
    assert one_check(M, refused).error_kind == C.CONNECTION_ERROR


def test_dns_failure_stops_before_http():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200)
    r = one_check(M, handler, resolver=bad_resolver)
    assert r.error_kind == C.DNS_ERROR and r.status == C.DOWN and calls == []


def test_expected_text_and_slow_response():
    m = Monitor(name="api", url="https://example.test/", expect_text='"status":"ok"', slow_ms=1)
    assert one_check(m, lambda req: httpx.Response(200, text="maintenance")).error_kind == C.CONTENT_MISMATCH

    def slow(req):
        import time
        time.sleep(0.01)
        return httpx.Response(200, text='{"status":"ok"}')
    r = one_check(m, slow)
    assert r.status == C.DEGRADED and "Slow" in r.detail


def test_certificate_expiry_rules():
    ok = lambda req: httpx.Response(200)  # noqa: E731
    assert one_check(M, ok, cert=cert_in(5), ssl_warn_days=14).status == C.DEGRADED
    expired = one_check(M, ok, cert=cert_in(-1))
    assert expired.status == C.DOWN and expired.error_kind == C.TLS_ERROR
    assert one_check(Monitor(name="h", url="http://example.test/"), ok).cert_days_left is None


def test_parse_cert_time():
    d = C.parse_cert_time("Jun  1 12:00:00 2027 GMT")
    assert (d.year, d.month, d.day, d.hour) == (2027, 6, 1, 12) and d.tzinfo is not None


# ---- incidents ----------------------------------------------------------------------

def test_single_failure_does_not_open_incident_but_two_do():
    s = Store(":memory:")
    assert process(s, result(status="down", kind=C.HTTP_5XX), fail_threshold=2) == []
    assert s.open_incident("api") is None
    alerts = process(s, result(status="down", kind=C.HTTP_5XX), fail_threshold=2)
    assert [a.event for a in alerts] == ["opened"]
    assert s.open_incident("api")["kind"] == C.HTTP_5XX


def test_incident_timeline_records_kind_change_and_auto_resolves():
    s = Store(":memory:")
    t0 = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    at = lambda m: (t0 + timedelta(minutes=m)).isoformat()  # noqa: E731
    process(s, result(status="down", kind=C.TIMEOUT, at=at(0)))
    process(s, result(status="down", kind=C.TIMEOUT, at=at(5)))
    process(s, result(status="down", kind=C.HTTP_5XX, at=at(10)))
    alerts = process(s, result(status="up", at=at(20)))
    assert [a.event for a in alerts] == ["resolved"] and "20m" in alerts[0].detail
    inc = s.incidents()[0]
    assert inc["closed_at"] == at(20)
    assert [e["event"] for e in inc["timeline"]] == ["opened", "changed", "resolved"]
    assert s.open_incident("api") is None


def test_recovery_without_incident_sends_nothing():
    s = Store(":memory:")
    process(s, result(status="down", kind=C.TIMEOUT))
    assert process(s, result(status="up")) == []


# ---- uptime --------------------------------------------------------------------------

def test_uptime_and_daily_bars():
    s = Store(":memory:")
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    for i in range(10):
        s.add_check(result(status="down" if i < 2 else ("degraded" if i == 2 else "up"),
                           at=(now - timedelta(hours=i)).isoformat()))
    assert s.uptime("api", 1, now) == pytest.approx(0.8)        # degraded counts as up
    bars = s.daily_uptime("api", 90, now)
    assert len(bars) == 90 and bars[-1]["day"] == "2026-11-23" and bars[0]["uptime"] is None


# ---- alerts --------------------------------------------------------------------------

def test_webhook_payload_works_for_slack_and_discord():
    got = []

    def handler(req):
        import json
        got.append(json.loads(req.content))
        return httpx.Response(204)
    a = Alert("opened", "api", C.HTTP_5XX, "HTTP 503", C.now_iso(), 1)

    async def go():
        async with client_for(handler) as c:
            return await send([a], url="https://hooks.example.test/x", client=c)
    assert run(go()) == 1
    assert got[0]["text"] == got[0]["content"] and "DOWN" in got[0]["text"] and got[0]["monitor"] == "api"


def test_alerts_without_webhook_are_only_logged():
    assert run(send([Alert("resolved", "api", None, "ok", C.now_iso(), 1)], url="")) == 0


# ---- full cycle, page and config ------------------------------------------------------

def test_full_cycle_and_status_page():
    settings = Settings(monitors=(Monitor(name="good", url="https://good.test/", label="Good site"),
                                  Monitor(name="bad", url="https://bad.test/")), fail_threshold=1)
    s = Store(":memory:")

    def handler(req):
        return httpx.Response(200 if req.url.host == "good.test" else 503)

    async def go():
        async with client_for(handler) as c:
            return await run_cycle(settings, s, client=c, webhook_url="", resolver=ok_resolver,
                                   cert_lookup=cert_in(60))
    results, alerts = run(go())
    assert {r.monitor: r.status for r in results} == {"good": "up", "bad": "down"}
    assert [a.monitor for a in alerts] == ["bad"]
    st = build_status(settings, s)
    assert st["overall"] == "major_outage"
    html = render_html(st)
    assert "Good site" in html and "Server error (5xx)" in html and "<script" not in html


def test_config_file_loads(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text('[settings]\ntitle="T"\n[[monitor]]\nname="a"\nurl="https://a.test/"\nexpect_status=[200,204]\n')
    s = load(p)
    assert s.title == "T" and s.monitors[0].expect_status == (200, 204)
    assert s.monitors[0].host == "a.test" and s.monitors[0].is_https


def test_repo_monitors_file_is_valid():
    s = load("monitors.toml")
    assert len(s.monitors) >= 5 and all(m.url.startswith("https://") for m in s.monitors)


def test_api_endpoints(tmp_path, monkeypatch):
    cfg = tmp_path / "m.toml"
    cfg.write_text('[[monitor]]\nname="a"\nurl="https://a.test/"\n')
    monkeypatch.setenv("MONITORS_FILE", str(cfg))
    monkeypatch.setenv("STATUSPULSE_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.delenv("RUN_TOKEN", raising=False)
    import statuspulse.api as api
    api = importlib.reload(api)
    from fastapi.testclient import TestClient
    with TestClient(api.app) as client:
        assert client.get("/api/health").json() == {"status": "ok", "monitors": 1}
        assert client.get("/api/status").json()["overall"] == "unknown"
        assert client.get("/api/monitors/nope/checks").status_code == 404
        assert client.get("/api/monitors/a/checks").json() == []
        assert client.post("/api/run").status_code == 403          # disabled without RUN_TOKEN
        assert "Waiting for the first checks" in client.get("/").text
    monkeypatch.setenv("RUN_TOKEN", "secret")
    with TestClient(api.app) as client:
        assert client.post("/api/run", headers={"Authorization": "Bearer wrong"}).status_code == 401
