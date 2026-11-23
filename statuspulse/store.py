"""SQLite storage for checks, incidents and incident timelines."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    status_code INTEGER,
    latency_ms REAL,
    error_kind TEXT,
    detail TEXT,
    cert_expires_at TEXT,
    cert_days_left REAL
);
CREATE INDEX IF NOT EXISTS idx_checks_monitor_time ON checks(monitor, checked_at);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor TEXT NOT NULL,
    kind TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    last_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents(monitor, closed_at);

CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    at TEXT NOT NULL,
    event TEXT NOT NULL,        -- opened / changed / resolved
    detail TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = "data/statuspulse.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- checks -------------------------------------------------------------------
    def add_check(self, r) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO checks (monitor, checked_at, status, status_code, latency_ms, error_kind,"
                      " detail, cert_expires_at, cert_days_left) VALUES (?,?,?,?,?,?,?,?,?)",
                      (r.monitor, r.checked_at, r.status, r.status_code, r.latency_ms, r.error_kind,
                       r.detail, r.cert_expires_at, r.cert_days_left))

    def recent_checks(self, monitor: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM checks WHERE monitor=? ORDER BY checked_at DESC, id DESC LIMIT ?",
                                 (monitor, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_check(self, monitor: str) -> dict | None:
        rows = self.recent_checks(monitor, 1)
        return rows[0] if rows else None

    def consecutive_failures(self, monitor: str) -> int:
        n = 0
        for r in self.recent_checks(monitor, 100):
            if r["status"] != "down":
                break
            n += 1
        return n

    def failure_streak_start(self, monitor: str) -> str | None:
        """Time of the first failed check in the current run of failures."""
        first = None
        for r in self.recent_checks(monitor, 100):
            if r["status"] != "down":
                break
            first = r["checked_at"]
        return first

    def uptime(self, monitor: str, days: float, now: datetime | None = None) -> float | None:
        """Share of checks in the window that were not 'down' (degraded counts as up)."""
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status != 'down' THEN 1 ELSE 0 END) AS up "
            "FROM checks WHERE monitor=? AND checked_at >= ?", (monitor, since)).fetchone()
        return None if not row["n"] else row["up"] / row["n"]

    def daily_uptime(self, monitor: str, days: int = 90, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        start = (now - timedelta(days=days - 1)).date()
        rows = self.conn.execute(
            "SELECT substr(checked_at,1,10) AS day, COUNT(*) AS n, "
            "SUM(CASE WHEN status != 'down' THEN 1 ELSE 0 END) AS up "
            "FROM checks WHERE monitor=? AND checked_at >= ? GROUP BY day", (monitor, start.isoformat())).fetchall()
        by_day = {r["day"]: r["up"] / r["n"] for r in rows}
        out = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            out.append({"day": d, "uptime": by_day.get(d)})
        return out

    def avg_latency(self, monitor: str, days: float = 1, now: datetime | None = None) -> float | None:
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).isoformat()
        row = self.conn.execute("SELECT AVG(latency_ms) AS a FROM checks WHERE monitor=? AND checked_at>=? "
                                "AND latency_ms IS NOT NULL AND status != 'down'", (monitor, since)).fetchone()
        return None if row["a"] is None else round(row["a"], 1)

    # ---- incidents ------------------------------------------------------------------
    def open_incident(self, monitor: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM incidents WHERE monitor=? AND closed_at IS NULL "
                                "ORDER BY id DESC LIMIT 1", (monitor,)).fetchone()
        return dict(row) if row else None

    def create_incident(self, monitor: str, kind: str, at: str, detail: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO incidents (monitor, kind, opened_at, last_detail) VALUES (?,?,?,?)",
                            (monitor, kind, at, detail))
            iid = cur.lastrowid
            c.execute("INSERT INTO incident_events (incident_id, at, event, detail) VALUES (?,?,?,?)",
                      (iid, at, "opened", detail))
        return iid

    def add_event(self, incident_id: int, at: str, event: str, detail: str, kind: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO incident_events (incident_id, at, event, detail) VALUES (?,?,?,?)",
                      (incident_id, at, event, detail))
            if kind:
                c.execute("UPDATE incidents SET kind=?, last_detail=? WHERE id=?", (kind, detail, incident_id))
            else:
                c.execute("UPDATE incidents SET last_detail=? WHERE id=?", (detail, incident_id))

    def close_incident(self, incident_id: int, at: str, detail: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE incidents SET closed_at=? WHERE id=?", (at, incident_id))
            c.execute("INSERT INTO incident_events (incident_id, at, event, detail) VALUES (?,?,?,?)",
                      (incident_id, at, "resolved", detail))

    def incidents(self, limit: int = 50, monitor: str | None = None) -> list[dict]:
        q = "SELECT * FROM incidents" + (" WHERE monitor=?" if monitor else "") + " ORDER BY opened_at DESC, id DESC LIMIT ?"
        args = (monitor, limit) if monitor else (limit,)
        out = []
        for r in self.conn.execute(q, args).fetchall():
            d = dict(r)
            d["timeline"] = [dict(e) for e in self.conn.execute(
                "SELECT at, event, detail FROM incident_events WHERE incident_id=? ORDER BY id", (r["id"],)).fetchall()]
            out.append(d)
        return out

    def prune(self, keep_days: int = 120) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self.tx() as c:
            c.execute("DELETE FROM checks WHERE checked_at < ?", (since,))
