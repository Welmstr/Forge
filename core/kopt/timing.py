"""计时 —— 唯一计时来源。

设计 §5.5 / 06_Xe-Forge §6：

    永不自己写测速脚本；唯一计时来源是一个命令。
    用别的方法得到的数不可比。

这里刻意做得「难用」：
  * 只能通过 `run_sequence` 跑完整的一轮调度，没有「单独测一次」的入口。
  * 没有公开的「现在几点了、减一下」接口给上层用。
  * 每次计时都用 `time.perf_counter_ns`，并且只回报**本次调度内**的数据。
"""

from __future__ import annotations

import gc
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np


@dataclass(frozen=True)
class Sample:
    """一次计时。单位统一为毫秒。"""

    label: str
    ms: float
    pair: int

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "ms": self.ms, "pair": self.pair}


def _time_once(fn: Callable[[], Any]) -> float:
    t0 = time.perf_counter_ns()
    fn()
    t1 = time.perf_counter_ns()
    return (t1 - t0) / 1e6


def run_sequence(sequence: list[tuple[int, str, Callable[[], Any]]],
                 warmup: Callable[[], Any] | None = None,
                 warmup_times: int = 3) -> list[Sample]:
    """按给定顺序跑一轮计时，返回每次的耗时。

    `sequence` 是 (pair_index, label, thunk) 的有序列表 —— **顺序由调度器决定，
    不由这里决定**。这个函数唯一会加的东西是 warmup，且 warmup 不计入返回结果。

    计时前后各关一次 GC：避免某一侧的测量恰好撞上一次循环回收。
    两侧都关，所以对配对比较是公平的。
    """
    if warmup is not None:
        for _ in range(max(0, warmup_times)):
            warmup()

    out: list[Sample] = []
    for pair_idx, label, thunk in sequence:
        gc.collect()
        gc.disable()
        try:
            ms = _time_once(thunk)
        finally:
            gc.enable()
        out.append(Sample(label=label, ms=ms, pair=pair_idx))
    return out


def build_sequence(pairs: int, *, labels: tuple[str, str] = ("baseline", "candidate"),
                   abba: bool = True) -> list[list[str]]:
    """生成交错顺序。设计 §5.1。

    第 1 对 B→C，第 2 对 C→B，第 3 对 B→C……

    为什么不能 5 连 + 5 连：机器热漂移、频率爬坡、缓存状态、别人的负载
    会全部和「哪一版」共线，测出来的差异可能一点都不来自改动。
    03_atrex §3 第 4 条用的是同一招（ABBA 验证）。
    """
    order: list[list[str]] = []
    for i in range(pairs):
        if abba and i % 2 == 1:
            order.append([labels[1], labels[0]])
        else:
            order.append([labels[0], labels[1]])
    return order


def flatten(order: list[list[str]], thunks: dict[str, Callable[[], Any]]
            ) -> list[tuple[int, str, Callable[[], Any]]]:
    seq: list[tuple[int, str, Callable[[], Any]]] = []
    for i, pair in enumerate(order):
        for label in pair:
            seq.append((i, label, thunks[label]))
    return seq


def paired_ratios(samples: list[Sample],
                  labels: tuple[str, str] = ("baseline", "candidate")
                  ) -> list[float]:
    """每一对的 基线耗时 / 候选耗时。> 1 表示候选更快。

    配对要求两个样本属于同一 pair 且都在场。不齐就是错 —— 不能拿多出来的
    那一个去凑数，那会把交错设计的好处丢干净。
    """
    by_pair: dict[int, dict[str, float]] = {}
    for s in samples:
        by_pair.setdefault(s.pair, {})[s.label] = s.ms

    ratios: list[float] = []
    for idx in sorted(by_pair):
        row = by_pair[idx]
        b = row.get(labels[0])
        c = row.get(labels[1])
        if b is None or c is None:
            raise ValueError(f"pair {idx} is incomplete: {row}")
        if not (np.isfinite(b) and np.isfinite(c)) or c <= 0:
            raise ValueError(f"pair {idx} has a non-finite or non-positive timing: {row}")
        ratios.append(b / c)
    return ratios
