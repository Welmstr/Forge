"""程序侧的日志 —— 事件流，不是知识。

设计依据：`forge-kb-design.md` §13（本轮新增）。

和知识库的分工（这是整个模块存在的理由）：

    知识 = 通过准入的结论，要求「对」     → Forge-KnowledgeHub
    日志 = 全部事件，要求「全且不可改」   → Forge-Logs

所以这里**不做过闸**。一次被拒的 verify、一次参数写错的调用、
一次被 guard 挡下的写入，都是要记的——那正是调试时需要的东西。

三条硬规矩：

  1. **只有程序写。** agent 不能调用本模块。它写不了自己要被检查的东西，
     日志才有可能用来检查它。agent 只能通过 `forge_note` 追加一行备注，
     而且那行会明确标成 `author=agent`。
  2. **只追加。** 打开方式是 `ab`，不改旧行。日志文件在 git 里是追加历史，
     不是可编辑文档。
  3. **写不进日志不许静默吞掉。** 落盘失败要抛错——一份悄悄断掉的日志，
     比没有日志更危险，因为它看起来是完整的。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class EventLog:
    """一个会话一个目录，两种事件流，外加一个身份清单。

    目录形状（和 Forge-Logs 仓库的约定一致）：

        <logroot>/<yyyy>/<mm>/<dd>/<session_id>/
            manifest.json    这个会话是谁、跑在哪、工具清单什么样
            calls.jsonl      每次工具调用（agent 发起的）
            actions.jsonl    每次受约束的动作（guard / 写路径 / 闸门拒绝）
            notes.md         agent 自己的备注，标 author=agent
    """

    root: Path
    session_id: str
    agent: str = "unknown"

    _dir: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc)
        self._dir = (self.root / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
                     / self.session_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        manifest = self._dir / "manifest.json"
        if not manifest.exists():
            self._write_manifest(manifest)

    @property
    def directory(self) -> Path:
        return self._dir

    def _write_manifest(self, path: Path) -> None:
        payload = {
            "schema": "forge/log/v1",
            "session_id": self.session_id,
            "agent": self.agent,
            "opened_at": _utc_now(),
            "host": _host(),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ---- 两条事件流 ----

    def call(self, **fields: Any) -> None:
        """记一次工具调用。谁、调了什么、参数、退出码、多久。"""
        self._append("calls.jsonl", {"event": "tool_call", **fields})

    def action(self, kind: str, **fields: Any) -> None:
        """记一次受约束的动作。被拒绝的也算——拒绝本身是最有价值的一类。"""
        self._append("actions.jsonl", {"event": kind, **fields})

    def note(self, text: str) -> Path:
        """agent 的备注。**独立文件、明确署名**，混不进上面两条流。"""
        path = self._dir / "notes.md"
        stamp = _utc_now()
        header = "" if path.exists() else (
            "# 备注（author=agent）\n\n"
            "> 以下内容由 agent 自己写，程序未校验其真实性。\n"
            "> 判断 agent 是否按预期工作，请以 calls.jsonl / actions.jsonl 为准。\n\n")
        body = f"{header}- `{stamp}` {text.strip()}\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        row = {"at": _utc_now(), "session": self.session_id, **payload}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        try:
            with (self._dir / name).open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # 规矩 3：不许静默丢日志。
            raise RuntimeError(
                f"forge log: cannot append to {self._dir / name}: {exc}. "
                f"A log that silently stops is worse than no log.") from exc


def _host() -> str:
    try:
        import platform
        return f"{platform.node()} · {platform.system()} {platform.release()}"
    except Exception:  # noqa: BLE001 - 记日志失败不该让主流程崩
        return "unknown"
