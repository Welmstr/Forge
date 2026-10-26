"""判决 —— S10。由程序给出，不由 LLM 给出。

设计 §5.2 / 07 定律 1：

    「凡是 agent 说它成功了的地方，六个仓库里没有一个把它当结论。」

这里的每一条分支都对应一份设计文档里的原话，改动前先回去读。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .bench import BenchReport
from .spec import Spec
from .verdicts import PerfVerdict


@dataclass
class Adjudication:
    verdict: PerfVerdict
    reason: str
    accept_threshold: float
    interval_low: float | None = None
    interval_high: float | None = None
    point: float | None = None
    # 「无法分辨」档：既不是正也不是负，在排行里单独占中间位。
    rank_tier: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "accept_threshold": self.accept_threshold,
            "point": self.point,
            "ci_low": self.interval_low,
            "ci_high": self.interval_high,
            "rank_tier": self.rank_tier,
        }

    def render(self) -> str:
        base = f"ADJUDICATE {self.verdict.value}"
        if self.point is not None:
            base += (f"  {self.point:.4f}x "
                     f"CI [{self.interval_low:.4f}, {self.interval_high:.4f}]")
        base += f"\n  reason: {self.reason}"
        if self.verdict.requires_prose:
            base += f"\n  prose required → {self.verdict.prose_dir}/"
        return base


def accept_threshold(spec: Spec, calibration: dict[str, Any] | None) -> float:
    """接受阈值 = max(题目要求的最小加速, 1 + k·σ_AA)。

    设计 §5.4。k 取 1：A/A 标定给出的 CI 半宽已经在 1.6σ 左右，
    用 1σ 作为「效应量必须超过噪声」的底线，比拍一个 1.005 要诚实。
    """
    floor_target = spec.min_speedup
    if calibration and calibration.get("usable"):
        sigma = float(calibration["sigma"])
        return max(floor_target, float(1.0 + sigma))
    return floor_target


def adjudicate(
    spec: Spec,
    bench: BenchReport | None,
    *,
    calibration: dict[str, Any] | None = None,
    timing_consistent: bool = True,
    verify_passed: bool | None = None,
) -> Adjudication:
    """给出性能判决。

    `timing_consistent` 对应 04_AKA §4 的 `benchmark_method_consistent`：
    缺失或 false ⇒ 判废（INVALID），**不拿平均时间反推**。
    """
    thr = accept_threshold(spec, calibration)

    # ---- 1. 这次比较本身有效吗 ----
    if bench is None or bench.interval is None:
        return Adjudication(PerfVerdict.INVALID,
                            "no measurement interval was produced",
                            thr, rank_tier=PerfVerdict.INVALID.rank_tier)

    if not timing_consistent:
        return Adjudication(
            PerfVerdict.INVALID,
            "benchmark_method_consistent is false: the timing method could not "
            "be certified. A slower-but-consistent number is not recoverable "
            "by averaging — see 04_AKA §4.",
            thr, rank_tier=PerfVerdict.INVALID.rank_tier,
        )

    if bench.pairs_used < spec.bench.min_pairs:
        return Adjudication(
            PerfVerdict.INVALID,
            f"only {bench.pairs_used} pairs were run; the spec requires at "
            f"least {spec.bench.min_pairs}",
            thr, rank_tier=PerfVerdict.INVALID.rank_tier,
        )

    i = bench.interval
    if not all(map(lambda x: x == x and abs(x) != float("inf"),
                   (i.point, i.low, i.high))):
        return Adjudication(PerfVerdict.INVALID,
                            "the interval contains a non-finite value",
                            thr, rank_tier=PerfVerdict.INVALID.rank_tier)

    if verify_passed is False:
        # 算错的东西快不快没有意义；而且「算错了所以快」是常见的加速来源。
        return Adjudication(
            PerfVerdict.INVALID,
            "accuracy verification did not pass; performance is not evaluated "
            "for an incorrect kernel (design §2: S8 gates S9)",
            thr, point=i.point, interval_low=i.low, interval_high=i.high,
            rank_tier=PerfVerdict.INVALID.rank_tier,
        )

    # ---- 2. 大得不像话的加速比 ⇒ 多半算少了 ----
    # 02_AKO4ALL §3：speedup > 10x 告警。
    if i.point > spec.bench.suspect_speedup:
        return Adjudication(
            PerfVerdict.SUSPECT,
            f"{i.point:.2f}x exceeds suspect_speedup={spec.bench.suspect_speedup:g}; "
            f"this is far more often an under-compute than a real win. "
            f"Accuracy must be re-verified before this can be accepted.",
            thr, point=i.point, interval_low=i.low, interval_high=i.high,
            rank_tier=PerfVerdict.SUSPECT.rank_tier,
        )

    # ---- 3. 正常判决 ----
    if i.low >= thr:
        return Adjudication(
            PerfVerdict.ACCEPT,
            f"CI lower bound {i.low:.4f} clears the accept threshold {thr:.4f}",
            thr, point=i.point, interval_low=i.low, interval_high=i.high,
            rank_tier=PerfVerdict.ACCEPT.rank_tier,
        )

    if i.high <= 1.0:
        return Adjudication(
            PerfVerdict.REJECT,
            f"CI upper bound {i.high:.4f} is at or below 1.0: the candidate is "
            f"not faster, and the interval rules out a win",
            thr, point=i.point, interval_low=i.low, interval_high=i.high,
            rank_tier=PerfVerdict.REJECT.rank_tier,
        )

    # CI 跨过 1（或跨越阈值但下界没到）：分辨不出。
    # 06_Xe-Forge §7：它既不等于最快，也不等于最差，必须单独占一档。
    return Adjudication(
        PerfVerdict.INDISTINGUISHABLE,
        f"CI [{i.low:.4f}, {i.high:.4f}] spans the decision line at {thr:.4f}: "
        f"this run cannot tell the two apart. Not a win, not a loss.",
        thr, point=i.point, interval_low=i.low, interval_high=i.high,
        rank_tier=PerfVerdict.INDISTINGUISHABLE.rank_tier,
    )
