"""Optional JSON persistence for the administrator-selected listen policy.

This is deliberately not a database.  Tunnel records stay in memory; the only
thing worth surviving a restart is the policy selected in the management page,
because it changes command generation for future tunnels.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import ListenMode, ListenPolicy, PolicyPersistenceError, utcnow

logger = logging.getLogger(__name__)


class PolicyStateStore:
    def __init__(self, path: Path | None):
        self.path = path

    def load(self) -> ListenPolicy | None:
        if self.path is None:
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("无法读取监听策略快照 %s：%s；将使用配置文件默认值", self.path, exc)
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("根节点不是对象")
            mode = ListenMode(str(data.get("mode", "")))
            specific = data.get("specific_ip")
            if specific is not None and not isinstance(specific, str):
                raise ValueError("specific_ip 类型错误")
            if mode is ListenMode.SPECIFIC and not specific:
                raise ValueError("specific 模式缺少 specific_ip")
            return ListenPolicy(mode, specific if mode is ListenMode.SPECIFIC else None)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "监听策略快照 %s 无效（%s）；将使用配置文件默认值", self.path, exc
            )
            return None

    def save(self, policy: ListenPolicy) -> None:
        if self.path is None:
            return
        payload = {
            "version": 1,
            "mode": policy.mode.value,
            "specific_ip": policy.specific_ip,
            "updated_at": utcnow().astimezone(timezone.utc).isoformat(),
        }
        parent = self.path.parent
        temp_name = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=parent, delete=False, prefix=".policy-"
            ) as handle:
                temp_name = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except OSError as exc:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise PolicyPersistenceError(f"无法保存监听策略快照：{exc}") from exc
