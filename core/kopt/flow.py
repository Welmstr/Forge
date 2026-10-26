"""流程控制 —— S4~S12 的闭锁规则。

设计 §2 / §7.5。这里只放「哪些动作现在不被允许」的判断，不放业务逻辑。

最硬的一条是 §7.5 的散文闭锁：

    上一轮判决尚未归档散文 ⇒ `kopt propose` 直接拒绝执行。

这抄的是 02_AKO4ALL §2「迭代 → ITERATIONS.md → git commit 三步必须连号」，
但做成了程序闭锁而不是 prompt 嘱托 —— 嘱托会被遗忘，闭锁不会。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import KoptError
from .store import Ledger
from .verdicts import PerfVerdict, VerifyVerdict


@dataclass
class PendingObligation:
    run_id: str
    verdict: PerfVerdict
    expected_dir: str

    def render(self) -> str:
        return (
            f"run {self.run_id} was adjudicated {self.verdict.value} but has no "
            f"archived prose in {self.expected_dir}/. Writing it is a closure "
            f"condition, not a courtesy — see design §7.5."
        )


def prose_path_exists(project_root: Path, run_id: str) -> bool:
    """这一轮的报告是否已经归档。

    按 run_id 前缀找文件：`docs/opt/**/<run_id>-*.md`。
    只看文件名，不读内容 —— 内容是否合格由 doc-gate 负责。
    """
    for base in ("docs/opt", "docs/opt/rejected"):
        d = project_root / base
        if not d.is_dir():
            continue
        for p in d.rglob(f"{run_id}-*.md"):
            if p.is_file():
                return True
    return False


def pending_obligations(project_root: str | Path, ledger: Ledger) -> list[PendingObligation]:
    """列出「判决要求写散文、但还没写」的轮次。

    只认最后一次出现过的判决：同一 run 若后来补了散文，就不再是欠账。
    """
    root = Path(project_root)
    latest: dict[str, PerfVerdict] = {}
    for row in ledger.read():
        if row.get("event") != "adjudicate":
            continue
        run_id = row.get("run_id")
        verdict = row.get("verdict")
        if not run_id or not verdict:
            continue
        try:
            latest[run_id] = PerfVerdict(verdict)
        except ValueError:
            continue

    out: list[PendingObligation] = []
    for run_id, verdict in latest.items():
        if not verdict.requires_prose:
            continue
        if prose_path_exists(root, run_id):
            continue
        out.append(PendingObligation(run_id, verdict, verdict.prose_dir or ""))
    return sorted(out, key=lambda o: o.run_id)


def require_prose_clear(project_root: str | Path, ledger: Ledger) -> None:
    """`kopt propose` 的入口闸。有欠账就拒绝。"""
    pend = pending_obligations(project_root, ledger)
    if pend:
        body = "\n".join("  - " + p.render() for p in pend)
        raise KoptError(
            "refusing to start a new round: previous results are not archived.\n"
            + body
        )


def next_run_id(project_root: str | Path, ledger: Ledger) -> str:
    """顺序号 rNNNN。从流水账里数已有的 run，避免依赖文件系统状态。"""
    seen: set[str] = set()
    for row in ledger.read():
        rid = row.get("run_id")
        if isinstance(rid, str) and rid.startswith("r"):
            seen.add(rid)
    n = 1
    while f"r{n:04d}" in seen:
        n += 1
    return f"r{n:04d}"


def load_json(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


__all__ = [
    "PendingObligation",
    "pending_obligations",
    "require_prose_clear",
    "prose_path_exists",
    "next_run_id",
    "VerifyVerdict",
]
