"""Configuration loading and strict validation.

The file is intentionally parsed with the Python standard library (tomllib) so
the service has one less dependency at startup.  Every field used by the service
is validated here; an invalid configuration aborts startup with a readable list
of problems instead of failing later while allocating a port.
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = "/app/config/config.toml"
CONFIG_ENV = "SSHPORT_CONFIG"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""

    def __init__(self, path: Path | str, errors: list[str]):
        self.path = Path(path)
        self.errors = errors
        message = f"配置文件 {self.path} 无效：\n" + "\n".join(
            f"  - {error}" for error in errors
        )
        super().__init__(message)


@dataclass(frozen=True)
class ServerConfig:
    listen_host: str
    port: int
    timezone: str


@dataclass(frozen=True)
class LoginLimitConfig:
    max_failures: int
    window_seconds: int
    lockout_seconds: int


@dataclass(frozen=True)
class AccessConfig:
    keys: tuple[str, ...] = field(repr=False)
    session_ttl_seconds: int
    cookie_secure: bool
    login_rate_limit: LoginLimitConfig


@dataclass(frozen=True)
class LimitConfig:
    max_requests: int
    window_seconds: int

    @property
    def requests_per_second(self) -> float:
        return self.max_requests / self.window_seconds


@dataclass(frozen=True)
class TunnelConfig:
    user: str
    server_host: str
    server_port: int
    port_start: int
    port_end: int
    default_ttl_seconds: int
    allowed_ttl_seconds: tuple[int, ...]
    dedupe_window_seconds: int
    expired_retention_seconds: int
    cleanup_timeout_seconds: int
    listen_mode: str
    specific_listen_ip: str
    public_host: str
    keepalive_interval_seconds: int
    keepalive_count_max: int


@dataclass(frozen=True)
class CleanupConfig:
    strategy: str
    helper_path: str
    sudo_path: str
    process_names: tuple[str, ...]
    clean_orphans_on_startup: bool


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    access_log: bool


@dataclass(frozen=True)
class StorageConfig:
    policy_state_file: Path | None


@dataclass(frozen=True)
class AppConfig:
    path: Path
    server: ServerConfig
    access: AccessConfig
    tunnel: TunnelConfig
    application_limit: LimitConfig
    status_limit: LimitConfig
    poll_interval_seconds: int
    reaper_interval_seconds: int
    cleanup: CleanupConfig
    logging: LoggingConfig
    storage: StorageConfig


_TOP_LEVEL_KEYS = {
    "server",
    "access",
    "tunnel",
    "limits",
    "status",
    "reaper",
    "cleanup",
    "logging",
    "storage",
}
_SECTION_KEYS = {
    "server": {"listen_host", "port", "timezone"},
    "access": {"keys", "session_ttl_seconds", "cookie_secure", "login_rate_limit"},
    "access.login_rate_limit": {
        "max_failures",
        "window_seconds",
        "lockout_seconds",
    },
    "tunnel": {
        "user",
        "server_host",
        "server_port",
        "port_start",
        "port_end",
        "default_ttl_seconds",
        "allowed_ttl_seconds",
        "dedupe_window_seconds",
        "expired_retention_seconds",
        "cleanup_timeout_seconds",
        "listen_mode",
        "specific_listen_ip",
        "public_host",
        "keepalive_interval_seconds",
        "keepalive_count_max",
    },
    "limits": {"application", "status"},
    "limits.application": {"max_requests", "window_seconds"},
    "limits.status": {"max_requests", "window_seconds"},
    "status": {"poll_interval_seconds"},
    "reaper": {"interval_seconds"},
    "cleanup": {
        "strategy",
        "helper_path",
        "sudo_path",
        "process_names",
        "clean_orphans_on_startup",
    },
    "logging": {"level", "access_log"},
    "storage": {"policy_state_file"},
}
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\."
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_USER_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$")
_PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _table(
    data: Mapping[str, Any], key: str, path: str, errors: list[str]
) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        errors.append(f"缺少配置项 {path}{key}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}{key} 必须是表（table）")
        return {}
    return value


def _reject_unknown(
    data: Mapping[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for key in sorted(set(data) - allowed):
        errors.append(f"{path}{key} 是未知配置项")


def _string(
    data: Mapping[str, Any],
    key: str,
    path: str,
    errors: list[str],
    *,
    allow_empty: bool = False,
) -> str:
    value = data.get(key)
    full = f"{path}{key}"
    if not isinstance(value, str):
        errors.append(f"{full} 必须是字符串")
        return ""
    if not allow_empty and not value.strip():
        errors.append(f"{full} 不能为空")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        errors.append(f"{full} 不能包含控制字符")
    return value


def _boolean(
    data: Mapping[str, Any], key: str, path: str, errors: list[str]
) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        errors.append(f"{path}{key} 必须是 true 或 false")
        return False
    return value


def _integer(
    data: Mapping[str, Any],
    key: str,
    path: str,
    errors: list[str],
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(key)
    full = f"{path}{key}"
    if not _is_int(value):
        errors.append(f"{full} 必须是整数")
        return minimum
    if value < minimum or value > maximum:
        errors.append(f"{full} 必须在 {minimum} 到 {maximum} 之间")
    return int(value)


def _valid_host(host: str) -> bool:
    if not host or host != host.strip():
        return False
    if host.startswith("-") or "@" in host or "/" in host or ":" in host:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            # Hostnames cannot contain a colon.  Bracketed IPv6 is not stored.
            return False
    return bool(_HOSTNAME_RE.fullmatch(host))


def format_ssh_host(host: str) -> str:
    """Return a host suitable for the target part of an ssh command."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        return f"[{host}]"
    return host


def _validate_access_keys(
    data: Mapping[str, Any], errors: list[str]
) -> tuple[str, ...]:
    value = data.get("keys")
    if not isinstance(value, list) or not value:
        errors.append("access.keys 必须是非空字符串数组")
        return ()
    keys: list[str] = []
    for index, item in enumerate(value):
        path = f"access.keys[{index}]"
        if not isinstance(item, str):
            errors.append(f"{path} 必须是字符串")
            continue
        if not item:
            errors.append(f"{path} 不能为空")
            continue
        if item != item.strip():
            errors.append(f"{path} 首尾不能有空白字符")
        if any(ord(char) < 32 or ord(char) == 127 for char in item):
            errors.append(f"{path} 不能包含控制字符")
        if item in keys:
            errors.append(f"{path} 与前面的访问密钥重复")
        keys.append(item)
    return tuple(keys)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(config_path, [f"文件不存在或不是普通文件：{config_path}"])
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(config_path, [f"无法读取文件：{exc}"]) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(config_path, [f"TOML 语法错误：{exc}"]) from exc

    if not isinstance(raw, dict):
        raise ConfigError(config_path, ["配置文件根节点必须是表（table）"])

    errors: list[str] = []
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "", errors)

    server_table = _table(raw, "server", "", errors)
    _reject_unknown(server_table, _SECTION_KEYS["server"], "server.", errors)
    access_table = _table(raw, "access", "", errors)
    _reject_unknown(access_table, _SECTION_KEYS["access"], "access.", errors)
    tunnel_table = _table(raw, "tunnel", "", errors)
    _reject_unknown(tunnel_table, _SECTION_KEYS["tunnel"], "tunnel.", errors)
    limits_table = _table(raw, "limits", "", errors)
    _reject_unknown(limits_table, _SECTION_KEYS["limits"], "limits.", errors)
    status_table = _table(raw, "status", "", errors)
    _reject_unknown(status_table, _SECTION_KEYS["status"], "status.", errors)
    reaper_table = _table(raw, "reaper", "", errors)
    _reject_unknown(reaper_table, _SECTION_KEYS["reaper"], "reaper.", errors)
    cleanup_table = _table(raw, "cleanup", "", errors)
    _reject_unknown(cleanup_table, _SECTION_KEYS["cleanup"], "cleanup.", errors)
    logging_table = _table(raw, "logging", "", errors)
    _reject_unknown(logging_table, _SECTION_KEYS["logging"], "logging.", errors)
    storage_table = raw.get("storage", {})
    if not isinstance(storage_table, dict):
        errors.append("storage 必须是表（table）")
        storage_table = {}
    _reject_unknown(storage_table, _SECTION_KEYS["storage"], "storage.", errors)

    # server
    listen_host = _string(server_table, "listen_host", "server.", errors)
    if listen_host and any(char.isspace() for char in listen_host):
        errors.append("server.listen_host 不能包含空白字符")
    port = _integer(
        server_table, "port", "server.", errors, minimum=1, maximum=65_535
    )
    timezone_name = _string(server_table, "timezone", "server.", errors)
    if timezone_name:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"server.timezone 不是有效时区：{timezone_name}")

    # access
    keys = _validate_access_keys(access_table, errors)
    session_ttl_seconds = _integer(
        access_table,
        "session_ttl_seconds",
        "access.",
        errors,
        minimum=60,
        maximum=31_536_000,
    )
    cookie_secure = _boolean(access_table, "cookie_secure", "access.", errors)
    login_table = _table(access_table, "login_rate_limit", "access.", errors)
    _reject_unknown(
        login_table,
        _SECTION_KEYS["access.login_rate_limit"],
        "access.login_rate_limit.",
        errors,
    )
    login_limit = LoginLimitConfig(
        max_failures=_integer(
            login_table,
            "max_failures",
            "access.login_rate_limit.",
            errors,
            minimum=1,
            maximum=10_000,
        ),
        window_seconds=_integer(
            login_table,
            "window_seconds",
            "access.login_rate_limit.",
            errors,
            minimum=1,
            maximum=86_400,
        ),
        lockout_seconds=_integer(
            login_table,
            "lockout_seconds",
            "access.login_rate_limit.",
            errors,
            minimum=1,
            maximum=604_800,
        ),
    )

    # limits
    apply_table = _table(limits_table, "application", "limits.", errors)
    _reject_unknown(
        apply_table,
        _SECTION_KEYS["limits.application"],
        "limits.application.",
        errors,
    )
    application_limit = LimitConfig(
        max_requests=_integer(
            apply_table,
            "max_requests",
            "limits.application.",
            errors,
            minimum=1,
            maximum=100_000,
        ),
        window_seconds=_integer(
            apply_table,
            "window_seconds",
            "limits.application.",
            errors,
            minimum=1,
            maximum=86_400,
        ),
    )
    status_rate_table = _table(limits_table, "status", "limits.", errors)
    _reject_unknown(
        status_rate_table,
        _SECTION_KEYS["limits.status"],
        "limits.status.",
        errors,
    )
    status_limit = LimitConfig(
        max_requests=_integer(
            status_rate_table,
            "max_requests",
            "limits.status.",
            errors,
            minimum=1,
            maximum=100_000,
        ),
        window_seconds=_integer(
            status_rate_table,
            "window_seconds",
            "limits.status.",
            errors,
            minimum=1,
            maximum=86_400,
        ),
    )
    if (
        status_limit.max_requests <= application_limit.max_requests
        or status_limit.requests_per_second
        <= application_limit.requests_per_second
    ):
        errors.append(
            "limits.status 的限流阈值必须高于 limits.application（按有效速率比较）"
        )

    poll_interval_seconds = _integer(
        status_table,
        "poll_interval_seconds",
        "status.",
        errors,
        minimum=5,
        maximum=300,
    )

    # tunnel
    user = _string(tunnel_table, "user", "tunnel.", errors)
    if user and not _USER_RE.fullmatch(user):
        errors.append("tunnel.user 只能包含字母、数字、点、下划线、连字符，且不能以连字符开头")
    server_host = _string(tunnel_table, "server_host", "tunnel.", errors)
    if server_host and not _valid_host(server_host):
        errors.append("tunnel.server_host 必须是有效主机名或 IP 地址，且不能带端口或协议")
    server_port = _integer(
        tunnel_table,
        "server_port",
        "tunnel.",
        errors,
        minimum=1,
        maximum=65_535,
    )
    port_start = _integer(
        tunnel_table,
        "port_start",
        "tunnel.",
        errors,
        minimum=1,
        maximum=65_535,
    )
    port_end = _integer(
        tunnel_table,
        "port_end",
        "tunnel.",
        errors,
        minimum=1,
        maximum=65_535,
    )
    if port_start > port_end:
        errors.append("tunnel.port_start 不能大于 tunnel.port_end")
    if port_start <= port <= port_end:
        errors.append("server.port 不能位于隧道端口范围内")
    if port_start <= server_port <= port_end:
        errors.append("tunnel.server_port 不能位于隧道端口范围内")
    default_ttl_seconds = _integer(
        tunnel_table,
        "default_ttl_seconds",
        "tunnel.",
        errors,
        minimum=60,
        maximum=31_536_000,
    )
    allowed_raw = tunnel_table.get("allowed_ttl_seconds")
    allowed_ttls: list[int] = []
    if not isinstance(allowed_raw, list) or not allowed_raw:
        errors.append("tunnel.allowed_ttl_seconds 必须是非空整数数组")
    else:
        for index, item in enumerate(allowed_raw):
            if not _is_int(item) or item < 60 or item > 31_536_000:
                errors.append(
                    f"tunnel.allowed_ttl_seconds[{index}] 必须是 60 到 31536000 的整数"
                )
                continue
            if item in allowed_ttls:
                errors.append(f"tunnel.allowed_ttl_seconds[{index}] 重复")
                continue
            allowed_ttls.append(item)
    allowed_ttls.sort()
    if allowed_ttls and default_ttl_seconds not in allowed_ttls:
        errors.append("tunnel.default_ttl_seconds 必须等于 allowed_ttl_seconds 中的一个档位")
    dedupe_window_seconds = _integer(
        tunnel_table,
        "dedupe_window_seconds",
        "tunnel.",
        errors,
        minimum=1,
        maximum=86_400,
    )
    expired_retention_seconds = _integer(
        tunnel_table,
        "expired_retention_seconds",
        "tunnel.",
        errors,
        minimum=60,
        maximum=86_400,
    )
    cleanup_timeout_seconds = _integer(
        tunnel_table,
        "cleanup_timeout_seconds",
        "tunnel.",
        errors,
        minimum=1,
        maximum=120,
    )
    listen_mode = _string(tunnel_table, "listen_mode", "tunnel.", errors)
    if listen_mode not in {"loopback", "all", "specific"}:
        errors.append("tunnel.listen_mode 必须是 loopback、all 或 specific")
    specific_listen_ip = _string(
        tunnel_table,
        "specific_listen_ip",
        "tunnel.",
        errors,
        allow_empty=True,
    )
    public_host = _string(
        tunnel_table, "public_host", "tunnel.", errors, allow_empty=True
    )
    if public_host:
        try:
            public_ip = ipaddress.ip_address(public_host)
            if public_ip.is_unspecified:
                errors.append("tunnel.public_host 不能是 0.0.0.0 或 ::")
        except ValueError:
            if not _valid_host(public_host):
                errors.append("tunnel.public_host 必须是有效主机名或 IP 地址，且不能带端口")
    if listen_mode == "specific":
        if not specific_listen_ip:
            errors.append("listen_mode 为 specific 时必须配置 tunnel.specific_listen_ip")
        else:
            try:
                ip = ipaddress.ip_address(specific_listen_ip)
            except ValueError:
                errors.append("tunnel.specific_listen_ip 必须是单个 IP 地址，不能是 CIDR 网段")
            else:
                if ip.is_unspecified:
                    errors.append("tunnel.specific_listen_ip 不能是通配地址")
    elif specific_listen_ip:
        # Keeping a stored value while another mode is active is useful when
        # switching back, but it still has to be safe.
        try:
            ip = ipaddress.ip_address(specific_listen_ip)
        except ValueError:
            errors.append("tunnel.specific_listen_ip 必须是单个 IP 地址，不能是 CIDR 网段")
        else:
            if ip.is_unspecified:
                errors.append("tunnel.specific_listen_ip 不能是通配地址")
    if listen_mode == "all" and not public_host:
        errors.append("listen_mode 为 all 时必须配置 tunnel.public_host（对外可达地址）")
    keepalive_interval_seconds = _integer(
        tunnel_table,
        "keepalive_interval_seconds",
        "tunnel.",
        errors,
        minimum=1,
        maximum=3_600,
    )
    keepalive_count_max = _integer(
        tunnel_table,
        "keepalive_count_max",
        "tunnel.",
        errors,
        minimum=1,
        maximum=100,
    )

    # cleanup
    strategy = _string(cleanup_table, "strategy", "cleanup.", errors)
    if strategy not in {"auto", "direct", "helper"}:
        errors.append("cleanup.strategy 必须是 auto、direct 或 helper")
    helper_path = _string(cleanup_table, "helper_path", "cleanup.", errors)
    sudo_path = _string(cleanup_table, "sudo_path", "cleanup.", errors)
    process_names_raw = cleanup_table.get("process_names")
    process_names: list[str] = []
    if not isinstance(process_names_raw, list) or not process_names_raw:
        errors.append("cleanup.process_names 必须是非空字符串数组")
    else:
        for index, item in enumerate(process_names_raw):
            if not isinstance(item, str) or not _PROCESS_NAME_RE.fullmatch(item):
                errors.append(f"cleanup.process_names[{index}] 必须是合法进程名")
            elif item not in process_names:
                process_names.append(item)
    clean_orphans_on_startup = cleanup_table.get("clean_orphans_on_startup", True)
    if not isinstance(clean_orphans_on_startup, bool):
        errors.append("cleanup.clean_orphans_on_startup 必须是 true 或 false")
        clean_orphans_on_startup = False

    # logging
    log_level = _string(logging_table, "level", "logging.", errors).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        errors.append("logging.level 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
    access_log = _boolean(logging_table, "access_log", "logging.", errors)

    # storage
    state_file_raw = storage_table.get("policy_state_file", "")
    state_file: Path | None = None
    if not isinstance(state_file_raw, str):
        errors.append("storage.policy_state_file 必须是字符串")
    elif state_file_raw.strip():
        state_file = Path(state_file_raw).expanduser()
        if not state_file.is_absolute():
            state_file = (config_path.parent / state_file).resolve()

    reaper_interval_seconds = _integer(
        reaper_table, "interval_seconds", "reaper.", errors, minimum=1, maximum=3_600
    )

    if errors:
        raise ConfigError(config_path, errors)

    return AppConfig(
        path=config_path,
        server=ServerConfig(
            listen_host=listen_host,
            port=port,
            timezone=timezone_name,
        ),
        access=AccessConfig(
            keys=keys,
            session_ttl_seconds=session_ttl_seconds,
            cookie_secure=cookie_secure,
            login_rate_limit=login_limit,
        ),
        tunnel=TunnelConfig(
            user=user,
            server_host=server_host,
            server_port=server_port,
            port_start=port_start,
            port_end=port_end,
            default_ttl_seconds=default_ttl_seconds,
            allowed_ttl_seconds=tuple(allowed_ttls),
            dedupe_window_seconds=dedupe_window_seconds,
            expired_retention_seconds=expired_retention_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
            listen_mode=listen_mode,
            specific_listen_ip=specific_listen_ip,
            public_host=public_host,
            keepalive_interval_seconds=keepalive_interval_seconds,
            keepalive_count_max=keepalive_count_max,
        ),
        application_limit=application_limit,
        status_limit=status_limit,
        poll_interval_seconds=poll_interval_seconds,
        reaper_interval_seconds=reaper_interval_seconds,
        cleanup=CleanupConfig(
            strategy=strategy,
            helper_path=helper_path,
            sudo_path=sudo_path,
            process_names=tuple(process_names),
            clean_orphans_on_startup=clean_orphans_on_startup,
        ),
        logging=LoggingConfig(level=log_level, access_log=access_log),
        storage=StorageConfig(policy_state_file=state_file),
    )
