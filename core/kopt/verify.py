"""精度验证模块 —— S8，硬门。

设计 §4。核心规矩：

* 阈值只从 spec 来（04 定律 / 03_atrex §6「规划器不能提供阈值」）。
* 每次重新生成输入，禁止复用（02_AKO4ALL §3）。
* 一个 case 失败即整体 FAIL（03_atrex §6：一个完整的失败工作量即可证伪）。
* 形状/dtype 没有覆盖 ⇒ UNSUPPORTED，不是 PASS（03_atrex §6 / 07 定律 3）。
* 执行抛异常 ⇒ INCONCLUSIVE，不是 FAIL —— 没拿到结论就是没拿到结论。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import UnsupportedError
from .metrics import Deviation, compare, perturbation
from .opload import Candidate, Reference, np_dtype
from .spec import Case, Spec, Tolerance
from .verdicts import VerifyVerdict


@dataclass
class CaseResult:
    case: Case
    seed: int
    verdict: VerifyVerdict
    deviation: Deviation | None = None
    error: str | None = None
    input_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.as_dict(),
            "seed": self.seed,
            "verdict": self.verdict.value,
            "deviation": self.deviation.as_dict() if self.deviation else None,
            "error": self.error,
            "input_fingerprint": self.input_fingerprint,
        }


@dataclass
class VerifyReport:
    verdict: VerifyVerdict
    tolerance: Tolerance | None
    results: list[CaseResult] = field(default_factory=list)
    sentinel: dict[str, Any] | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "tolerance": self.tolerance.as_dict() if self.tolerance else None,
            "sentinel": self.sentinel,
            "results": [r.as_dict() for r in self.results],
        }

    # 给模型看的渲染：短、明确、不带情绪
    def render(self) -> str:
        lines = [f"VERIFY {self.verdict.value}"]
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        if self.tolerance:
            t = self.tolerance
            lines.append(f"  threshold: {t.metric} <= {t.threshold:g} ({t.dtype})")
        ok = sum(1 for r in self.results if r.verdict is VerifyVerdict.PASS)
        lines.append(f"  cases: {ok}/{len(self.results)} passed")
        # 失败时把偏差值放在最显眼的位置 —— 它才是「差多少」的答案。
        for r in self.results:
            if r.deviation and r.deviation.value is not None:
                head = ("    FAIL " if r.verdict is VerifyVerdict.FAIL else "    dev  ")
                lines.append(
                    f"{head}{r.case.label} seed={r.seed}: "
                    f"{r.deviation.metric}={r.deviation.value:.6g} "
                    f"(limit {r.deviation.threshold:g})"
                )
        lines.append("  per case·seed:")
        for r in self.results:
            tag = r.verdict.value
            if r.deviation and r.deviation.value is not None:
                detail = f"{r.deviation.metric}={r.deviation.value:.6g}"
            elif r.error:
                detail = _first_error(r.error)
            elif r.deviation and r.deviation.reason:
                detail = r.deviation.reason[:120]
            else:
                detail = ""
            lines.append(f"    [{tag:12s}] {r.case.label} seed={r.seed} {detail}")
        return "\n".join(lines)


def _first_error(exc_text: str) -> str:
    """从 traceback 里挑出真正有用的那一行。

    只看第一行会得到 "Traceback (most recent call last):" —— 那是一句废话，
    INCONCLUSIVE 的原因必须能让人直接看出哪里坏了。
    """
    lines = [ln.strip() for ln in exc_text.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not ln.startswith("^") and not ln.startswith("File "):
            return ln[:300]
    return lines[-1][:300] if lines else "unknown error"


def _fingerprint(*arrays: np.ndarray) -> str:
    """输入指纹：证明这一次的输入确实是新生成的，不是复用的。"""
    import hashlib

    h = hashlib.sha256()
    for a in arrays:
        arr = np.ascontiguousarray(a)
        h.update(str(arr.shape).encode())
        h.update(arr.dtype.str.encode())
        h.update(arr.tobytes())
    return h.hexdigest()[:16]


def run_verification(
    spec: Spec,
    reference: Reference,
    *,
    candidate: Candidate | None = None,
    run: Any = None,
    case_filter: str | None = None,
) -> VerifyReport:
    """跑一遍精度验证。

    candidate 给出时用它；否则用 `run`（用于验证参考实现自己，即基线自检）。
    """
    fn = candidate.run if candidate is not None else run
    if fn is None:
        raise ValueError("run_verification needs either candidate or run")

    cases = list(spec.cases)
    if case_filter is not None:
        cases = [c for c in cases if c.label == case_filter]
        if not cases:
            raise UnsupportedError(
                f"case '{case_filter}' is not in spec.toml; "
                f"known: {', '.join(c.label for c in spec.cases)}"
            )

    # 阈值只能从 spec 取。任何 dtype 缺容差都在这里炸，绝不兜底。
    tolerances = {c.dtype: spec.tolerance_for(c.dtype) for c in cases}

    results: list[CaseResult] = []
    sentinel_failures: list[str] = []

    for case in cases:
        tol = tolerances[case.dtype]
        for seed in spec.seeds:
            # 每个 (case, seed) 都重新造输入 —— fresh_inputs 是硬性的，
            # spec 加载期已经禁止关掉它。
            try:
                inputs = reference.make_inputs(case, seed)
            except Exception:  # noqa: BLE001
                results.append(CaseResult(
                    case, seed, VerifyVerdict.INCONCLUSIVE,
                    error="make_inputs failed:\n" + traceback.format_exc(limit=3),
                ))
                continue
            fp = _fingerprint(*inputs)

            try:
                want = np.asarray(reference.run(*inputs, **case.params))
            except Exception:  # noqa: BLE001
                results.append(CaseResult(
                    case, seed, VerifyVerdict.INCONCLUSIVE, input_fingerprint=fp,
                    error="reference run failed:\n" + traceback.format_exc(limit=3),
                ))
                continue

            # ---- 变异哨兵：扰动参考输出，度量必须报错 ----
            # 02_AKO4ALL §3：不报错说明测试根本没接上。
            mutated = perturbation(want, tol.threshold)
            sent = compare(mutated, want, metric=tol.metric, threshold=tol.threshold)
            if sent.ok:
                sentinel_failures.append(
                    f"{case.label} seed={seed}: mutation with delta≈10x threshold "
                    f"was accepted ({tol.metric}={sent.value})"
                )

            try:
                got = np.asarray(fn(*inputs, **case.params))
            except Exception:  # noqa: BLE001
                results.append(CaseResult(
                    case, seed, VerifyVerdict.INCONCLUSIVE, input_fingerprint=fp,
                    error="candidate run failed:\n" + traceback.format_exc(limit=3),
                ))
                continue

            dev = compare(got, want, metric=tol.metric, threshold=tol.threshold)
            results.append(CaseResult(
                case, seed,
                VerifyVerdict.PASS if dev.ok else VerifyVerdict.FAIL,
                deviation=dev, input_fingerprint=fp,
            ))

    sentinel = {
        "ok": not sentinel_failures,
        "failures": sentinel_failures,
    }

    if sentinel_failures:
        # 哨兵没过：整个 harness 没有被授权下任何结论。
        return VerifyReport(
            verdict=VerifyVerdict.INCONCLUSIVE,
            tolerance=None, results=results, sentinel=sentinel,
            reason="MUTATION SENTINEL FAILED — the verifier is not wired to the "
                   "comparison. No verdict can be trusted until this is fixed.",
        )

    if any(r.verdict is VerifyVerdict.INCONCLUSIVE for r in results):
        return VerifyReport(
            verdict=VerifyVerdict.INCONCLUSIVE,
            tolerance=next(iter(tolerances.values()), None),
            results=results, sentinel=sentinel,
            reason="at least one case raised; no conclusion was reached",
        )

    failed = [r for r in results if r.verdict is VerifyVerdict.FAIL]
    if failed:
        return VerifyReport(
            verdict=VerifyVerdict.FAIL,
            tolerance=next(iter(tolerances.values()), None),
            results=results, sentinel=sentinel,
            reason=f"{len(failed)}/{len(results)} case·seed pairs exceeded the "
                   f"declared tolerance",
        )

    return VerifyReport(
        verdict=VerifyVerdict.PASS,
        tolerance=next(iter(tolerances.values()), None),
        results=results, sentinel=sentinel,
    )
