# Forge

算子性能优化 harness。程序掌舵，模型掌帆，结论只从被验证过的证据里长出来。

这个仓库装的是**机制**：一个可以做判决的 Python 内核，一个把它接进
DeepSeek Harness 的插件，和设计文档。

它**不装**结论。结论在 [Forge-KnowledgeHub](https://github.com/Welmstr/Forge-KnowledgeHub)，
运行过程在 [Forge-Logs](https://github.com/Welmstr/Forge-Logs)。三个仓库的分工是硬的：

| 仓库 | 装什么 | 什么时候信它 |
|---|---|---|
| **Forge**（这个） | 机制：`core/` `dsh/` `docs/` | 任何时候 —— 它是代码 |
| **Forge-KnowledgeHub** | 通过准入的结论 | **只有过了闸的**，且没过有效期 |
| **Forge-Logs** | 全部事件，只追加 | **只作为过程证据**，不产生任何结论 |

一句话区分后两个：知识要求「对」，日志要求「全」。

## 为什么要有第三个仓库

因为要检查 agent 是否按预期工作，就不能让 agent 写它自己的检查记录。

`Forge-Logs` 里的每一条都由**程序**写：插件在每次工具调用返回之后追加一行，
字段是适配器算出来的。agent 传不进来，也改不了已经写下的内容。
agent 唯一能写的是 `notes.md`，而那里面每条都标着 `author=agent`。

## 目录

```
core/            Python 内核 kopt（CLI + 全部判决逻辑）
dsh/             DeepSeek Harness 插件（把 kopt 接成工具）
docs/            设计文档
examples/        演示项目（可复现的输入，不提交任何跑出来的东西）
```

## 三个分钟级的上手

```bash
# 1. 内核能跑
cd core && python -m pip install --no-build-isolation --no-deps .
python -m kopt.cli --root examples/demo probe-status

# 2. 插件能被加载（不需要装 DSH）
cd dsh && node tools/smoke.mjs ../core ../core/examples/demo

# 3. 日志真的会写
FORGE_LOG_ROOT=/tmp/forge-logs node tools/smoke.mjs ../core ../core/examples/demo
```

## 它在防什么

- **模型不能决定「什么算快」。** 没有 `bench` 工具。计时是 `kopt_verify` 的副作用，
  而且只在验证通过之后发生。配对数由 A/A 标定算出，模型给不了。
- **模型不能改测量器。** `spec.toml`、`env.lock.json`、`gate.json` 对 agent 是只读的，
  写入会以退出码 6 被拒。
- **模型不能自己说「这个方案有用」。** 判决是 `adjudicate` 出的，五种闭合结论，
  每一张判决收据都挂在具体的内容哈希上。
- **模型不能给自己记功。** 日志由程序写。

## 状态

机制完整、可跑、`smoke.mjs` 全绿；**还没有装进 DSH 跑过真实会话**。
没验证的事这里就不写"已完成"。

设计依据见 `docs/`，先说清楚为什么，再说怎么实现。
