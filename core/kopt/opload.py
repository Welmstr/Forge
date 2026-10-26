"""算子模块的加载与契约。

这是 `01_KernelAgent` §1 规范 1 说的「把可替换点写成抽象基类」里最关键的一道接缝：
换硬件 / 换算子语言，只需要换这里的加载与调用方式，上层状态机一行不动。

契约（刻意做成不对称的）：

    参考实现 reference.py   必须有 make_inputs(shape, dtype, seed, params)
                            必须有 run(*inputs, **params)

    候选实现 candidate.py   必须有 run(*inputs, **params)
                            **不许有 make_inputs**

最后一条来自 02_AKO4ALL §3：`solution/` 不许包含 `get_inputs`，
否则候选可以自造输入骗过分。这里把它从「prompt 里的禁令」变成「加载期的硬错误」。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import numpy as np

from .errors import KoptError
from .spec import DTYPES, Case

# 候选模块里出现任何一个都视为作弊入口
FORBIDDEN_IN_CANDIDATE = ("make_inputs", "get_inputs", "generate_inputs")


def np_dtype(name: str) -> np.dtype:
    """spec 里的 dtype 名 → numpy dtype。bf16 没有原生支持，显式拒绝而不是悄悄降级。"""
    canon = DTYPES.get(name)
    if canon is None:
        raise KoptError(f"unknown dtype: {name}")
    if canon == "bfloat16":
        raise KoptError(
            "bfloat16 has no native numpy dtype; supply a backend that "
            "implements it rather than silently falling back to float32"
        )
    return np.dtype(canon)


def _load_module(path: Path, alias: str) -> ModuleType:
    if not path.exists():
        raise KoptError(f"module not found: {path}")
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise KoptError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    # 用唯一别名注册，避免候选和参考实现同名互相覆盖。
    sys.modules[alias] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 - 原样上报，不吞
        raise KoptError(f"failed to import {path}: {exc!r}") from exc
    return mod


@dataclass
class Reference:
    """参考实现：既造输入，也给正确答案。受保护，agent 不可写。"""

    path: Path
    module: ModuleType
    entry: str

    @classmethod
    def load(cls, path: str | Path, entry: str = "run") -> "Reference":
        p = Path(path)
        mod = _load_module(p, f"_kopt_ref_{abs(hash(str(p)))}")
        for fn in ("make_inputs", entry):
            if not callable(getattr(mod, fn, None)):
                raise KoptError(
                    f"reference {p} must define a callable '{fn}'"
                )
        return cls(p, mod, entry)

    def make_inputs(self, case: Case, seed: int) -> tuple[np.ndarray, ...]:
        dt = np_dtype(case.dtype)
        out = self.module.make_inputs(
            shape=tuple(case.shape),
            dtype=dt,
            seed=int(seed),
            params=dict(case.params),
        )
        if isinstance(out, np.ndarray):
            out = (out,)
        if not isinstance(out, tuple):
            out = tuple(out)
        return out

    @property
    def run(self) -> Callable[..., Any]:
        return getattr(self.module, self.entry)


@dataclass
class Candidate:
    """候选实现：只许算，不许造输入。"""

    path: Path
    module: ModuleType
    entry: str

    @classmethod
    def load(cls, path: str | Path, entry: str = "run") -> "Candidate":
        p = Path(path)
        mod = _load_module(p, f"_kopt_cand_{abs(hash(str(p)))}")
        if not callable(getattr(mod, entry, None)):
            raise KoptError(f"candidate {p} must define a callable '{entry}'")
        for bad in FORBIDDEN_IN_CANDIDATE:
            if hasattr(mod, bad):
                raise KoptError(
                    f"candidate {p} defines '{bad}'. A candidate that can "
                    f"manufacture its own inputs can pass verification without "
                    f"being correct — see 02_AKO4ALL §3. Remove it."
                )
        return cls(p, mod, entry)

    @property
    def run(self) -> Callable[..., Any]:
        return getattr(self.module, self.entry)
