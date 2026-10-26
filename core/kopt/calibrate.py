"""A/A 标定 —— 噪声地板。

设计 §5.4。05_cuda-opt-agent 的局限里写得很老实：它的 1.005 阈值是通用
默认值，**没有按平台噪声标定**。这里把这个补上。

做法：拿基线代码和**它自己**跑一遍完整的配对流程。真实加速比恒等于 1，
所以测出来的离散度就是这台机器此刻的噪声地板 σ。

这一步顺带回答一个迟早要问的问题：「以我这台机器的噪声，5 对能分辨出的
最小加速是多少？」
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .opload import Reference
from .spec import Spec
from .stats import Interval, paired_bootstrap
from .timing import build_sequence, paired_ratios, run_sequence


@dataclass
class Calibration:
    case: str
    pairs: int
    sigma: float                 # log(ratio) 的标准差 —— 噪声地板
    ratios: list[float]
    interval: Interval
    resolution: float            # 这对数下能分辨的最小加速比
    usable: bool
    reason: str | None = None
    # A/A 点估计偏离 1 的程度。记录但**不据此判废** —— 见 run_calibration 末尾。
    bias: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "pairs": self.pairs,
            "sigma": self.sigma,
            "bias": self.bias,
            "ratios": self.ratios,
            "interval": self.interval.as_dict(),
            "resolution": self.resolution,
            "usable": self.usable,
            "reason": self.reason,
        }

    def render(self) -> str:
        lines = [
            f"CALIBRATE case={self.case} pairs={self.pairs}",
            f"  A/A ratios: " + " ".join(f"{r:.4f}" for r in self.ratios),
            f"  sigma = {self.sigma:.5g} (log units), bias = {self.bias:+.4f}",
            f"  {self.pairs} pairs resolve down to ~{self.resolution:.4f}x",
        ]
        if not self.usable:
            lines.append(f"  NOT USABLE: {self.reason}")
        return "\n".join(lines)


# 各配对数下，95% CI 半宽约等于多少个 σ（bootstrap 的经验值）。
# 5 对 ≈ 1.6σ，7 对 ≈ 1.35σ，9 对 ≈ 1.2σ。
_HALF_WIDTH_Z = {5: 1.6, 7: 1.35, 9: 1.2}


def run_calibration(spec: Spec, reference: Reference, *, case: Any | None = None,
                    pairs: int = 5) -> Calibration:
    """A/A：基线 vs 基线。真正的加速比恒为 1，量出来的离散度就是噪声。"""
    case = case or spec.cases[0]
    n = max(5, pairs)

    prepared = {i: reference.make_inputs(case, 7000 + i) for i in range(n)}
    order = build_sequence(n)

    def thunk(pair_idx: int):
        inputs = prepared[pair_idx]
        params = dict(case.params)
        return lambda: reference.run(*inputs, **params)

    # 两臂都是同一个函数，但用两份独立的 thunk —— 避免闭包被优化掉，
    # 也让两侧各自经历一次调用开销。
    a_thunks = {i: thunk(i) for i in range(n)}
    b_thunks = {i: thunk(i) for i in range(n)}

    seq = []
    for i, labels in enumerate(order):
        for label in labels:
            fn = a_thunks[i] if label == "baseline" else b_thunks[i]
            seq.append((i, label, fn))

    samples = run_sequence(seq, warmup=a_thunks[0], warmup_times=spec.bench.warmup)
    ratios = paired_ratios(samples)

    logs = np.log(np.asarray(ratios, dtype=np.float64))
    # ddof=1：这是样本标准差，不是总体标准差。
    sigma = float(logs.std(ddof=1)) if logs.size > 1 else float("inf")

    interval = paired_bootstrap(ratios,
                                resamples=spec.bench.bootstrap_resamples,
                                seed=spec.bench.bootstrap_seed)

    hw = _HALF_WIDTH_Z.get(n, 1.6)
    resolution = float(np.exp(hw * sigma)) if np.isfinite(sigma) else float("inf")

    usable = True
    reason: str | None = None
    if not np.isfinite(sigma):
        usable = False
        reason = "sigma is not finite"
    elif sigma == 0.0:
        # 完全零方差通常意味着计时器被固定或被缓存掉了，不能当成「完美机器」。
        usable = False
        reason = ("A/A ratios are exactly identical — a zero noise floor "
                  "usually means the timer is reading a constant, not a "
                  "very quiet machine")

    # 注意这里**不**因为 A/A 的 CI 不含 1 就判「不可用」。
    # 第一版这么写过，实测下来它太严了：A/A 两臂跑的是同一个函数，
    # 但仍是两次独立运行，CPU 频率漂移就会让 5 对的 CI 偶尔偏离 1。
    # 那是**噪声的估计**，不是**系统偏差的证据** —— 要求 A/A 的 CI 必须含 1，
    # 等于要求噪声自己先证明自己不存在。
    #
    # 正确做法：A/A 只负责给出 σ（噪声地板）。真实的顺序偏差由 ABBA 的
    # 逐对翻转来抵偿，而不是靠 A/A 事后检验。所以这里只记录 bias，
    # 由调用方决定要不要怀疑。

    return Calibration(case=case.label, pairs=n, sigma=sigma, ratios=ratios,
                       interval=interval, resolution=resolution,
                       usable=usable, reason=reason,
                       bias=float(np.exp(logs.mean())))
