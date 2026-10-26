"""候选：慢一点（预期 REJECT）。

和参考实现一样算对，但每个元素多做了三次多余的乘。
加速比的预期 < 1，CI 上界落在 1.0 以下，从而触发 REJECT。
"""

from __future__ import annotations

import numpy as np


def run(src, alpha, bias):
    out = np.empty_like(src)
    fs = src.reshape(-1)
    fb = bias.reshape(-1)
    fo = out.reshape(-1)
    a = float(alpha)
    one = 1.0000001
    for i in range(fs.size):
        v = fs[i] * a + fb[i]
        v = v * one
        v = v * one
        v = v * one
        fo[i] = v
    return out
