"""人类闸门 —— S1。

设计 §3.4 / §9b.3。三条出口：

    kopt probe approve    原样冻结
    kopt probe amend      人工修正，带署名与理由
    kopt probe reject     推翻重探

关键设计：批准不只是放行，它产出一个**冻结的、带哈希的工件** `env.lock.json`，
而这个哈希会进入之后每一次测量的身份。

状态**落盘**（`gate.json`），不寄存在宿主的一次性审批服务上 ——
DSH 的 `ctx.approval` 只在 open turn 内有效、没有授权存储，会话一恢复就全丢。
它只负责「你点了一下」这个不可伪造的动作，状态在这里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .errors import GateError
from .identity import canonical_json, digest_obj, short
from .probe import Fact
from .store import utc_now
from .verdicts import Confidence, GateState


@dataclass
class Gate:
    state: GateState
    facts: list[Fact] = field(default_factory=list)
    report_path: str | None = None
    report_digest: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    note: str | None = None
    env_lock_digest: str | None = None

    # ---- 持久化 ----

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "report_path": self.report_path,
            "report_digest": self.report_digest,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "note": self.note,
            "env_lock_digest": self.env_lock_digest,
            "facts": [f.as_dict() for f in self.facts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Gate":
        facts = [
            Fact(
                key=f["key"],
                value=f.get("value"),
                confidence=Confidence(f.get("confidence", "unknown")),
                source=f.get("source", ""),
                excerpt=f.get("excerpt", ""),
                exit_code=f.get("exit_code"),
                origin=f.get("origin", "probe"),
                why=f.get("why"),
            )
            for f in data.get("facts", [])
        ]
        return cls(
            state=GateState(data.get("state", "absent")),
            facts=facts,
            report_path=data.get("report_path"),
            report_digest=data.get("report_digest"),
            decided_at=data.get("decided_at"),
            decided_by=data.get("decided_by"),
            note=data.get("note"),
            env_lock_digest=data.get("env_lock_digest"),
        )


def load_gate(path) -> Gate:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return Gate(state=GateState.ABSENT)
    return Gate.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_gate(path, gate: Gate) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(canonical_json(gate.as_dict()) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 三个出口


def approve(gate: Gate, *, by: str = "user", note: str | None = None) -> tuple[Gate, dict[str, Any]]:
    """批准并冻结 env.lock。返回 (新闸门, env_lock 内容)。

    冻结的不是「刚才屏幕上那些字」，而是**事实表的规范化 JSON**。
    所以批准之后，报告文件被谁改过都不影响冻结结果；
    要改事实，只能走 amend 或 reject，两者都留痕。
    """
    if gate.state is GateState.ABSENT:
        raise GateError("nothing to approve: run `kopt probe` first")

    facts = [f for f in gate.facts]
    if not facts:
        raise GateError("gate has no facts; re-run `kopt probe`")

    payload = {
        "schema": "kopt/env-lock/v1",
        "harness_version": __version__,
        "frozen_at": utc_now(),
        "approved_by": by,
        "note": note,
        "report_path": gate.report_path,
        "report_digest": gate.report_digest,
        "facts": [f.as_dict() for f in facts],
        "values": {f.key: f.value for f in facts},
    }
    env_digest = digest_obj(payload)
    payload["digest"] = env_digest

    gate.state = GateState.APPROVED
    gate.decided_at = utc_now()
    gate.decided_by = by
    gate.note = note
    gate.env_lock_digest = env_digest
    return gate, payload


def amend(gate: Gate, key: str, value: Any, *, why: str,
          by: str = "user") -> Gate:
    """逐字段人工修正。每条修正都标 `source: human` 且理由必填。"""
    if not why or not why.strip():
        raise GateError("amend requires --why: a human edit without a stated "
                        "reason is indistinguishable from a guess")
    if gate.state is GateState.APPROVED:
        raise GateError(
            "env.lock is already frozen. Amendments after approval would "
            "silently change the measurement identity — re-run `kopt probe` "
            "and approve again instead."
        )
    if gate.state is GateState.ABSENT:
        raise GateError("nothing to amend: run `kopt probe` first")

    found = False
    for f in gate.facts:
        if f.key == key:
            f.value = value
            f.confidence = Confidence.DOCUMENTED
            f.origin = "human"
            f.why = why
            f.source = "human amendment"
            found = True
    if not found:
        known = ", ".join(x.key for x in gate.facts)
        raise GateError(f"unknown fact '{key}'. Known: {known}")
    gate.state = GateState.PENDING
    return gate


def reject(gate: Gate, *, why: str, by: str = "user") -> Gate:
    if not why or not why.strip():
        raise GateError("reject requires --why")
    gate.state = GateState.REJECTED
    gate.decided_at = utc_now()
    gate.decided_by = by
    gate.note = why
    gate.env_lock_digest = None
    return gate


def require_approved(gate: Gate) -> str:
    """冻结的闸门哈希。没批准就抛 —— 这是设计 §3 的核心闭锁。"""
    if gate.state is not GateState.APPROVED:
        raise GateError(
            f"environment gate is '{gate.state.value}'. The optimization "
            f"pipeline does not start until a human approves the recon report "
            f"(`kopt probe approve`)."
        )
    if not gate.env_lock_digest:
        raise GateError("gate is approved but has no env_lock digest — "
                        "state is corrupt, re-run `kopt probe`")
    return gate.env_lock_digest
