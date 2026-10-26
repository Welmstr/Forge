"""计时调度 —— ABBA 交错 + 配对 bootstrap。

设计 §5。三件事一起做：

  §5.1  不要 5 连 baseline + 5 连候选，要逐对交错并翻转顺序。
        10 次运行一次不多一次不少，代价为零，统计性质严格更好。
  §5.4  配对数由 A/A 标定决定，**下限 5**，只增不减。
  §5.2  判据是配对 bootstrap 的置信区间，不是平均值比较。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .opload import Candidate, Reference
from .spec import Spec
from .stats import Interval, paired_bootstrap
from .timing import Sample, build_sequence, flatten, paired_ratios, run_sequence
from .verdicts import PerfVerdict


@dataclass
class BenchReport:
    samples: list[Sample]
    ratios: list[float]
    interval: Interval | None
    pairs_used: int
    pairs_reason: str
    # 这里**不写 verdict** —— 判决是 adjudicate 的职责，见设计 §2 S10。
    order: list[list[str]]
    # 这一轮用的 σ。它决定了「几对能分辨这个目标」，不记下来的话，
    # 以后没人能解释为什么当时跑了 5 对而不是 9 对。
    sigma: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs_used": self.pairs_used,
            "pairs_reason": self.pairs_reason,
            "sigma": self.sigma,
            "order": self.order,
            "samples": [s.as_dict() for s in self.samples],
            "ratios": self.ratios,
            "interval": self.interval.as_dict() if self.interval else None,
        }

    def per_side_mean_ms(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for s in self.samples:
            out.setdefault(s.label, []).append(s.ms)
        return {k: float(np.mean(v)) for k, v in out.items()}

    def render(self) -> str:
        means = self.per_side_mean_ms()
        lines = [
            f"BENCH pairs={self.pairs_used} ({self.pairs_reason})",
            "  mean ms: " + ", ".join(f"{k}={v:.4f}" for k, v in means.items()),
        ]
        for s in self.samples:
            lines.append(f"    pair{s.pair} {s.label:9s} {s.ms:10.4f} ms")
        if self.interval:
            i = self.interval
            lines.append(
                f"  speedup (geomean) {i.point:.4f}  "
                f"{i.confidence*100:.0f}% CI [{i.low:.4f}, {i.high:.4f}]"
            )
        lines.append("  per-pair ratios: "
                     + " ".join(f"{r:.4f}" for r in self.ratios))
        return "\n".join(lines)


def resolve_pairs(spec: Spec, calibration: dict[str, Any] | None,
                  case: Any) -> tuple[int, str]:
    """决定这一轮要跑几对。设计 §5.4。

    三条约束：
      1. 配对数**不是工具参数** —— 模型给不了，只能由这里算。
      2. 只增不减：一次运行内变严可以，变松不行。
      3. 提升必须留痕：返回的 reason 会写进收据和散文 front-matter。
    """
    floor = max(spec.bench.min_pairs, 5)

    if calibration is None:
        return floor, "no A/A calibration for this case; using the hard floor"

    if not calibration.get("usable", False):
        return floor, ("A/A calibration was inconclusive "
                       f"({calibration.get('reason', 'unknown reason')}); "
                       "using the hard floor")

    sigma = float(calibration["sigma"])
    target = spec.min_speedup
    # 需要的 log 效应量 / 噪声。σ 是 log(ratios) 的标准差。
    needed_z = np.log(target) / sigma if sigma > 0 else float("inf")

    for pairs in (floor, 7, 9):
        # 5 对时 CI 半宽约 1.6σ，7 对约 1.35σ，9 对约 1.2σ（经验常数）
        half_width_z = {5: 1.6, 7: 1.35, 9: 1.2}[pairs]
        if half_width_z * sigma <= np.log(target):
            if pairs == floor:
                reason = (f"A/A sigma={sigma:.4g} log-units; "
                          f"{floor} pairs resolve the {target:g}x target")
            else:
                reason = (f"A/A sigma={sigma:.4g} log-units is too coarse for "
                          f"{floor} pairs at target {target:g}x; "
                          f"raised to {pairs}")
            return pairs, reason

    return 9, (f"A/A sigma={sigma:.4g} log-units exceeds even 9 pairs' "
               f"resolution for target {target:g}x; results will likely be "
               f"INDISTINGUISHABLE")


def run_bench(
    spec: Spec,
    reference: Reference,
    candidate: Candidate,
    *,
    case: Any | None = None,
    pairs: int | None = None,
    pairs_reason: str = "explicit override",
    sigma: float | None = None,
) -> BenchReport:
    """跑一轮配对计时。

    每一对内部都**重新生成输入**（02_AKO4ALL §4：计时后用原地重新随机化的输入
    再跑一次，防缓存 / 常量折叠）。
    """
    case = case or spec.cases[0]
    n_pairs = pairs or max(spec.bench.min_pairs, 5)
    if n_pairs < 5:
        raise ValueError(f"pairs={n_pairs} is below the hard floor of 5")

    order = build_sequence(n_pairs)
    warm_count = spec.bench.warmup

    # 每一对用不同种子造输入：既防缓存，也让「恰好在一个输入上快」暴露出来。
    prepared: dict[int, tuple[Any, ...]] = {}
    for i in range(n_pairs):
        prepared[i] = reference.make_inputs(case, 1000 + i)

    def make_thunk(label: str, pair_idx: int) -> Callable[[], Any]:
        inputs = prepared[pair_idx]
        fn = (reference.run if label == "baseline" else candidate.run)
        params = dict(case.params)

        def thunk() -> Any:
            return fn(*inputs, **params)

        return thunk

    thunks_by_pair: dict[int, dict[str, Callable[[], Any]]] = {}
    for i in range(n_pairs):
        thunks_by_pair[i] = {
            "baseline": make_thunk("baseline", i),
            "candidate": make_thunk("candidate", i),
        }

    seq: list[tuple[int, str, Callable[[], Any]]] = []
    for i, pair_labels in enumerate(order):
        for label in pair_labels:
            seq.append((i, label, thunks_by_pair[i][label]))

    warm = make_thunk("baseline", 0)
    samples = run_sequence(seq, warmup=warm, warmup_times=warm_count)
    ratios = paired_ratios(samples)

    interval = paired_bootstrap(
        ratios,
        resamples=spec.bench.bootstrap_resamples,
        seed=spec.bench.bootstrap_seed,
    )

    return BenchReport(
        samples=samples,
        ratios=ratios,
        interval=interval,
        pairs_used=n_pairs,
        pairs_reason=pairs_reason,
        order=order,
        sigma=sigma,
    )
