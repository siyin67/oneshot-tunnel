"""Tunnel allocation, status probing, expiry and cleanup orchestration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import re
import secrets
import string
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from .cleaner import ProcessCleaner
from .config import AppConfig
from .host import InterfaceInspector, PortInspector
from .models import (
    AppError,
    CheckedTunnel,
    CleanupError,
    CleanupResult,
    HostProbeError,
    ListenMode,
    ListenPolicy,
    PolicyError,
    PortPoolExhausted,
    TunnelNotFound,
    TunnelRecord,
    TunnelState,
    ValidationError,
    format_host_for_ssh,
    utcnow,
)
from .store import PolicyStateStore

logger = logging.getLogger(__name__)

_USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")
_SERVER_USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,31}$")
_REMOTE_PASSWORD_LENGTH = 24
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_remote_password() -> str:
    """Return a shell-safe password shown once to the tunnel owner."""
    while True:
        candidate = "".join(
            secrets.choice(_PASSWORD_ALPHABET)
            for _ in range(_REMOTE_PASSWORD_LENGTH)
        )
        if (
            any(char.islower() for char in candidate)
            and any(char.isupper() for char in candidate)
            and any(char.isdigit() for char in candidate)
        ):
            return candidate


class CleanerProtocol(Protocol):
    async def cleanup(
        self, port: int, timeout_seconds: int | None = None
    ) -> CleanupResult: ...


@dataclass
class TunnelApplication:
    record: TunnelRecord
    reused: bool


@dataclass
class AdminTunnelView:
    record: TunnelRecord
    state: TunnelState
    checked_at: datetime
    remaining_seconds: int
    actual_addresses: tuple[str, ...] = ()
    probe_error: str | None = None


class TunnelService:
    def __init__(
        self,
        config: AppConfig,
        ports: PortInspector,
        interfaces: InterfaceInspector,
        cleaner: CleanerProtocol | ProcessCleaner,
        policy_store: PolicyStateStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], str] | None = None,
        password_factory: Callable[[], str] = generate_remote_password,
    ):
        self.config = config
        self.ports = ports
        self.interfaces = interfaces
        self.cleaner = cleaner
        self.policy_store = policy_store
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._password_factory = password_factory
        self._records: dict[str, TunnelRecord] = {}
        self._port_to_id: dict[int, str] = {}
        self._allocate_lock = asyncio.Lock()
        self._policy_lock = asyncio.Lock()
        self._cleanup_locks: dict[int, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        persisted = policy_store.load()
        self._policy = persisted or ListenPolicy.from_config(
            config.tunnel.listen_mode, config.tunnel.specific_listen_ip
        )
        self._persisted_source = "snapshot" if persisted else "config"

    # ------------------------------------------------------------------ policy
    @property
    def policy(self) -> ListenPolicy:
        return self._policy

    @property
    def policy_source(self) -> str:
        return self._persisted_source

    def policy_info(self) -> dict[str, object]:
        policy = self._policy
        return {
            "policy": policy,
            "mode": policy.mode,
            "mode_value": policy.mode.value,
            "label": policy.label,
            "description": policy.description,
            "specific_ip": policy.specific_ip or "",
            "bind_address": policy.bind_address,
            "public_host": self.config.tunnel.public_host,
        }

    async def set_policy(
        self, mode: str, specific_ip: str | None, *, source: str
    ) -> ListenPolicy:
        try:
            parsed_mode = ListenMode(mode)
        except ValueError as exc:
            raise PolicyError("监听地址策略取值无效") from exc
        specific: str | None = None
        if parsed_mode is ListenMode.SPECIFIC:
            candidate = (specific_ip or "").strip()
            if not candidate:
                raise PolicyError("选择指定地址时必须填写一个具体的监听 IP")
            if "/" in candidate:
                raise PolicyError("指定地址必须是单个 IP，不支持 CIDR 网段")
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError as exc:
                raise PolicyError("指定地址不是有效的 IP 地址") from exc
            if ip.is_unspecified:
                raise PolicyError("指定地址不能是 0.0.0.0 或 :: 通配地址")
            try:
                addresses = await asyncio.to_thread(self.interfaces.addresses)
            except HostProbeError as exc:
                raise PolicyError(str(exc)) from exc
            if candidate not in addresses:
                current = "、".join(sorted(addresses)[:8]) or "（未读取到任何地址）"
                raise PolicyError(
                    f"IP {candidate} 不在宿主机任何网卡上，已拒绝保存。当前可用地址：{current}"
                )
            specific = candidate
        policy = ListenPolicy(parsed_mode, specific)
        async with self._policy_lock:
            self.policy_store.save(policy)
            self._policy = policy
            self._persisted_source = "snapshot"
        logger.info(
            "监听地址策略已更新：mode=%s specific_ip=%s source=%s",
            policy.mode.value,
            policy.specific_ip or "-",
            source,
        )
        return policy

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._policy.mode is ListenMode.SPECIFIC:
            addresses = await asyncio.to_thread(self.interfaces.addresses)
            if self._policy.specific_ip not in addresses:
                raise PolicyError(
                    f"配置的监听 IP {self._policy.specific_ip} "
                    "不在宿主机任何网卡上，请修正配置或策略快照"
                )
        if self.config.cleanup.clean_orphans_on_startup:
            await self._cleanup_startup_orphans()
        self._stop_event.clear()
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="tunnel-reaper")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._reaper_task
        self._reaper_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _cleanup_startup_orphans(self) -> None:
        try:
            listening = await asyncio.to_thread(self.ports.listening_ports)
        except HostProbeError as exc:
            logger.warning("启动清理跳过：无法读取宿主机端口表：%s", exc)
            return
        candidates = sorted(
            port
            for port in listening
            if self.config.tunnel.port_start <= port <= self.config.tunnel.port_end
        )
        for port in candidates:
            result = await self.cleaner.cleanup(
                port, self.config.tunnel.cleanup_timeout_seconds
            )
            if result.success:
                logger.info("启动清理：端口 %s 的残留隧道进程已清理", port)
            else:
                logger.warning("启动清理：端口 %s 清理失败：%s", port, result.message)

    # --------------------------------------------------------------- allocation
    def _configured_command(self, record: TunnelRecord) -> str:
        tunnel = self.config.tunnel
        bind = record.bind_address
        bind_part = f"[{bind}]" if ":" in bind else bind
        target = f"{record.server_user}@{format_host_for_ssh(tunnel.server_host)}"
        return (
            f"ssh -N -T -p {tunnel.server_port} "
            f"-o ExitOnForwardFailure=yes "
            f"-o PreferredAuthentications=password "
            f"-o PubkeyAuthentication=no "
            f"-o NumberOfPasswordPrompts=1 "
            f"-o ServerAliveInterval={tunnel.keepalive_interval_seconds} "
            f"-o ServerAliveCountMax={tunnel.keepalive_count_max} "
            f"-o ConnectTimeout=15 "
            f"-R {bind_part}:{record.port}:localhost:{record.remote_port} "
            f"{target}"
        )

    def _remote_setup_command(self, record: TunnelRecord) -> str:
        user = record.remote_user
        password = record.remote_password
        return (
            "# Run this in an elevated PowerShell on the remote Windows computer.\n"
            "$ErrorActionPreference = 'Stop'\n"
            f"$name = '{user}'\n"
            f"$password = ConvertTo-SecureString '{password}' -AsPlainText -Force\n"
            "if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) {\n"
            "    Set-LocalUser -Name $name -Password $password "
            "-PasswordNeverExpires $true\n"
            "} else {\n"
            "    New-LocalUser -Name $name -Password $password "
            "-PasswordNeverExpires -AccountNeverExpires "
            "-Description 'One-shot SSH tunnel account'\n"
            "}\n"
            "Enable-LocalUser -Name $name\n"
            "$usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'\n"
            "if (-not (Get-LocalGroupMember -Group $usersGroup.Name -Member $name "
            "-ErrorAction SilentlyContinue)) {\n"
            "    Add-LocalGroupMember -Group $usersGroup.Name -Member $name\n"
            "}"
        )

    def _agent_prompt(self, record: TunnelRecord) -> str:
        host = format_host_for_ssh(record.connect_host)
        remote = f"{record.remote_user}@{host}"
        return (
            "你正在通过一条一次性 SSH 反向隧道操作远程 Windows 电脑。\n"
            "\n"
            "连接信息：\n"
            f"- 服务器连接地址：{record.connect_host}\n"
            f"- 服务器端口：{record.port}\n"
            f"- 远程电脑登录用户名：{record.remote_user}\n"
            f"- 远程电脑登录密码：{record.remote_password}\n"
            f"- 隧道目标：远程 Windows 的 localhost:{record.remote_port}，"
            "不需要直接连接该端口\n"
            "- 远程电脑运行 Windows；需要执行命令时使用 PowerShell"
            "（例如 powershell -NoProfile -Command \"...\"），不要假设有 bash。\n"
            "\n"
            "认证方式：\n"
            "- 本地 Agent 登录远程电脑使用账号密码，不使用私钥认证。\n"
            f"- 连接示例：ssh -p {record.port} "
            f"-o PreferredAuthentications=password -o PubkeyAuthentication=no {remote}\n"
            "- 出现 Password 提示时输入上面的密码；不要把密码写入脚本、命令行参数、"
            "日志或任何持久化文件。\n"
            "- 如果本地 Agent 无法交互输入密码，且本地环境已安装 sshpass，可使用 "
            "`sshpass -e ssh ...`，通过 `SSHPASS` 环境变量传入密码；不要使用 "
            "`sshpass -p`，也不要把密码写进文件。\n"
            "- 远程 Windows 的 OpenSSH Server 必须允许密码认证；如果密码被拒绝，"
            "停止并报告，不要修改远程电脑的 SSH 配置。\n"
            "\n"
            "服务器隧道账号：\n"
            f"- 远程电脑上执行 ssh -R 时连接的是服务器用户 {record.server_user}，"
            "其密码由服务器拥有者设置，终端会提示输入，不在本提示词中。\n"
            "\n"
            "操作边界：\n"
            "- 只通过上述隧道操作远程电脑，不尝试其他入口。\n"
            "- 不修改远程电脑的 SSH 配置，不安装常驻服务，不写入 authorized_keys "
            "或任何授权文件。\n"
            "- 隧道为一次性资源。任务结束后立即终止隧道（在远程电脑上停止执行本条 "
            "ssh -R 命令的进程），不缓存密码、端口和命令，不复用。\n"
            "- 如果连接失败、端口不再监听或隧道已经断开：立即停止操作并报告；"
            "不要自行重建隧道、改用其他端口或反复重试。\n"
            "\n"
            "说明：网站只能探测服务器端口是否在监听，不探测远程电脑的 SSH 服务可用性。"
        )

    async def _listening_snapshot(self) -> dict[int, tuple[str, ...]]:
        return await asyncio.to_thread(self.ports.listening_ports)

    def _find_dedupe_candidate(self, source: str, now: datetime) -> TunnelRecord | None:
        cutoff = now - timedelta(seconds=self.config.tunnel.dedupe_window_seconds)
        candidates = [
            record
            for record in self._records.values()
            if record.source == source
            and record.expired_at is None
            and record.created_at >= cutoff
            and record.expires_at > now
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.created_at)

    def _validate_request(
        self,
        server_user: str | None,
        remote_user: str,
        remote_port: int,
        ttl_seconds: int,
        note: str,
    ) -> tuple[str, str, int, str]:
        fields: dict[str, str] = {}
        raw_server_user = (
            self.config.tunnel.user if server_user is None else server_user
        )
        if not isinstance(raw_server_user, str):
            fields["server_user"] = "连接服务器的 SSH 用户名必须是字符串"
            normalized_server_user = ""
        else:
            normalized_server_user = raw_server_user.strip()
            if not normalized_server_user:
                fields["server_user"] = "连接服务器的 SSH 用户名不能为空"
            elif not _SERVER_USER_RE.fullmatch(normalized_server_user):
                fields["server_user"] = (
                    "服务器用户名只能包含字母、数字、点、下划线、连字符，"
                    "长度 1-32，且不能以连字符开头"
                )
        normalized_user = remote_user.strip()
        if not normalized_user:
            fields["remote_user"] = "远程 SSH 用户名不能为空"
        elif normalized_user.lower() in {"root", "administrator"}:
            fields["remote_user"] = (
                "不能使用 root 或 Windows 内置 Administrator，"
                "请填写远程电脑上的专用标准用户"
            )
        elif not _USER_RE.fullmatch(normalized_user):
            fields["remote_user"] = "用户名只能包含字母、数字、点、下划线、连字符，长度 1-20"
        elif len(normalized_user) > 20:
            fields["remote_user"] = "Windows 本地用户名不能超过 20 个字符"
        if not isinstance(remote_port, int) or isinstance(remote_port, bool):
            fields["remote_port"] = "远程 SSH 端口必须是整数"
        elif not 1 <= remote_port <= 65_535:
            fields["remote_port"] = "远程 SSH 端口必须在 1 到 65535 之间"
        if ttl_seconds not in self.config.tunnel.allowed_ttl_seconds:
            fields["ttl_seconds"] = "所选有效期不在允许的档位中"
        normalized_note = note.strip()
        if len(normalized_note) > 120:
            fields["note"] = "备注最多 120 个字符"
        if any(ord(char) < 32 or ord(char) == 127 for char in normalized_note):
            fields["note"] = "备注不能包含控制字符"
        if fields:
            raise ValidationError(fields)
        return normalized_server_user, normalized_user, remote_port, normalized_note

    async def create_tunnel(
        self,
        *,
        remote_user: str,
        remote_port: int,
        ttl_seconds: int,
        note: str,
        source: str,
        server_user: str | None = None,
    ) -> TunnelApplication:
        (
            normalized_server_user,
            normalized_user,
            normalized_port,
            normalized_note,
        ) = self._validate_request(
            server_user, remote_user, remote_port, ttl_seconds, note
        )
        now = self._clock()
        async with self._allocate_lock:
            existing = self._find_dedupe_candidate(source, now)
            if existing is not None:
                logger.info(
                    "重复申请复用隧道：id=%s port=%s source=%s",
                    existing.id,
                    existing.port,
                    source,
                )
                return TunnelApplication(existing, reused=True)

            try:
                listening = await self._listening_snapshot()
            except HostProbeError:
                raise
            used = set(self._port_to_id)
            selected: int | None = None
            for port in range(
                self.config.tunnel.port_start, self.config.tunnel.port_end + 1
            ):
                if port in used or port in listening:
                    continue
                selected = port
                break
            if selected is None:
                raise PortPoolExhausted(
                    "端口池已耗尽：当前端口范围内没有可用端口，请稍后重试或删除不再使用的隧道"
                )
            try:
                policy = self._policy
                bind = policy.bind_address
                connect_host = policy.agent_host(self.config.tunnel.public_host)
            except ValueError as exc:
                raise PolicyError(str(exc)) from exc
            record = TunnelRecord(
                id=self._id_factory(),
                port=selected,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
                ttl_seconds=ttl_seconds,
                remote_user=normalized_user,
                remote_port=normalized_port,
                server_user=normalized_server_user,
                remote_password=self._password_factory(),
                note=normalized_note,
                source=source,
                policy=policy,
                bind_address=bind,
                connect_host=connect_host,
            )
            self._records[record.id] = record
            self._port_to_id[selected] = record.id
            logger.info(
                "隧道已分配：id=%s port=%s ttl=%ss server_user=%s "
                "remote_user=%s remote_port=%s listen_mode=%s bind=%s source=%s",
                record.id,
                record.port,
                record.ttl_seconds,
                record.server_user,
                record.remote_user,
                record.remote_port,
                record.policy.mode.value,
                record.bind_address,
                source,
            )
            return TunnelApplication(record, reused=False)

    def application_payload(
        self, application: TunnelApplication, *, now: datetime | None = None
    ) -> dict[str, object]:
        record = application.record
        now = now or self._clock()
        remaining = max(0, math.ceil((record.expires_at - now).total_seconds()))
        return {
            "id": record.id,
            "port": record.port,
            "server_user": record.server_user,
            "remote_user": record.remote_user,
            "remote_port": record.remote_port,
            "remote_password": record.remote_password,
            "remote_setup_command": self._remote_setup_command(record),
            "note": record.note,
            "ttl_seconds": record.ttl_seconds,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "remaining_seconds": remaining,
            "listen_mode": record.policy.mode.value,
            "listen_mode_label": record.policy.label,
            "listen_address": record.bind_address,
            "connect_host": record.connect_host,
            "ssh_command": self._configured_command(record),
            "agent_prompt": self._agent_prompt(record),
            "reused": application.reused,
        }

    # ------------------------------------------------------------------- status
    def _remaining(self, record: TunnelRecord, now: datetime) -> int:
        return max(0, math.ceil((record.expires_at - now).total_seconds()))

    def _mark_expired(self, record: TunnelRecord, reason: str, now: datetime) -> None:
        if record.expired_at is not None:
            return
        record.expired_at = now
        record.expiry_reason = reason
        record.last_known_state = TunnelState.EXPIRED
        record.cleanup_pending = True
        logger.info(
            "隧道已过期：id=%s port=%s reason=%s at=%s",
            record.id,
            record.port,
            reason,
            now.isoformat(),
        )

    def _release_port(self, record: TunnelRecord) -> None:
        if self._port_to_id.get(record.port) == record.id:
            self._port_to_id.pop(record.port, None)

    def _cleanup_lock(self, port: int) -> asyncio.Lock:
        lock = self._cleanup_locks.get(port)
        if lock is None:
            lock = asyncio.Lock()
            self._cleanup_locks[port] = lock
        return lock

    async def _cleanup_record(
        self, record: TunnelRecord, *, force: bool = False
    ) -> CleanupResult:
        lock = self._cleanup_lock(record.port)
        async with lock:
            if not record.cleanup_pending and not force:
                return CleanupResult(True, "端口已清理")
            try:
                result = await self.cleaner.cleanup(
                    record.port, self.config.tunnel.cleanup_timeout_seconds
                )
            except AppError as exc:
                result = CleanupResult(False, exc.message)
            except Exception as exc:  # defensive: never let a reaper loop die
                logger.exception("清理端口 %s 时出现未预期错误", record.port)
                result = CleanupResult(False, f"清理异常：{exc}")
            if result.success:
                record.cleanup_pending = False
                self._release_port(record)
                logger.info(
                    "隧道清理完成：id=%s port=%s killed_pids=%s",
                    record.id,
                    record.port,
                    ",".join(str(pid) for pid in result.killed_pids) or "-",
                )
            else:
                logger.warning(
                    "隧道清理失败：id=%s port=%s reason=%s message=%s",
                    record.id,
                    record.port,
                    record.expiry_reason or "manual",
                    result.message,
                )
            return result

    async def get_status(self, tunnel_id: str) -> CheckedTunnel:
        record = self._records.get(tunnel_id)
        if record is None:
            raise TunnelNotFound("隧道不存在或已经被回收")
        now = self._clock()
        if record.expired_at is not None:
            # Status is always derived from a fresh host probe, including for
            # expired tombstones.  This keeps "port still listening after a
            # failed cleanup" visible and lets a later status request retry the
            # idempotent cleanup.
            listening_expired: dict[int, tuple[str, ...]] | None = None
            probe_error_expired: str | None = None
            try:
                listening_expired = await self._listening_snapshot()
            except HostProbeError as exc:
                probe_error_expired = str(exc)
            now = self._clock()
            record.last_checked_at = now
            if record.cleanup_pending:
                await self._cleanup_record(record)
            addresses_expired = (
                tuple(listening_expired.get(record.port, ()))
                if listening_expired is not None
                else ()
            )
            return CheckedTunnel(
                record,
                TunnelState.EXPIRED,
                now,
                self._remaining(record, now),
                addresses_expired,
                probe_error_expired,
            )

        listening: dict[int, tuple[str, ...]] | None = None
        probe_error: str | None = None
        try:
            listening = await self._listening_snapshot()
        except HostProbeError as exc:
            probe_error = str(exc)

        now = self._clock()
        if now >= record.expires_at:
            self._mark_expired(record, "ttl", now)
            record.last_checked_at = now
            addresses = tuple(listening.get(record.port, ())) if listening is not None else ()
            await self._cleanup_record(record)
            return CheckedTunnel(
                record,
                TunnelState.EXPIRED,
                now,
                self._remaining(record, now),
                addresses,
                probe_error,
            )

        if listening is None:
            record.last_checked_at = now
            return CheckedTunnel(
                record,
                TunnelState.UNKNOWN,
                now,
                self._remaining(record, now),
                (),
                probe_error,
            )

        addresses = tuple(listening.get(record.port, ()))
        record.last_checked_at = now
        if addresses:
            record.ever_listening = True
            record.last_known_state = TunnelState.ESTABLISHED
            state = TunnelState.ESTABLISHED
        elif record.ever_listening:
            self._mark_expired(record, "listener_closed", now)
            await self._cleanup_record(record)
            state = TunnelState.EXPIRED
        else:
            state = TunnelState.PENDING
            record.last_known_state = state
        return CheckedTunnel(
            record,
            state,
            now,
            self._remaining(record, now),
            addresses,
        )

    def status_payload(self, checked: CheckedTunnel) -> dict[str, object]:
        record = checked.record
        return {
            "id": record.id,
            "port": record.port,
            "state": checked.state.value,
            "state_label": {
                TunnelState.PENDING: "已分配，待连接",
                TunnelState.ESTABLISHED: "已建立",
                TunnelState.EXPIRED: "已过期",
                TunnelState.UNKNOWN: "未知",
            }[checked.state],
            "expires_at": record.expires_at,
            "remaining_seconds": checked.remaining_seconds,
            "listen_address": record.bind_address,
            "checked_at": checked.checked_at,
            "actual_listen_addresses": list(checked.actual_listen_addresses),
            "probe_error": checked.probe_error,
        }

    # -------------------------------------------------------------------- admin
    async def admin_views(self) -> tuple[list[AdminTunnelView], str | None]:
        now = self._clock()
        try:
            listening = await self._listening_snapshot()
            probe_error = None
        except HostProbeError as exc:
            listening = None
            probe_error = str(exc)
        views: list[AdminTunnelView] = []
        for record in sorted(self._records.values(), key=lambda item: item.created_at, reverse=True):
            if record.expired_at is not None or now >= record.expires_at:
                state = TunnelState.EXPIRED
                addresses: tuple[str, ...] = ()
            elif listening is None:
                state = TunnelState.UNKNOWN
                addresses = ()
            else:
                addresses = tuple(listening.get(record.port, ()))
                if addresses:
                    state = TunnelState.ESTABLISHED
                elif record.ever_listening:
                    state = TunnelState.EXPIRED
                else:
                    state = TunnelState.PENDING
            views.append(
                AdminTunnelView(
                    record=record,
                    state=state,
                    checked_at=now,
                    remaining_seconds=self._remaining(record, now),
                    actual_addresses=addresses,
                    probe_error=probe_error,
                )
            )
        return views, probe_error

    async def delete_tunnel(self, tunnel_id: str, *, source: str) -> int:
        record = self._records.get(tunnel_id)
        if record is None:
            raise TunnelNotFound("隧道不存在或已经被回收")
        now = self._clock()
        if record.expired_at is None:
            self._mark_expired(record, "manual", now)
        # An already-cleaned tombstone must not trigger a cleanup against a port
        # that may have been allocated to a newer tunnel.
        if record.cleanup_pending:
            result = await self._cleanup_record(record, force=True)
            if not result.success:
                raise CleanupError(result.message)
        self._remove_record(record)
        logger.info(
            "手动删除隧道：port=%s tunnel_id=%s time=%s source=%s",
            record.port,
            record.id,
            self._clock().isoformat(),
            source,
        )
        return record.port

    def _remove_record(self, record: TunnelRecord) -> None:
        if self._records.pop(record.id, None) is not None:
            self._release_port(record)

    # ------------------------------------------------------------------- reaper
    async def reap_once(self) -> None:
        now = self._clock()
        try:
            listening = await self._listening_snapshot()
            probe_error = None
        except HostProbeError as exc:
            listening = None
            probe_error = str(exc)
            logger.warning("自动回收本轮无法探测端口：%s", exc)

        for record in list(self._records.values()):
            try:
                if record.expired_at is None:
                    if now >= record.expires_at:
                        self._mark_expired(record, "ttl", now)
                        await self._cleanup_record(record)
                    elif record.cleanup_pending:
                        await self._cleanup_record(record)
                    elif listening is not None:
                        addresses = listening.get(record.port, ())
                        record.last_checked_at = now
                        if addresses:
                            if not record.ever_listening:
                                logger.info(
                                    "自动回收检测到隧道已建立：id=%s port=%s",
                                    record.id,
                                    record.port,
                                )
                            record.ever_listening = True
                            record.last_known_state = TunnelState.ESTABLISHED
                        elif record.ever_listening:
                            self._mark_expired(record, "listener_closed", now)
                            await self._cleanup_record(record)
                else:
                    if record.cleanup_pending:
                        await self._cleanup_record(record)
                    if (
                        not record.cleanup_pending
                        and record.expired_at
                        and now
                        >= record.expired_at
                        + timedelta(seconds=self.config.tunnel.expired_retention_seconds)
                    ):
                        self._remove_record(record)
                        logger.info(
                            "已移除过期隧道记录：id=%s port=%s", record.id, record.port
                        )
            except Exception:
                logger.exception(
                    "自动回收处理隧道 %s 时发生错误（port=%s）", record.id, record.port
                )
        if probe_error:
            logger.debug("自动回收探测错误：%s", probe_error)

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await self.reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("自动回收循环出现未预期错误")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.reaper_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue
            return
