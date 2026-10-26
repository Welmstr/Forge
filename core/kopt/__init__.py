"""kopt —— 算子优化 harness 核心。

设计依据：kopt-harness-design.md v0.2。

一句话：程序掌舵（探针、冻结、判精度、计时、判决、校验散文、管权限、扣预算），
LLM 掌帆（读证据、提假设、改代码、写散文、反思），人类掌锚（定阈值、批准环境、决定晋升）。

这里**必须**是常规包，不能是命名空间包。起因是一个实测踩到的坑：

    在包的**父目录**（也就是 `import kopt` 能命中的那一层）里执行
    `python -m kopt.cli`，如果这个文件缺失或为空，Python 会把 `./kopt`
    当成命名空间包收下 —— `import kopt` 成功，但 `kopt.__version__` 不存在，
    报 `cannot import name '__version__' from 'kopt' (unknown location)`。

更隐蔽的是：editable 安装（`pip install -e`）写的那个 finder，对顶层包一律按
**命名空间包**处理（MAPPING 只给路径、不认 `__init__.py`）。所以「用 editable
装过、又恰好在父目录里跑」这一个组合会中招，换个目录跑又完全正常 —— 一个
「看你在哪个目录就时好时坏」的包，比跑不起来的包难查得多。
"""

__version__ = "0.1.0"
