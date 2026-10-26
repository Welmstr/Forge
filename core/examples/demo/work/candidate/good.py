"""候选：真优化。

把逐元素 Python 循环换成 numpy 向量化表达式。
在这个题目上这是唯一诚实的优化方向 —— 改动面小、可归因、可回滚。

相对参考实现约 20x。注意它会触发 SUSPECT 告警（>10x），
这是设计里的预期行为，不是 bug：见 design §5.2 与 02_AKO4ALL §3。
"""

from __future__ import annotations


def run(src, alpha, bias):
    return src * alpha + bias
