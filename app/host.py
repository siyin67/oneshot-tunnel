"""Read-only inspection of the Linux host network namespace.

The container is expected to run with host networking.  In that mode
``/proc/net/tcp`` and ``/proc/net/tcp6`` describe the host listening sockets,
and reading them works without any capabilities.  We deliberately use these
files instead of trying to bind probe sockets: binding cannot reliably test
dual-stack or every interface, and it would race with the incoming tunnel.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from pathlib import Path
from typing import Protocol

from .models import HostProbeError

_TCP_LISTEN = "0A"
_IPV4_TOKEN = re.compile(r"^[0-9A-Fa-f]{8}$")
_IPV6_TOKEN = re.compile(r"^[0-9A-Fa-f]{32}$")


class PortInspector(Protocol):
    def listening_ports(self) -> dict[int, tuple[str, ...]]:
        """Return every TCP port in LISTEN state, mapped to its addresses."""

    def listening_addresses(self, port: int) -> tuple[str, ...]:
        ...


class InterfaceInspector(Protocol):
    def addresses(self) -> frozenset[str]:
        ...


class ProcNetPortInspector:
    """Inspect listening TCP sockets through the kernel's procfs tables."""

    def __init__(
        self,
        tcp_path: str | Path = "/proc/net/tcp",
        tcp6_path: str | Path = "/proc/net/tcp6",
    ):
        self.tcp_path = Path(tcp_path)
        self.tcp6_path = Path(tcp6_path)

    @staticmethod
    def _ipv4_from_hex(token: str) -> str:
        raw = bytes.fromhex(token)
        return socket.inet_ntoa(raw[::-1])

    @staticmethod
    def _ipv6_from_hex(token: str) -> str:
        raw = bytes.fromhex(token)
        # Each 32-bit word is stored in host byte order (little-endian on the
        # platforms the service targets), so reverse within each word.
        network_order = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, network_order)

    def _read_table(
        self, path: Path, ipv6: bool, result: dict[int, set[str]]
    ) -> bool:
        try:
            with path.open("r", encoding="ascii", errors="replace") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise HostProbeError(f"无法读取宿主机端口表 {path}: {exc}") from exc
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _TCP_LISTEN:
                continue
            local = fields[1]
            if ":" not in local:
                continue
            address_token, port_token = local.rsplit(":", 1)
            try:
                port = int(port_token, 16)
            except ValueError:
                continue
            if not 0 < port <= 65_535:
                continue
            try:
                if ipv6:
                    if not _IPV6_TOKEN.fullmatch(address_token):
                        continue
                    address = self._ipv6_from_hex(address_token)
                else:
                    if not _IPV4_TOKEN.fullmatch(address_token):
                        continue
                    address = self._ipv4_from_hex(address_token)
            except (ValueError, OSError):
                continue
            # ::ffff:127.0.0.1 is easier to display as the IPv4 address.
            if address.lower().startswith("::ffff:"):
                mapped = address.rsplit(":", 1)[1]
                try:
                    address = str(ipaddress.IPv4Address(mapped))
                except ValueError:
                    pass
            result.setdefault(port, set()).add(address)
        return True

    def listening_ports(self) -> dict[int, tuple[str, ...]]:
        result: dict[int, set[str]] = {}
        found_ipv4 = self._read_table(self.tcp_path, False, result)
        found_ipv6 = self._read_table(self.tcp6_path, True, result)
        if not found_ipv4 and not found_ipv6:
            raise HostProbeError(
                "无法读取 /proc/net/tcp 或 /proc/net/tcp6；服务必须运行在 Linux "
                "且使用宿主机网络命名空间（Docker network_mode: host）"
            )
        return {port: tuple(sorted(addresses)) for port, addresses in result.items()}

    def listening_addresses(self, port: int) -> tuple[str, ...]:
        return self.listening_ports().get(port, ())


class IpCommandInterfaceInspector:
    """List interface addresses by invoking the host's iproute2 ``ip`` tool."""

    def __init__(self, command: str = "ip", timeout_seconds: int = 5):
        self.command = command
        self.timeout_seconds = timeout_seconds

    def addresses(self) -> frozenset[str]:
        try:
            completed = subprocess.run(
                [self.command, "-o", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env={"LC_ALL": "C", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            )
        except FileNotFoundError as exc:
            raise HostProbeError(
                "找不到 ip 命令；请确认容器内安装了 iproute2，且服务使用宿主机网络命名空间"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HostProbeError("读取宿主机网卡地址超时") from exc
        except OSError as exc:
            raise HostProbeError(f"无法调用 ip 命令: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()[:300]
            raise HostProbeError(f"读取宿主机网卡地址失败（ip 返回 {completed.returncode}）: {detail}")
        found: set[str] = set()
        for token in completed.stdout.split():
            if "/" not in token:
                continue
            address_part = token.split("/", 1)[0]
            if "%" in address_part:
                address_part = address_part.split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(address_part)
            except ValueError:
                continue
            found.add(str(ip))
        if not found:
            raise HostProbeError("未能从 ip 命令输出中解析出任何网卡地址")
        return frozenset(found)


class FakePortInspector:
    """Small in-memory inspector for local development and tests."""

    def __init__(self, ports: set[int] | dict[int, tuple[str, ...]] | None = None):
        if isinstance(ports, dict):
            self._ports = {port: tuple(addrs) for port, addrs in ports.items()}
        else:
            self._ports = {port: ("127.0.0.1",) for port in (ports or set())}

    def set_listening(self, port: int, addresses: tuple[str, ...] = ("127.0.0.1",)) -> None:
        self._ports[port] = addresses

    def set_closed(self, port: int) -> None:
        self._ports.pop(port, None)

    def listening_ports(self) -> dict[int, tuple[str, ...]]:
        return dict(self._ports)

    def listening_addresses(self, port: int) -> tuple[str, ...]:
        return self._ports.get(port, ())


class FakeInterfaceInspector:
    def __init__(self, addresses: set[str] | None = None):
        self._addresses = frozenset(addresses or {"127.0.0.1"})

    def addresses(self) -> frozenset[str]:
        return self._addresses
