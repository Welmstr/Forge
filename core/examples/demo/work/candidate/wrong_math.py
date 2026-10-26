"""候选：假优化（算错了，所以快）。

这是 harness 存在的主要理由之一：**「变快了」不能替代「算对了」**。
把乘加改成只乘不加，快得毫无悬念，但结果是错的。

预期判决：verify = FAIL，pipeline 在 S8 就停住，根本走不到 bench。
"""

from __future__ import annotations


def run(src, alpha, bias):
    # 漏了 + bias
    return src * alpha
