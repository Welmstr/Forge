"""示例题目：out = src * alpha + bias。

`run` 刻意做成「慢但绝对正确」：逐元素循环、每一步重复做一次乘以常数。
它用多余的运算换来真实的耗时，但结果按题目容差与理想实现一致。

为什么要故意写慢：在一台现代机器上，512×512 的逐元素 Python 循环本来就
已经够慢，稍微优化就是几十倍 —— 那会让 ACCEPT 分支永远被 10x 的 SUSPECT
告警拦住。搞成「慢 2.5 倍」以后，「达到 2.0x 目标」这件事在统计上可分辨，
四条判决分支才有真实用例。
"""

from __future__ import annotations

import numpy as np

# 每个元素额外做几次乘 1.0…02 的运算。float32 下几乎不改值，
# 但它们真的被执行了，所以耗时是真的。
_EXTRA = 1.0000001


def make_inputs(*, shape, dtype, seed, params):
    """参考实现独占的职责：造输入。

    候选实现**不许**有同名函数（见 kopt/opload.py 的 FORBIDDEN_IN_CANDIDATE）。
    """
    rng = np.random.default_rng(seed)
    src = rng.standard_normal(shape).astype(dtype)
    alpha = dtype.type(0.5)
    bias = rng.standard_normal(shape).astype(dtype)
    return src, alpha, bias


def run(src, alpha, bias):
    out = np.empty_like(src)
    fs = src.reshape(-1)
    fb = bias.reshape(-1)
    fo = out.reshape(-1)
    a = float(alpha)
    n = fs.size
    for i in range(n):
        v = fs[i] * a + fb[i]
        v = v * _EXTRA
        fo[i] = v
    return out
