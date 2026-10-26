"""项目布局与受保护路径。

设计 §8 / §9：agent 只能改内核，不能改测量器（07 定律 4）。

受保护集合**按命名模式定义，不只按目录**（04_AKA §3）：
`*_test.py`、`*_harness.py`、`conftest.py` 这类文件无论放在哪都受保护。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .errors import ProtectedPathError

# 只读目录（相对 projectRoot）
PROTECTED_DIRS = (
    "harness",
    "reference",
    "receipts",
)

# 只读文件
PROTECTED_FILES = (
    "spec.toml",
    "env.lock.json",
    "gate.json",
)

# 无论放在哪都受保护的命名模式（04_AKA §3）
PROTECTED_PATTERNS = (
    "*_test.py",
    "*_harness.py",
    "conftest.py",
    "bench*.py",
    "*_bench.py",
)

# agent 可以自由写的地方
WRITABLE_DIRS = (
    "work/candidate",
    "scratch",
)

# 只能经 CLI 追加的地方
APPEND_ONLY_DIRS = (
    "docs/opt",
    "docs/recon",
    "journal",
    "knowledge",
)


@dataclass(frozen=True)
class Project:
    root: Path

    @classmethod
    def at(cls, root: str | Path) -> "Project":
        return cls(Path(root).resolve())

    # ---- 目录 ----
    @property
    def spec_path(self) -> Path:
        return self.root / "spec.toml"

    @property
    def env_lock(self) -> Path:
        return self.root / "env.lock.json"

    @property
    def env_lock_draft(self) -> Path:
        return self.root / "env.lock.draft.json"

    @property
    def gate_path(self) -> Path:
        return self.root / "gate.json"

    @property
    def candidate_dir(self) -> Path:
        return self.root / "work" / "candidate"

    @property
    def docs_opt(self) -> Path:
        return self.root / "docs" / "opt"

    @property
    def docs_opt_rejected(self) -> Path:
        return self.root / "docs" / "opt" / "rejected"

    @property
    def docs_recon(self) -> Path:
        return self.root / "docs" / "recon"

    def ensure_dirs(self) -> None:
        for rel in ("work/candidate", "scratch", "receipts", "journal",
                    "knowledge", "docs/opt/rejected", "docs/recon"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)

    # ---- 写权限裁决 ----
    def classify(self, path: str | Path) -> str:
        """返回 'writable' | 'append-only' | 'protected' | 'outside'。"""
        p = Path(path).resolve()
        try:
            rel = p.relative_to(self.root).as_posix()
        except ValueError:
            return "outside"

        name = p.name
        for pat in PROTECTED_PATTERNS:
            if fnmatch.fnmatch(name, pat):
                return "protected"
        if rel in PROTECTED_FILES:
            return "protected"
        for d in PROTECTED_DIRS:
            if rel == d or rel.startswith(d + "/"):
                return "protected"
        for d in WRITABLE_DIRS:
            if rel == d or rel.startswith(d + "/"):
                return "writable"
        for d in APPEND_ONLY_DIRS:
            if rel == d or rel.startswith(d + "/"):
                return "append-only"
        return "protected"

    def assert_writable(self, path: str | Path) -> None:
        kind = self.classify(path)
        if kind != "writable":
            raise ProtectedPathError(
                f"{path} is {kind}; agents may only write under "
                f"{', '.join(WRITABLE_DIRS)}"
            )
