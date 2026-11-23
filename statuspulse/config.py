"""Monitor definitions, loaded from monitors.toml."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Monitor:
    name: str                       # short id, e.g. "vaultai-api"
    url: str
    label: str = ""                 # display name on the status page
    group: str = "Services"
    method: str = "GET"
    expect_status: tuple[int, ...] = (200,)
    expect_text: str | None = None  # body must contain this (optional)
    timeout_s: float = 10.0
    slow_ms: int = 3000             # slower than this counts as "degraded"

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url).hostname or ""

    @property
    def is_https(self) -> bool:
        return self.url.lower().startswith("https://")


@dataclass(frozen=True)
class Settings:
    fail_threshold: int = 2         # consecutive failures before an incident opens
    ssl_warn_days: int = 14         # warn when a certificate expires sooner than this
    interval_s: int = 300           # how often the built-in scheduler runs
    title: str = "StatusPulse"
    monitors: tuple[Monitor, ...] = field(default_factory=tuple)


def load(path: str | Path = "monitors.toml") -> Settings:
    data = tomllib.loads(Path(path).read_text())
    s = data.get("settings", {})
    monitors = []
    for m in data.get("monitor", []):
        if "expect_status" in m:
            m = {**m, "expect_status": tuple(m["expect_status"])}
        monitors.append(Monitor(**m))
    names = [m.name for m in monitors]
    if len(names) != len(set(names)):
        raise ValueError("monitor names must be unique")
    return Settings(monitors=tuple(monitors), **s)
