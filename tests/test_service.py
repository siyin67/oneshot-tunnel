from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.cleaner import ProcessCleaner
from app.host import HostProbeError
from app.models import (
    CleanupError,
    ListenMode,
    ListenPolicy,
    PolicyError,
    PortPoolExhausted,
    TunnelState,
    ValidationError,
)
from app.service import TunnelService
from app.store import PolicyStateStore


def run(coroutine):
    return asyncio.run(coroutine)


class RaisingPortInspector:
    def listening_ports(self):
        raise HostProbeError("/proc/net/tcp 不可读")

    def listening_addresses(self, port: int):
        raise HostProbeError("/proc/net/tcp 不可读")


def create(service: TunnelService, **overrides):
    values = {
        "remote_user": "root",
        "remote_port": 22,
        "ttl_seconds": 60,
        "note": "测试备注",
        "source": "198.51.100.10",
    }
    values.update(overrides)
    return run(service.create_tunnel(**values))


def test_command_and_prompt_contain_required_contract(service: TunnelService):
    application = create(service, remote_port=2222)
    payload = service.application_payload(application)

    command = payload["ssh_command"]
    assert "-N -T" in command
    assert "-o ExitOnForwardFailure=yes" in command
    assert "-o ServerAliveInterval=15" in command
    assert "-o ServerAliveCountMax=3" in command
    assert "-R 127.0.0.1:32000:localhost:2222" in command
    assert "tunnel@tunnel.example.com" in command

    prompt = payload["agent_prompt"]
    assert "服务器连接地址：127.0.0.1" in prompt
    assert "服务器端口：32000" in prompt
    assert "远程电脑登录用户名：root" in prompt
    assert "仅使用服务器拥有者指定的 SSH 私钥" in prompt
    assert "禁止使用密码认证" in prompt
    assert "不写入 authorized_keys" in prompt
    assert "不缓存端口和命令" in prompt
    assert "不要自行重建隧道" in prompt
    assert "test-access-key" not in prompt
    assert payload["remaining_seconds"] == 60


def test_allocation_skips_listening_and_in_memory_ports(service: TunnelService, ports):
    ports.set_listening(32000)
    first = create(service)
    assert first.record.port == 32001
    second = create(service, source="198.51.100.11")
    assert second.record.port == 32002


def test_dedupe_reuses_same_source_only_inside_window(
    service: TunnelService, clock, ports
):
    first = create(service)
    again = create(service)
    assert again.reused is True
    assert again.record.id == first.record.id

    other = create(service, source="198.51.100.11")
    assert other.reused is False
    assert other.record.port != first.record.port

    clock.advance(301)
    after_window = create(service)
    assert after_window.reused is False


def test_pool_exhaustion_is_explicit(service: TunnelService, ports):
    for port in range(32000, 32004):
        ports.set_listening(port)
    with pytest.raises(PortPoolExhausted) as exc:
        create(service)
    assert "端口池已耗尽" in str(exc.value)


def test_request_validation(service: TunnelService):
    with pytest.raises(ValidationError) as exc:
        create(service, remote_user="bad user")
    assert "remote_user" in exc.value.fields

    with pytest.raises(ValidationError) as exc:
        create(service, remote_port=70000)
    assert "remote_port" in exc.value.fields

    with pytest.raises(ValidationError) as exc:
        create(service, ttl_seconds=999)
    assert "ttl_seconds" in exc.value.fields

    with pytest.raises(ValidationError) as exc:
        create(service, note="x" * 121)
    assert "note" in exc.value.fields


def test_status_pending_established_expired_and_cleanup(
    service: TunnelService, ports, cleaner
):
    application = create(service)
    port = application.record.port
    checked = run(service.get_status(application.record.id))
    assert checked.state is TunnelState.PENDING
    assert checked.actual_listen_addresses == ()

    ports.set_listening(port, ("127.0.0.1",))
    checked = run(service.get_status(application.record.id))
    assert checked.state is TunnelState.ESTABLISHED
    assert checked.actual_listen_addresses == ("127.0.0.1",)
    assert checked.record.ever_listening is True

    ports.set_closed(port)
    checked = run(service.get_status(application.record.id))
    assert checked.state is TunnelState.EXPIRED
    assert checked.record.expiry_reason == "listener_closed"
    assert cleaner.calls[0][0] == port
    assert checked.record.cleanup_pending is False


def test_ttl_expiry_cleans_and_releases_port(service: TunnelService, clock, cleaner):
    application = create(service, ttl_seconds=60)
    port = application.record.port
    clock.advance(61)
    checked = run(service.get_status(application.record.id))
    assert checked.state is TunnelState.EXPIRED
    assert checked.record.expiry_reason == "ttl"
    assert cleaner.calls
    assert port not in service._port_to_id


def test_status_probe_failure_reports_unknown(service: TunnelService):
    application = create(service)
    service.ports = RaisingPortInspector()
    checked = run(service.get_status(application.record.id))
    assert checked.state is TunnelState.UNKNOWN
    assert checked.probe_error
    assert checked.remaining_seconds > 0


def test_cleanup_failure_keeps_port_reserved_and_retries(
    service: TunnelService, clock, cleaner, ports
):
    application = create(service)
    port = application.record.port
    ports.set_listening(port)
    cleaner.failures_remaining = 1
    clock.advance(61)
    checked = run(service.get_status(application.record.id))
    assert checked.state is TunnelState.EXPIRED
    assert checked.record.cleanup_pending is True
    assert port in service._port_to_id

    replacement = create(service, source="198.51.100.20")
    assert replacement.record.port != port

    run(service.reap_once())
    assert checked.record.cleanup_pending is False
    assert port not in service._port_to_id


def test_retention_removes_cleaned_tombstone(service: TunnelService, clock, ports):
    application = create(service)
    ports.set_listening(application.record.port)
    clock.advance(61)
    run(service.get_status(application.record.id))
    assert application.record.id in service._records
    clock.advance(31)
    run(service.reap_once())
    assert application.record.id not in service._records


def test_manual_delete_cleans_and_logs(service: TunnelService, ports, cleaner, caplog):
    caplog.set_level(logging.INFO, logger="app.service")
    application = create(service)
    port = application.record.port
    ports.set_listening(port)
    deleted = run(service.delete_tunnel(application.record.id, source="203.0.113.9"))
    assert deleted == port
    assert cleaner.calls[-1][0] == port
    assert port not in ports.listening_ports()
    assert application.record.id not in service._records
    assert "手动删除隧道" in caplog.text
    assert f"port={port}" in caplog.text
    assert "source=203.0.113.9" in caplog.text


def test_manual_delete_failure_is_reported_and_keeps_record(
    service: TunnelService, ports, cleaner
):
    application = create(service)
    ports.set_listening(application.record.port)
    cleaner.failures_remaining = 1
    with pytest.raises(CleanupError):
        run(service.delete_tunnel(application.record.id, source="203.0.113.9"))
    assert application.record.id in service._records
    assert application.record.cleanup_pending is True


def test_policy_specific_validation_and_only_new_tunnels(
    service: TunnelService, interfaces, ports
):
    first = create(service)
    assert first.record.bind_address == "127.0.0.1"

    policy = run(service.set_policy("specific", "10.0.0.5", source="203.0.113.9"))
    assert policy.bind_address == "10.0.0.5"
    second = create(service, source="198.51.100.30")
    assert second.record.bind_address == "10.0.0.5"
    assert "10.0.0.5" in service.application_payload(second)["ssh_command"]
    assert "服务器连接地址：10.0.0.5" in service.application_payload(second)["agent_prompt"]

    # 旧隧道保留申请时的策略。
    assert first.record.bind_address == "127.0.0.1"

    with pytest.raises(PolicyError):
        run(service.set_policy("specific", "10.9.9.9", source="203.0.113.9"))
    with pytest.raises(PolicyError):
        run(service.set_policy("specific", "10.0.0.0/24", source="203.0.113.9"))
    assert service.policy.specific_ip == "10.0.0.5"


def test_all_mode_uses_public_host_for_agent(service: TunnelService):
    run(service.set_policy("all", None, source="203.0.113.9"))
    application = create(service)
    payload = service.application_payload(application)
    assert application.record.bind_address == "0.0.0.0"
    assert application.record.connect_host == "public.example.com"
    assert "-R 0.0.0.0:32000" in payload["ssh_command"]
    assert "服务器连接地址：public.example.com" in payload["agent_prompt"]


def test_start_rejects_specific_ip_missing_from_host(
    base_config, ports, interfaces, cleaner, clock
):
    config = replace(
        base_config,
        tunnel=replace(
            base_config.tunnel,
            listen_mode="specific",
            specific_listen_ip="10.9.9.9",
        ),
    )
    service = TunnelService(
        config,
        ports,
        interfaces,
        cleaner,
        PolicyStateStore(None),
        clock=clock,
    )
    with pytest.raises(PolicyError, match="不在宿主机任何网卡上"):
        run(service.start())


def test_helper_cleanup_passes_range_timeout_and_process_names(
    base_config, monkeypatch
):
    config = replace(
        base_config.cleanup,
        strategy="helper",
        helper_path=sys.executable,
        sudo_path=sys.executable,
        process_names=("sshd", "sshd-session"),
    )
    cleaner = ProcessCleaner(config, base_config.tunnel)
    captured: dict[str, tuple[str, ...]] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = tuple(str(arg) for arg in args)

        class Completed:
            returncode = 0

            async def communicate(self):
                return b"cleaned", b""

        return Completed()

    monkeypatch.setattr("app.cleaner.asyncio.create_subprocess_exec", fake_exec)
    result = run(cleaner.cleanup(32001, 7))
    assert result.success
    assert captured["args"] == (
        sys.executable,
        "-n",
        sys.executable,
        "32001",
        "32000",
        "32003",
        "7",
        "sshd",
        "sshd-session",
    )


def test_helper_cleanup_treats_no_listener_as_success(base_config, monkeypatch):
    config = replace(
        base_config.cleanup,
        strategy="helper",
        helper_path=sys.executable,
        sudo_path=sys.executable,
    )
    cleaner = ProcessCleaner(config, base_config.tunnel)

    async def fake_exec(*args, **kwargs):
        class Completed:
            returncode = 3

            async def communicate(self):
                return b"port not listening", b""

        return Completed()

    monkeypatch.setattr("app.cleaner.asyncio.create_subprocess_exec", fake_exec)
    result = run(cleaner.cleanup(32001, 7))
    assert result.success
    assert "无需清理" in result.message


def test_policy_store_round_trip(interfaces):
    path = Path(__file__).with_name(".policy-state.json")
    path.unlink(missing_ok=True)
    try:
        store = PolicyStateStore(path)
        store.save(ListenPolicy(ListenMode.SPECIFIC, "10.0.0.5"))
        loaded = store.load()
        assert loaded is not None
        assert loaded.mode.value == "specific"
        assert loaded.specific_ip == "10.0.0.5"
    finally:
        path.unlink(missing_ok=True)


def test_admin_views_reflect_states(service: TunnelService, ports, clock):
    application = create(service)
    views, error = run(service.admin_views())
    assert error is None
    assert views[0].state is TunnelState.PENDING

    ports.set_listening(application.record.port)
    views, _ = run(service.admin_views())
    assert views[0].state is TunnelState.ESTABLISHED

    clock.advance(61)
    views, _ = run(service.admin_views())
    assert views[0].state is TunnelState.EXPIRED
