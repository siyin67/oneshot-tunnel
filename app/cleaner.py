"""Terminate the sshd listener that owns a tunnel port.

The service process itself is deliberately unprivileged.  On Docker this class
uses a narrowly scoped sudo helper; outside Docker, or when running as root, it
can inspect and signal sshd directly.  Only processes whose executable name is
in ``cleanup.process_names`` and which currently own the requested listening
socket are ever signalled.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .config import CleanupConfig, TunnelConfig
from .models import CleanupResult, CleanupError

_PID_RE = re.compile(r"pid=(\d+)")


class ProcessCleaner:
    def __init__(self, config: CleanupConfig, tunnel_config: TunnelConfig):
        self.config = config
        self.tunnel_config = tunnel_config

    @staticmethod
    def _ss_command() -> str | None:
        return shutil.which("ss") or shutil.which("ss", path="/usr/bin:/usr/sbin:/bin:/sbin")

    @staticmethod
    def _local_port(field: str) -> int | None:
        # ss local address forms: 127.0.0.1:30001, [::]:30001, *:30001
        if ":" not in field:
            return None
        try:
            return int(field.rsplit(":", 1)[1])
        except ValueError:
            return None

    def _scan_port(self, port: int) -> tuple[bool, set[int], str]:
        """Return (listener_present, pids, error). Blocking; run in a thread."""
        ss = self._ss_command()
        if not ss:
            return False, set(), "找不到 ss 命令；请安装 iproute2（容器镜像已包含）"
        try:
            completed = subprocess.run(
                [ss, "-H", "-ltnp"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env={
                    "LC_ALL": "C",
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, set(), f"执行 ss 失败：{exc}"
        if completed.returncode != 0:
            return False, set(), f"ss 返回 {completed.returncode}：{completed.stderr.strip()[:200]}"
        matching = []
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            if fields[0] != "LISTEN":
                continue
            if self._local_port(fields[3]) == port:
                matching.append(line)
        if not matching:
            return False, set(), ""
        pids: set[int] = set()
        for line in matching:
            pids.update(int(value) for value in _PID_RE.findall(line))
        if not pids:
            return (
                True,
                set(),
                "端口正在监听，但当前用户无权读取监听进程信息；请使用受限 root 清理工具",
            )
        return True, pids, ""

    def _verify_allowed(self, pids: set[int]) -> str | None:
        allowed = {name.casefold() for name in self.config.process_names}
        for pid in sorted(pids):
            comm_path = Path(f"/proc/{pid}/comm")
            try:
                comm = comm_path.read_text(encoding="ascii", errors="replace").strip()
            except OSError as exc:
                return f"无法读取进程 {pid} 的名称，拒绝清理：{exc}"
            if comm.casefold() not in allowed:
                return (
                    f"端口所属进程 {pid} ({comm or '未知'}) 不在允许清理的进程名单中，"
                    "拒绝结束该进程"
                )
        return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    async def _direct_cleanup(self, port: int, timeout_seconds: int) -> CleanupResult:
        loop = asyncio.get_running_loop()
        present, pids, error = await asyncio.to_thread(self._scan_port, port)
        if error and not present:
            return CleanupResult(False, error)
        if not present:
            return CleanupResult(True, "端口未被监听，无需清理", ())
        allowed_error = await asyncio.to_thread(self._verify_allowed, pids)
        if allowed_error:
            return CleanupResult(False, allowed_error)
        if not pids:
            return CleanupResult(False, "未能识别监听该端口的 sshd 进程，拒绝盲目清理")

        killed: list[int] = []
        for pid in sorted(pids):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                continue
            except PermissionError:
                return CleanupResult(
                    False,
                    "没有权限结束宿主机 sshd 监听进程；请启用受限 root 清理工具",
                    tuple(killed),
                )
            except OSError as exc:
                return CleanupResult(False, f"向进程 {pid} 发送 SIGTERM 失败：{exc}", tuple(killed))

        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            present, remaining, scan_error = await asyncio.to_thread(self._scan_port, port)
            if scan_error:
                return CleanupResult(False, scan_error, tuple(killed))
            if not present:
                return CleanupResult(True, "已结束 sshd 隧道监听进程", tuple(killed))
            pids = remaining or pids

        # A listener that survived SIGTERM gets SIGKILL.  Re-verify the process
        # name to avoid killing a different process that grabbed the port.
        allowed_error = await asyncio.to_thread(self._verify_allowed, pids)
        if allowed_error:
            return CleanupResult(False, allowed_error, tuple(killed))
        for pid in sorted(pids):
            if self._alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    return CleanupResult(False, f"向进程 {pid} 发送 SIGKILL 失败：{exc}", tuple(killed))

        deadline = loop.time() + min(2, max(1, timeout_seconds))
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            present, _, scan_error = await asyncio.to_thread(self._scan_port, port)
            if scan_error:
                return CleanupResult(False, scan_error, tuple(killed))
            if not present:
                return CleanupResult(True, "已强制结束 sshd 隧道监听进程", tuple(killed))
        return CleanupResult(False, "清理后端口仍在监听，请检查宿主机 sshd 状态", tuple(killed))

    async def _helper_cleanup(self, port: int, timeout_seconds: int) -> CleanupResult:
        helper = Path(self.config.helper_path)
        sudo = Path(self.config.sudo_path)
        if not helper.is_file():
            return CleanupResult(False, f"受限 root 清理工具不存在：{helper}")
        if not sudo.is_file():
            return CleanupResult(False, f"sudo 不可用：{sudo}")
        try:
            completed = await asyncio.create_subprocess_exec(
                str(sudo),
                "-n",
                str(helper),
                str(port),
                str(self.tunnel_config.port_start),
                str(self.tunnel_config.port_end),
                str(timeout_seconds),
                *self.config.process_names,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                completed.communicate(), timeout=timeout_seconds + 10
            )
        except asyncio.TimeoutError:
            return CleanupResult(False, "受限 root 清理工具执行超时")
        except OSError as exc:
            return CleanupResult(False, f"无法执行受限 root 清理工具：{exc}")
        output = (stdout or b"").decode("utf-8", "replace").strip()
        error = (stderr or b"").decode("utf-8", "replace").strip()
        if completed.returncode == 0:
            message = "已通过受限 root 清理工具结束 sshd 隧道监听进程"
            if output:
                message += f"：{output[:300]}"
            return CleanupResult(True, message)
        if completed.returncode == 3:
            message = "端口已无监听，无需清理"
            if output:
                message += f"：{output[:300]}"
            return CleanupResult(True, message)
        detail = error or output or f"退出码 {completed.returncode}"
        return CleanupResult(False, f"受限 root 清理工具失败：{detail[:500]}")

    async def cleanup(self, port: int, timeout_seconds: int | None = None) -> CleanupResult:
        if not 0 < int(port) <= 65_535:
            raise CleanupError("非法端口号")
        timeout = timeout_seconds or self.tunnel_config.cleanup_timeout_seconds
        strategy = self.config.strategy
        if strategy == "direct":
            return await self._direct_cleanup(int(port), timeout)
        if strategy == "helper":
            return await self._helper_cleanup(int(port), timeout)

        # auto: root/direct first; otherwise prefer the narrowly-scoped helper,
        # then fall back to direct (useful for local development).
        euid = os.geteuid() if hasattr(os, "geteuid") else 0
        if euid == 0:
            return await self._direct_cleanup(int(port), timeout)
        helper_result = await self._helper_cleanup(int(port), timeout)
        if helper_result.success:
            return helper_result
        direct_result = await self._direct_cleanup(int(port), timeout)
        if direct_result.success:
            return direct_result
        return CleanupResult(
            False,
            f"{helper_result.message}；直连清理也失败：{direct_result.message}",
        )
