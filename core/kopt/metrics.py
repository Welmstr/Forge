"""偏差度量。

两条贯穿全文件的规矩：

1. **度量本身必须比被测对象更精确。** 全部提升到 float64 再算，
   否则用 fp16 去衡量 fp16 的误差，量出来的是量具的误差。

2. **非有限数一律判死，不参与度量。** 04_AKA §5：重复 JSON key、非有限数、
   NaN / Infinity 直接拒。NaN 参与运算会把自己传染给结果，
   最后得到一个「看起来没超阈值」的 nan 比较 —— 那是最坏的一种假通过。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import KoptError


@dataclass(frozen=True)
class Deviation:
    """一次比较的结果。`value is None` 表示「没能算出来」，不是 0。"""

    metric: str
    value: float | None
    threshold: float
    ok: bool
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "value": self.value,          # None → JSON null，永远不写成 0
            "threshold": self.threshold,
            "ok": self.ok,
            "reason": self.reason,
        }


def _finite_guard(got: np.ndarray, metric: str, threshold: float) -> Deviation | None:
    if np.issubdtype(got.dtype, np.integer):
        return None
    if not np.all(np.isfinite(got)):
        n_nan = int(np.count_nonzero(np.isnan(got)))
        n_inf = int(np.count_nonzero(np.isinf(got)))
        return Deviation(
            metric=metric, value=None, threshold=threshold, ok=False,
            reason=f"candidate output contains {n_nan} NaN and {n_inf} Inf "
                   f"values; a non-finite result is a failure, not a deviation",
        )
    return None


def _rel_l2(got: np.ndarray, want: np.ndarray) -> float:
    num = float(np.linalg.norm((got - want).ravel()))
    den = float(np.linalg.norm(want.ravel()))
    if den == 0.0:
        # 参考答案全零：此时任何非零偏差都是无穷大的相对误差。
        # 返回 0 会把「候选输出了垃圾」说成「完美吻合」。
        return 0.0 if num == 0.0 else float("inf")
    return num / den


def _max_abs(got: np.ndarray, want: np.ndarray) -> float:
    return float(np.max(np.abs(got - want))) if got.size else 0.0


def _max_rel(got: np.ndarray, want: np.ndarray) -> float:
    if not got.size:
        return 0.0
    diff = np.abs(got - want)
    den = np.abs(want)
    # 参考值为 0 的位置：有偏差就是 inf，没偏差就是 0。不做 eps 平滑 ——
    # 平滑会让「在零点附近彻底算错」看起来像小误差。
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(den > 0, diff / den,
                       np.where(diff > 0, np.inf, 0.0))
    return float(np.max(rel))


def _ulp(got: np.ndarray, want: np.ndarray) -> float:
    if np.issubdtype(got.dtype, np.integer):
        raise KoptError("metric 'ulp' is meaningless for integer dtypes")
    # 在原 dtype 下数 ULP：先映射到有序整数空间再相减。
    kind = got.dtype
    ibits = {np.dtype("float16"): np.int16,
             np.dtype("float32"): np.int32,
             np.dtype("float64"): np.int64}.get(kind)
    if ibits is None:
        raise KoptError(f"metric 'ulp' unsupported for dtype {kind}")

    def to_ordered(a: np.ndarray) -> np.ndarray:
        i = a.view(ibits).astype(np.int64)
        # IEEE754 负数在位模式上是逆序的，翻成单调序列
        return np.where(i < 0, np.int64(np.iinfo(ibits).min) - i + 1, i)

    return float(np.max(np.abs(to_ordered(got) - to_ordered(want))))


_IMPL = {
    "rel_l2": _rel_l2,
    "max_abs": _max_abs,
    "max_rel": _max_rel,
    "ulp": _ulp,
}


def compare(got, want, *, metric: str, threshold: float) -> Deviation:
    """把候选输出和参考输出比一比。

    形状或 dtype 对不上一律判失败 —— 不广播、不转型。
    一个「形状差不多」的输出不是「几乎正确」，是错的。
    """
    if metric not in _IMPL:
        raise KoptError(f"unknown metric: {metric}")

    got = np.asarray(got)
    want = np.asarray(want)

    if got.shape != want.shape:
        return Deviation(
            metric=metric, value=None, threshold=threshold, ok=False,
            reason=f"shape mismatch: candidate {got.shape} vs reference {want.shape}",
        )

    guard = _finite_guard(got, metric, threshold)
    if guard is not None:
        return guard

    if metric == "ulp":
        if got.dtype != want.dtype:
            return Deviation(
                metric=metric, value=None, threshold=threshold, ok=False,
                reason=f"dtype mismatch under ulp metric: {got.dtype} vs {want.dtype}",
            )
        value = _ulp(got, want)
    else:
        # 提升到 float64 再比：量具要比被测对象精确。
        g = got.astype(np.float64) if not np.issubdtype(got.dtype, np.integer) else got.astype(np.int64)
        w = want.astype(np.float64) if not np.issubdtype(want.dtype, np.integer) else want.astype(np.int64)
        value = _IMPL[metric](g.astype(np.float64), w.astype(np.float64))

    if not np.isfinite(value):
        return Deviation(
            metric=metric, value=None, threshold=threshold, ok=False,
            reason=f"{metric} evaluated to a non-finite value; "
                   f"treated as failure rather than as a deviation",
        )

    return Deviation(metric=metric, value=float(value),
                     threshold=threshold, ok=float(value) <= threshold)


def perturbation(ref_out: np.ndarray, threshold: float) -> np.ndarray:
    """造一个「一定会被任何度量判死」的扰动，用于变异哨兵。

    delta = 10 * threshold * (|ref| + 1) 对四种度量都足够大：
      max_abs  : max|delta| >= 10t  > t
      max_rel  : delta/|ref| >= 10t > t
      rel_l2   : ||delta||/||ref|| >= 10t > t
      ulp      : 远超任何合理的 ULP 阈值
    """
    ref = np.asarray(ref_out)
    if np.issubdtype(ref.dtype, np.integer):
        # 整数：最小可见扰动就是 1，且必然超过任何 < 1 的阈值。
        return (ref + np.ones_like(ref)).astype(ref.dtype)
    delta = 10.0 * max(threshold, 1e-12) * (np.abs(ref.astype(np.float64)) + 1.0)
    return (ref.astype(np.float64) + delta).astype(ref.dtype)
