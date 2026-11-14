"""Domain models shared by the tunnel service, web layer and tests."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ListenMode(str, Enum):
    LOOPBACK = "loopback"
    ALL = "all"
    SPECIFIC = "specific"


LISTEN_MODE_LABELS: dict[ListenMode, str] = {
    ListenMode.LOOPBACK: "仅回环地址",
    ListenMode.ALL: "全部地址",
    ListenMode.SPECIFIC: "指定地址",
}

LISTEN_MODE_DESCRIPTIONS: dict[ListenMode, str] = {
    ListenMode.LOOPBACK: "隧道端口只监听 127.0.0.1，只有宿主机本机可以连接。",
    ListenMode.ALL: "隧道端口监听 0.0.0.0，网络上能访问服务器的机器都可能连接。",
    ListenMode.SPECIFIC: "隧道端口只监听宿主机网卡上的一个指定 IP。",
}


class TunnelState(str, Enum):
    PENDING = "pending"
    ESTABLISHED = "established"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


STATE_LABELS: dict[TunnelState, str] = {
    TunnelState.PENDING: "已分配，待连接",
    TunnelState.ESTABLISHED: "已建立",
    TunnelState.EXPIRED: "已过期",
    TunnelState.UNKNOWN: "未知",
}


def format_host_for_ssh(host: str) -> str:
    """Bracket IPv6 literals for use in ssh's user@host position."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if ip.version == 6 else host


@dataclass(frozen=True)
class ListenPolicy:
    mode: ListenMode
    specific_ip: str | None = None

    @classmethod
    def from_config(cls, mode: str, specific_ip: str) -> "ListenPolicy":
        parsed = ListenMode(mode)
        return cls(parsed, specific_ip if parsed is ListenMode.SPECIFIC else None)

    @property
    def bind_address(self) -> str:
        if self.mode is ListenMode.LOOPBACK:
            return "127.0.0.1"
        if self.mode is ListenMode.ALL:
            return "0.0.0.0"
        if not self.specific_ip:
            raise ValueError("specific listen policy requires specific_ip")
        return self.specific_ip

    def agent_host(self, public_host: str) -> str:
        if self.mode is ListenMode.LOOPBACK:
            return "127.0.0.1"
        if self.mode is ListenMode.ALL:
            if not public_host or public_host in {"0.0.0.0", "::", "[::]"}:
                raise ValueError("all-listen policy requires a reachable public_host")
            return public_host
        if not self.specific_ip:
            raise ValueError("specific listen policy requires specific_ip")
        return self.specific_ip

    @property
    def label(self) -> str:
        return LISTEN_MODE_LABELS[self.mode]

    @property
    def description(self) -> str:
        return LISTEN_MODE_DESCRIPTIONS[self.mode]

    def display_bind(self) -> str:
        address = self.bind_address
        return f"[{address}]" if ":" in address else address

    def display_agent_host(self, public_host: str) -> str:
        address = self.agent_host(public_host)
        return f"[{address}]" if ":" in address else address

    def to_dict(self) -> dict[str, str | None]:
        return {"mode": self.mode.value, "specific_ip": self.specific_ip}


DEFAULT_REMOTE_USER = "sshport"


@dataclass
class TunnelRecord:
    id: str
    port: int
    created_at: datetime
    expires_at: datetime
    ttl_seconds: int
    remote_user: str
    remote_port: int
    remote_password: str = field(repr=False)
    note: str
    source: str
    policy: ListenPolicy
    bind_address: str
    connect_host: str
    ever_listening: bool = False
    last_checked_at: datetime | None = None
    last_known_state: TunnelState = TunnelState.PENDING
    expired_at: datetime | None = None
    expiry_reason: str | None = None
    cleanup_pending: bool = False

    @property
    def is_expired(self) -> bool:
        return self.expired_at is not None


@dataclass
class CheckedTunnel:
    record: TunnelRecord
    state: TunnelState
    checked_at: datetime
    remaining_seconds: int
    actual_listen_addresses: tuple[str, ...] = ()
    probe_error: str | None = None


@dataclass
class CleanupResult:
    success: bool
    message: str
    killed_pids: tuple[int, ...] = field(default_factory=tuple)


class AppError(Exception):
    """Base class for expected service errors."""

    code = "error"
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(AppError):
    code = "validation_error"
    status_code = 400

    def __init__(self, fields: dict[str, str], message: str = "请检查填写内容"):
        super().__init__(message)
        self.fields = fields


class PortPoolExhausted(AppError):
    code = "port_pool_exhausted"
    status_code = 503


class TunnelNotFound(AppError):
    code = "tunnel_not_found"
    status_code = 404


class HostProbeError(AppError):
    code = "host_probe_failed"
    status_code = 503


class CleanupError(AppError):
    code = "cleanup_failed"
    status_code = 502


class PolicyError(AppError):
    code = "policy_invalid"
    status_code = 400


class PolicyPersistenceError(AppError):
    code = "policy_persist_failed"
    status_code = 503


def duration_label(seconds: int) -> str:
    """Human-readable duration for TTL dropdowns and tables."""
    seconds = max(0, int(seconds))
    if seconds % 86400 == 0 and seconds >= 86400:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def compact_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
