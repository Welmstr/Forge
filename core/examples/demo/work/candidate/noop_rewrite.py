"""候选：看起来优化了，实际上什么也没改。

和参考实现同样是逐元素循环，只是把内层循环的写法换了一下。
用来验证「分辨不出」这一档：06_Xe-Forge §7 —— 「分辨不出」既不等于最快，
也不等于最差，必须能在排序里占位。

预期判决：verify = PASS（它确实算对了），
        bench 的 CI 跨过判定线 → adjudicate = INDISTINGUISHABLE。
"""

from __future__ import annotations

import numpy as np


def run(src, alpha, bias):
    # 和参考实现几乎一样的工作量，只是换了循环变量的组织方式
    out = np.empty_like(src)
    fs = src.reshape(-1)
    fb = bias.reshape(-1)
    fo = out.reshape(-1)
    a = float(alpha)
    for i, (s, b) in enumerate(zip(fs.tolist(), fb.tolist())):
        fo[i] = s * a + b
    return out
