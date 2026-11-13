from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import pytest

from app.config import AppConfig, StorageConfig, load_config
from app.host import FakeInterfaceInspector, FakePortInspector
from app.models import CleanupResult
from app.service import TunnelService
from app.store import PolicyStateStore

ROOT = Path(__file__).resolve().parents[1]


class MutableClock:
    def __init__(self, value: datetime | None = None) -> None:
        self.value = value or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeCleaner:
    def __init__(self, ports: FakePortInspector) -> None:
        self.ports = ports
        self.calls: list[tuple[int, int | None]] = []
        self.failures_remaining = 0
        self.killed_pids: tuple[int, ...] = (4242,)

    async def cleanup(self, port: int, timeout_seconds: int | None = None) -> CleanupResult:
        self.calls.append((port, timeout_seconds))
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            return CleanupResult(False, "模拟清理失败")
        self.ports.set_closed(port)
        return CleanupResult(True, "模拟清理完成", self.killed_pids)


@pytest.fixture
def base_config() -> AppConfig:
    config = load_config(ROOT / "config" / "config.example.toml")
    return replace(
        config,
        access=replace(
            config.access,
            keys=("test-access-key",),
            session_ttl_seconds=3600,
            cookie_secure=False,
            login_rate_limit=replace(
                config.access.login_rate_limit,
                max_failures=2,
                window_seconds=60,
                lockout_seconds=120,
            ),
        ),
        tunnel=replace(
            config.tunnel,
            user="tunnel",
            server_host="tunnel.example.com",
            server_port=22,
            port_start=32000,
            port_end=32003,
            default_ttl_seconds=60,
            allowed_ttl_seconds=(60, 120),
            dedupe_window_seconds=300,
            expired_retention_seconds=30,
            cleanup_timeout_seconds=2,
            listen_mode="loopback",
            specific_listen_ip="",
            public_host="public.example.com",
            keepalive_interval_seconds=15,
            keepalive_count_max=3,
        ),
        application_limit=replace(
            config.application_limit, max_requests=50, window_seconds=60
        ),
        status_limit=replace(config.status_limit, max_requests=500, window_seconds=60),
        poll_interval_seconds=5,
        reaper_interval_seconds=60,
        cleanup=replace(
            config.cleanup,
            strategy="direct",
            clean_orphans_on_startup=False,
        ),
        storage=StorageConfig(None),
    )


@pytest.fixture
def ports() -> FakePortInspector:
    return FakePortInspector()


@pytest.fixture
def interfaces() -> FakeInterfaceInspector:
    return FakeInterfaceInspector({"127.0.0.1", "10.0.0.5"})


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def cleaner(ports: FakePortInspector) -> FakeCleaner:
    return FakeCleaner(ports)


@pytest.fixture
def id_factory() -> Callable[[], str]:
    counter = {"value": 0}

    def next_id() -> str:
        counter["value"] += 1
        return f"tunnel-{counter['value']}"

    return next_id


@pytest.fixture
def service(
    base_config: AppConfig,
    ports: FakePortInspector,
    interfaces: FakeInterfaceInspector,
    cleaner: FakeCleaner,
    clock: MutableClock,
    id_factory: Callable[[], str],
) -> TunnelService:
    return TunnelService(
        base_config,
        ports,
        interfaces,
        cleaner,
        PolicyStateStore(None),
        clock=clock,
        id_factory=id_factory,
    )
