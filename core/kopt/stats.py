"""统计：配对 bootstrap 置信区间。

设计 §5.2 / 05_cuda-opt-agent §4：

    接受判据用「配对差的置信区间」，不用点估计。
    单发差异在噪声内时不接受。

用 numpy 而不是 scipy：可复现性优先，随机种子固定，重采样次数写进收据。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    confidence: float
    resamples: int
    seed: int

    def as_dict(self) -> dict:
        return {
            "point": self.point,
            "ci_low": self.low,
            "ci_high": self.high,
            "confidence": self.confidence,
            "resamples": self.resamples,
            "seed": self.seed,
        }


def _percentile(sorted_vals: np.ndarray, q: float) -> float:
    """线性插值分位数。用 numpy.percentile 保持行为一致、可复现。"""
    return float(np.percentile(sorted_vals, q))


def paired_bootstrap(ratios: list[float], *, resamples: int = 2000,
                     seed: int = 17, confidence: float = 0.95) -> Interval:
    """对配对比值做 bootstrap，返回几何均值的置信区间。

    为什么用几何均值而不是算术均值：比值是对数正态型量，
    算术均值会被个别「某次掉到一半」的样本拖偏，而几何均值对等比例的
    快慢是对称的 —— 2× 和 0.5× 应该互相抵消，而不是把均值拉低。

    为什么在 log 空间重采样：保证 CI 的下界永远 > 0，
    并且在两版完全相同时区间干净地收缩到 1 附近，不会跑出负的加速比。
    """
    arr = np.asarray(ratios, dtype=np.float64)
    if arr.size == 0:
        raise ValueError("paired_bootstrap needs at least one pair")
    if not np.all(np.isfinite(arr)) or np.any(arr <= 0):
        raise ValueError(f"ratios must be finite and positive, got {arr.tolist()}")

    logs = np.log(arr)
    rng = np.random.default_rng(seed)
    n = logs.size
    # (resamples, n) 一次性抽完：快，而且用同一个 rng 保证可复现。
    idx = rng.integers(0, n, size=(resamples, n))
    boot = np.exp(logs[idx].mean(axis=1))

    alpha = (1.0 - confidence) / 2.0
    low = _percentile(boot, alpha * 100.0)
    high = _percentile(boot, (1.0 - alpha) * 100.0)
    return Interval(
        point=float(np.exp(logs.mean())),
        low=low,
        high=high,
        confidence=confidence,
        resamples=resamples,
        seed=seed,
    )


def coefficient_of_variation(values: list[float]) -> float:
    """变异系数。用于 A/A 标定：同一份代码和自己比的离散度。"""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size < 2:
        raise ValueError("need at least 2 values")
    m = float(arr.mean())
    if m == 0:
        return float("inf")
    return float(arr.std(ddof=1) / m)
