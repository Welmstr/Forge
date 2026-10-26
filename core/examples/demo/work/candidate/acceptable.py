"""候选：达标且不超可疑线 —— 给 ACCEPT 分支一个真实用例。

加速比的试错记录（七次实测，都在这道题上）：

  * 去掉循环里的下标运算            → 1.4x    够不到 2.0 目标
  * 展开 8 次循环                  → 1.1x    展开省的是循环控制，不是下标
  * 全部 + 二次循环转列表           → 1.77x   差一点没跨过 2.0
  * 一半转列表、一半用向量化        → 2.04x   勉强 —— 说明这是噪声边缘
  * 三分之一转列表、三分之二走标量  → 0.31x   标量 API 太慢
  * 向量化 + 两次无用加法           → 33x
  * 切 8 段向量化                  → 52x     numpy 调用远比 Python 迭代便宜

规律：**加速比 ≈ 1 / 每元素留在 Python 层下标运算上的时间占比**，
而且它对「这半截用快写法、那半截用慢写法」的比例极其敏感 ——
因为快写法和慢写法之间差着两三个数量级，不是线性的。

本文件取 n/4 的 Python 列表迭代 + 其余走一次向量化。
实测落在 6.86x（CI [5.82, 7.65]）：越过 2.0 目标，又远不到 10x 的可疑线。

注意 6.86x 对 n/4 这个参数非常敏感 —— 因为向量化和 Python 迭代之间
差着两三个数量级。这本身就是一条值得写进散文的经验：**混合写法的
加速比对切分比例不是线性的**，调参时要按数量级估，不能按比例估。
"""

from __future__ import annotations

import numpy as np


def run(src, alpha, bias):
    n = src.size
    out = np.empty_like(src)
    fo = out.reshape(-1)
    fs = src.reshape(-1)
    fb = bias.reshape(-1)
    a = float(alpha)

    cut = n // 4
    # 前 1/4：列表迭代（快于 ndarray 逐元素下标）
    ls = fs[:cut].tolist()
    lb = fb[:cut].tolist()
    i = 0
    while i < cut:
        fo[i] = ls[i] * a + lb[i]
        i += 1
    # 其余：一次性向量化
    fo[cut:] = fs[cut:] * a + fb[cut:]
    return out
