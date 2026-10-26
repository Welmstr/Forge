"""候选：作弊 —— 自己造输入。

用来验证加载期的硬拒绝：候选模块里出现 make_inputs / get_inputs
一律拒绝导入。02_AKO4ALL §3 把「solution/ 不许含 get_inputs」写在 prompt 里，
这里把它变成加载期错误。

预期结果：Candidate.load 抛 KoptError，command 退出码 2。
"""

from __future__ import annotations

import numpy as np


def make_inputs(*, shape, dtype, seed, params):
    # 候选无权决定输入 —— 这个函数的存在本身就是违规
    return np.zeros(shape, dtype=dtype)


def run(src, alpha, bias):
    return src * alpha + bias
