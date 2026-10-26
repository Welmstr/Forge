# 算子优化 Harness 插件 —— 设计方案 v0.2

> 规范来源：`/root/Agent_design/docs/` 六份开源项目设计归档（KernelAgent / AKO4ALL / atrex / AgentKernelArena / cuda-opt-agent / Xe-Forge）。
> 本方案与具体硬件平台无关；平台相关的部分全部收敛到 `interfaces.py` 的几个抽象里。
>
> **v0.2 已定下的四件事**：① 宿主 = DSH 插件；② Python 核心 + TypeScript 宿主适配层；
> ③ 被否方向与「分辨不出」也要写散文（`docs/opt/rejected/`，且由程序强制）；
> ④ 5 配对是**下限**，A/A 标定说不够时程序自动提到 7 / 9 对。
>
> v0.2 的 DSH 接口部分（§9）是**读过 DSH 0.2.1-rc.3 实际源码后**写的，不是推测。

---

## 0. 一句话

**程序掌舵（探针、冻结、判精度、计时、判决、校验散文、管权限、扣预算），LLM 掌帆（读证据、提假设、改代码、写散文、反思），人类掌锚（定阈值、批准环境、决定晋升）。**

前两句是 `07_COMPARISON.md` §7 的原话；第三句是你这次新增的需求，六个开源项目里只有 atrex 的 supervisor 有类似形状，而且它的「人」是程序扮演的。你要的是真人闸门，所以这一层要新设计。

---

## 1. 权威边界表

抄 `03_atrex` §2 的形式，这是全设计最值钱的一张表。任何实现上的争议都回来查这张表。

| 角色 | 拥有什么 | **明确不拥有什么** |
|---|---|---|
| **人类（你）** | `spec.toml` 里的精度阈值与题目约束；recon 报告的批准 / 修正 / 否决；候选晋升进主线的决定；预算上限 | 不能手改 receipt；不能在 `docs/opt/` 里改已过闸的散文（只能追加勘误） |
| **Harness（程序）** | 执行探针；冻结 `env.lock.json`；精度判定；**唯一计时来源**；加速比判决；散文数字交叉校验；写权限裁决；预算预扣 | 不能替人类决定题目阈值（`spec.toml` 缺项 ⇒ 报错，不兜底） |
| **LLM（agent）** | 读证据、分类问题、提假设、写候选内核、写散文、写反思 | **不能判「这算通过」**；不能改测量器 / 参考实现 / spec；不能手打测量数字；不能自己写 benchmark 脚本 |

落到一句：**agent 可以决定「试什么」，不能决定「什么算成功」。**

---

## 2. 主流程（确定性状态机）

抄 `05_cuda-opt-agent` §1 的做法：控制流写死，LLM 只填节点内部的产出。`⟦⟧` 是真人闸门。

```
S0  probe        探针：硬件事实 + 软件事实（每条带命令与原始输出）
S1  report       渲染《环境探查报告》→ docs/recon/<ts>.md
    ⟦人类闸门 1⟧  approve / amend / reject
S2  freeze       冻结 env.lock.json（含人工修正的署名）
S3  calibrate    A/A 噪声地板 + 变异哨兵 + 基线精度收据 + 基线计时
S4  analyze      结构化问题清单（枚举类型 + 严重度 + 证据）
S5  plan         问题 → 阶段映射，硬依赖校验
S6  propose      一次只改一个阶段的一处
S7  build        编译（带修复重试上限）
S8  verify       ★ 精度验证模块                      ← 硬门，不过不计时
S9  bench        ★ 5 配对交错计时 + bootstrap CI      ← 唯一计时来源
S10 adjudicate   判决：ACCEPT / REJECT / INDISTINGUISHABLE / INVALID（程序，非 LLM）
S11 narrate      写散文 → doc-gate 数字交叉校验 → docs/opt/
S12 journal      receipt + ledger 追加；知识投影
    ⟦人类闸门 2⟧  晋升进主线？（建议保留）
    → 回 S4 或终止（预算 / 停滞 / 达标）
```

**为什么 S8 必须在 S9 之前**：`01_KernelAgent` §2 第 1 条「先验证初始内核，再花 worker 的预算」。算错的东西快不快没有意义，而且算错往往就是「因为少算了所以快」。

---

## 3. 阶段一：自行探查 + 报告 + 人类确认

这是你明确要的第一步，也是整个设计里我改动最大的地方。

### 3.1 探针的硬规矩

`03_atrex` §7 的微分析技能里有一句话必须刻在这一层上：

> **「记住的硬件细节只是假设」**（记忆会过期）。

所以：

1. **每一条事实 = 一条实际执行的命令 + 退出码 + 原始输出片段。** 没有执行过的，不许出现在报告里。
2. **拿不到就是 `unknown`，不许猜，不许兜底。** 这是 `03_atrex` §8 的 `hardware_wiki` fail-loud 规矩：「返回一条不相关的优化经验还能用，返回一个不相关的峰值 FLOPS 会污染所有利用率计算。」
3. **LLM 在这一层只负责把原始输出翻译成人话**，不负责产生事实。模型没有权限往报告里添加任何它"知道"但探针没测到的东西。

### 3.2 探什么

分两类，因为这两类的失败处理不同：

**硬件事实**（错了会污染之后所有的利用率/上限计算 ⇒ fail-loud）
- 设备型号、数量、拓扑
- 各级存储容量与带宽（能测则实测，不能测则标 `unknown` 并注明「来自厂商文档」或「未知」）
- 计算单元规格、时钟策略（是否锁频、是否有 boost）
- 当前占用情况（有没有别人在用这台机器——直接影响 5 配对的噪声）

**软件事实**（错了一般只是走弯路 ⇒ 记录即可）
- 编译器 / 工具链版本与可用 flag
- 运行时 / 驱动版本
- profiler 是否存在、是否有权限（很多容器里没有）
- 沙箱能力（`05` §6：cuda-opt-agent 缺 bwrap 直接拒绝运行，不降级——这个口径要在报告里讲清楚）
- 关键环境变量

### 3.3 报告长什么样

`docs/recon/<timestamp>.md`，两段式：

- **人话段**：一段散文，讲「这台机器是什么、它擅长什么、哪里会成为瓶颈、哪些东西我没测到」。给人读的。
- **事实表**：每行 `事实 | 值 | 置信 | 来源命令 | 原始输出指针`。置信只有三档：`measured` / `documented` / `unknown`。

### 3.4 人类闸门 1 的三个出口

```
kopt recon approve            # 原样冻结
kopt recon amend <key> <value> --why "..."   # 人工修正，带署名与理由
kopt recon reject --why "..."                # 推翻重探
```

**关键设计**：批准这个动作不只是放行，它**产出一个冻结的、带哈希的工件** `env.lock.json`，而这个哈希会进入后面每一次测量的身份（见 §6）。

这样做的好处是：半个月后你回头看一篇散文，能精确知道它是在哪台机器、哪个工具链版本下测出来的；机器一变，`env.lock` 哈希一变，旧结论自动失效而不是悄悄继续被引用。这是 `03_atrex` §4「测量身份 = 内容哈希」的直接应用。

人工修正的字段要永久标记 `source: human`，不能和探针测到的混在一起 —— 人也会记错。

---

## 4. 阶段二：精度验证模块

你要求「精度限制由实际题目条件决定」。这一条正好撞上 `03_atrex` §6 最硬的一条规矩：

> 数值规划器**不能提供阈值**。

### 4.1 阈值的唯一来源：`spec.toml`

```toml
[problem]
name     = "..."
reference = "reference/ref_impl.py"     # 参考实现，受保护

# —— 每个 dtype 必须显式声明，没声明就是 UNSUPPORTED ——
[tolerance.fp32]
metric    = "rel_l2"      # rel_l2 | max_abs | max_rel | ulp
threshold = 1e-6

[tolerance.fp16]
metric    = "rel_l2"
threshold = 1e-3

[[case]]                   # 题目给定的形状
shape = [4096, 4096]
dtype = "fp16"

[verify]
seeds        = [0, 1]      # 每个 case 至少 2 个种子
fresh_inputs = true        # 每次重新生成，禁止复用
```

规矩：

1. **`spec.toml` 在受保护路径里，agent 不可写。** 阈值是题目条件，不是可调参数。
2. **dtype 没有声明容差 ⇒ `UNSUPPORTED`，流程停。** 不是默认值，不是通过。这是 `07` 定律 3：「缺失必须显式，不能等于零，也不能等于通过」，以及 `03_atrex` §6「没有可用 Shape ⇒ 显式 unsupported 提示，而不是『通过』」。
3. **agent 不许提交 Python 校验代码。** `03_atrex` §6：规划器只接受**声明式**用例计划。agent 想加 case，只能提交一条声明（shape/dtype/分布），由程序去执行。

### 4.2 判定流程

```
对每个 case × 每个 seed：
    生成全新输入（禁止复用、禁止 agent 提供输入）
    跑参考实现 → 跑候选
    按 spec 声明的 metric 算偏差
    与 spec 声明的 threshold 比
```

- **任何一个 case 失败 ⇒ 整体 FAIL。** 一个完整的失败工作量即可证伪（`03_atrex` §6）。
- **PASS 要求所有选中覆盖都通过**，不存在部分分。
- 四种结论：`PASS` / `FAIL` / `UNSUPPORTED`（没有可用阈值或形状）/ `INCONCLUSIVE`（执行出错，没拿到结论）。**没有第五种，尤其没有「大致通过」。**

### 4.3 变异哨兵（必做）

抄 `02_AKO4ALL` §3，这是六个项目里性价比最高的一条：

> 把参考实现的一个常数扰动后，校验**必须报错**；不报错说明测试根本没接上。

在 S3 calibrate 阶段跑一次，之后每次 `env.lock` 变更重跑。**哨兵没过，整个 harness 拒绝进入 S8 之后的任何阶段。** 理由很直白：一个永远返回 PASS 的校验器，比没有校验器更危险。

### 4.4 收据

每次验证产出 `receipts/verify/<id>.json`，绑定：case / shape / dtype / seed / metric / threshold / 实测偏差 / 参考实现摘要 / 候选源码 sha256 / `env.lock` 哈希。

散文里出现的任何精度数字，必须能在收据里找到（见 §7.3）。

---

## 5. 阶段三：性能判定（5 配对）

你要求「5 次优化版 + 5 次 baseline」。这个总量我完全保留 —— **10 次运行，一次不多一次不少** —— 但要改执行顺序和判读方式。

### 5.1 改顺序：不要 5 连 + 5 连，要交错配对

如果先连跑 5 次 baseline 再连跑 5 次候选，那么**机器的热漂移、频率爬坡、缓存状态、别人的负载**全部和「哪一版」共线。测出来的 5% 差异可能一点都不来自你的改动。

改成按对交错，并逐对翻转顺序（ABBA 式反平衡）：

```
第 1 对：  B → C
第 2 对：  C → B
第 3 对：  B → C
第 4 对：  C → B
第 5 对：  B → C
```

总共仍是 5 次 B + 5 次 C。代价为零，统计性质严格更好。这是 `03_atrex` §3 第 4 条 ABBA 验证的做法。

每一对内部：**重新随机化输入**（`02_AKO4ALL` §4：计时后用原地重新随机化的输入再跑一次，防缓存/常量折叠），warmup 次数由配置声明且**不计入这 10 次**。

### 5.2 改判读：配对 bootstrap 置信区间，不是比平均值

抄 `05_cuda-opt-agent` §4。拿到 5 个配对比值 `r_i = t_B,i / t_C,i` 之后：

```
paired bootstrap，2000 次重采样，固定随机种子 → 95% CI
```

判决（程序做，不是 LLM 做）：

| 条件 | 判决 |
|---|---|
| CI 下界 ≥ 接受阈值 | **ACCEPT** |
| CI 上界 ≤ 1（且下界也 <1） | **REJECT** |
| CI 跨过 1 | **INDISTINGUISHABLE** |
| 加速比 > 10× | **SUSPECT** → 强制回 S8 复验精度 |
| 缺计时一致性标记 / 有非有限数 / 配对不齐 | **INVALID**（不是 0，也不是估计值） |

最后两行分别来自 `02_AKO4ALL` §3（>10× 要标记为可疑）和 `04_AKA` §4（必须显式 `benchmark_method_consistent=True` 才给性能分；缺失或 false 一律判废，**不拿平均时间反推**）。

### 5.3 `INDISTINGUISHABLE` 是一等公民

这是 `06_Xe-Forge` §7 那个很细的坑：

> 如果只按 speedup 排序，「分辨不出」的 trial 会隐身，导致最后交出更慢的内核。

所以：
- 它**既不等于 ACCEPT，也不等于 REJECT**，在排行里单独占一档，排在「有测量值的 ACCEPT」之后、「REJECT」之前。
- 它的 `speedup` 字段保持 `null`，**绝不写成 1.0**。
- 排序用的键，**绝不回写成报告里的测量值**。

### 5.4 接受阈值从哪来：A/A 噪声标定

`05_cuda-opt-agent` 的局限里写得很老实：它的 1.005 阈值是通用默认值，**没有按平台噪声标定**。我们在 S3 就把这个补上。

**A/A 测试**：拿 baseline 和 baseline **自己**跑一遍完整的 5 配对流程。真实加速比恒等于 1，所以测出来的离散度就是这台机器此刻的噪声地板 σ。

于是：

```
接受阈值 = max(spec 里声明的最小加速, 1 + k·σ_AA)
```

**这一步还会顺带回答一个你迟早要问的问题**：

> 「以我这台机器的噪声，5 对能分辨出的最小加速是多少？」

程序把这个数算出来告诉你。如果它是 3%，而你期待的改动收益是 1%，那 5 对就是不够的 —— 这个结论由标定数据给出，而不是等到测了二十个改动全是 INDISTINGUISHABLE 之后才反应过来。

我建议把 5 定义成**下限而不是定值**：默认 5 对，当 A/A 标定说 5 对的可分辨粒度粗于 `spec` 里声明的目标收益时，程序自动提升到 7 或 9 对并在报告里说明原因。要不要开这个自动提升，留给你定（见 §10）。

**【v0.2 已定】自动提升开启。** 三条实现约束，缺一不可：

1. **配对数不是工具参数。** 模型**无法**传 `pairs`，它由 `spec.toml` 的目标收益 + 最近一次 A/A 标定的 σ 共同算出。理由：`07` 定律 5 讲预算「只能收紧」，统计严谨性是同一个道理的反面 —— **只能变严，不能被调松**。把 `pairs` 开放成参数，等于允许模型在测不出来的时候把门槛降下来。
2. **提升必须留痕。** receipt 里记 `pairs_required / pairs_reason / sigma_aa / calibration_id`，散文 front-matter 里一并冻结。读者要能看出「这篇是 9 对测的，因为那天机器噪声大」。
3. **提升是单向的。** 一次运行内只增不减；想回到 5 对，只能重新标定并留下新的 `calibration_id`。

### 5.5 唯一计时来源

`06_Xe-Forge` §6 的 NEVER 清单，照抄：

- **只有 `kopt bench` 能产生时间数字。** 「用别的方法得到的数不可比」。
- **agent 永不创建 benchmark / test / helper 脚本。**
- **agent 永不把测量数字手打进任何文件。**

第三条由 §7.3 的 doc-gate 强制执行，不靠自觉。

---

## 6. 测量身份 = 内容哈希

`07` 定律 2。每次测量的身份由这些内容拼出来做哈希：

```
候选源码 sha256
  + 参考实现 sha256
  + spec.toml sha256
  + env.lock.json 哈希
  + case 集合（shape/dtype/seed）
  + 计时参数（warmup/配对数/交错顺序）
  + harness 自身版本
```

一个机制同时解决三件事：**缓存**（同一身份直接返回旧记录，不重跑）、**去重**（不重复花钱）、**复现**（能证明当时用的到底是哪份代码）。

另外抄 `03_atrex` §4 一条：**结果未知（超时/中断）时不盲目重放**，标记为未知，交给人决定。

---

## 7. 阶段四：散文归档（你的核心需求）

### 7.1 定位：这是给人看的学习材料，不是知识库

必须先把这件事说死，否则它会变成设计里最大的隐患。

散文是 LLM 写的自然语言，doc-gate 能校验数字和结构，**校验不了「解释得对不对」**。`07` 定律 6 说得很清楚：经验只能从**被判过的证据**里投影，而且要有准入闸。

所以本设计里是两条分开的通道：

| | 散文 `docs/opt/*.md` | 知识 `knowledge/laws.jsonl` |
|---|---|---|
| 作者 | LLM 写，人读 | 程序从 receipt 投影 |
| 用途 | **给人学习** | 给下一轮检索 |
| 能否被当作事实引用 | **否** | 是 |
| 可否重建 | 否（是叙事） | 是（幂等） |

`05_cuda-opt-agent` §7 的 `project_knowledge` 幂等重建就是这个道理：**记忆坏了不会污染判断**。散文坏了，最多是读起来不舒服。

### 7.2 文件结构：机器写头，模型写身

`docs/opt/<run_id>-<slug>.md`：

```markdown
---
# ↓ 本段由 kopt 生成并冻结，agent 不可编辑（doc-gate 校验哈希）
run_id:           r0007
verdict:          ACCEPT
stage:            memory_access
speedup_ci95:     [1.034, 1.071]
pairs_ms:         [[12.41, 11.98], [12.38, 11.95], [12.44, 12.01], [12.39, 11.97], [12.42, 11.99]]
noise_floor_sigma: 0.004
accept_threshold: 1.012
correctness:      PASS
verify_receipt:   rc-9f2a1c...
measurement_id:   m-7c31be...
source_sha256:    a41f...
env_lock:         e-41b8...
---

## 当时看到了什么
## 我猜是什么原因
## 为什么这么猜
## 改了什么
## 什么结果会推翻这个猜测
## 实际测到了什么
## 哪里和预期不一样
## 这条经验在什么条件下才成立
```

八段的骨架来自 `06_Xe-Forge` §6 规定的推理格式：

> **Observation → hypothesis → proposed change → expected counter change → benchmark result**

加上两条补充：
- 第 7 段（哪里和预期不一样）来自 `01_KernelAgent` §6 的反思 schema —— **必须把「诊断对不对」和「修复有没有效」分开写**。猜对方向但代码写错，和方向本身就错，是完全不同的两件事，混在一起写就学不到东西。
- 第 8 段来自 `03_atrex` §7 —— **负结果只在其条件下成立**。没有边界的「经验」下次会把你坑了。

### 7.3 doc-gate：散文的准入闸

`kopt doc-gate` 必须全过，文件才会落到 `docs/opt/`。检查项：

1. **数字交叉校验**：正文里出现的每一个数字，都必须能从 front-matter 推出来（允许四舍五入误差）。推不出来 ⇒ 拒。
   这一条就是在程序层面执行 `06_Xe-Forge` 的「永不把测量数字手打进 trial result」，同时堵死 `02_AKO4ALL` §6 提到的「agent 自己读 bench 输出可能误读」。
2. **结论一致**：正文声称的结论必须和 front-matter 的 `verdict` 一致。散文里写「这次提速明显」而 front-matter 是 `INDISTINGUISHABLE` ⇒ 拒。
3. **八段齐全**，缺段 ⇒ 拒。
4. **必须有可证伪句**（第 5 段非空且包含一个具体的、可观测的反例条件）。
5. **必须有边界句**（第 8 段非空）。
6. **对已测量的量不许用模糊措辞**（"大概""应该是""估计" 修饰测量值 ⇒ 拒；修饰假设则允许）。
7. **front-matter 哈希未被改动。**

### 7.4 写入策略：只追加

- `docs/opt/` **只追加**，过闸之后不再编辑。
- 结论后来被推翻了，不是回去改旧文，而是**新写一篇**并在新文里引用旧文的 `run_id`。
- 原因见 `06_Xe-Forge` §8：经验文件放在 agent 工作区之外、只追加，既避免误写，也避免被当成源代码。

### 7.5 失败和「分辨不出」也要写（建议）

你说的是「每次取得优化进展需要写散文」。我建议把范围扩大一点，写进 `docs/opt/rejected/`：

- `02_AKO4ALL` §2 的停止条件里有一条是「**已记录 ≥3 个被否方向**」才允许停。没有否定记录，agent 会反复试同一条死路。
- `05_cuda-opt-agent` 把失败方向专门进 `blacklist`。
- 从人的角度看，**「我以为会快结果没快，原因是……」这类文章的学习价值通常高于成功案例** —— 成功案例往往只是确认了你已经相信的东西。

对 `INDISTINGUISHABLE` 的散文要有一句特别的话：**说清楚「这次没分辨出来」是『改动没用』还是『测量精度不够』**。这两者在 5 配对下经常长得一样，而 A/A 标定的 σ 正好能回答它。

**【v0.2 已定】采纳，并且由程序强制而不是靠自觉。**

实现方式抄 `02_AKO4ALL` §2 第 7 条那个「迭代 → 日志 → commit 三步必须连号」的结构性约束 —— 它是纯 prompt 能做到的最强约束，而我们在插件里可以做得更硬：

> **`kopt propose` 在上一轮判决尚未归档散文时直接拒绝执行。**

```
S10 判决 = ACCEPT             ⇒ 必须写 docs/opt/<run>.md
S10 判决 = REJECT / INDIST.   ⇒ 必须写 docs/opt/rejected/<run>.md
S10 判决 = INVALID / SUSPECT  ⇒ 不要求散文（这是 harness 自己的故障，不是知识）
未归档 ⇒ 下一轮 propose 报错退出
```

这样「写散文」就不是一句嘱托，而是**流程的闭锁条件**。注意第三行：测量失效不该逼模型硬写一篇反思 —— 那只会产出编造的因果。区分「这次结论无效」和「这次结论是负的」，是 `07` 定律 3 的直接应用。

`rejected/` 的散文模板比成功版少两段（没有"实际测到了什么"的正向结果），但**第 5 段（什么结果会推翻这个猜测）和第 8 段（适用边界）反而要求更严** —— 一条没有边界的否定结论，会让你以后再也不敢碰一个其实可行的方向。

---

## 8. 写权限清单

`07` 定律 4：**agent 只能改内核，不能改测量器。** 这条必须程序保证。

| 路径 | agent 权限 | 机制 |
|---|---|---|
| `work/candidate/**` | **读写**（唯一可自由写的代码目录） | — |
| `scratch/**` | 读写（不参与任何判定） | — |
| `docs/opt/**`、`journal/ledger.jsonl`、`knowledge/**` | **只能经 CLI 追加** | 不允许直接文件写 |
| `spec.toml`、`reference/**`、`harness/**`、`env.lock.json`、`receipts/**` | **只读** | 运行前摘要快照 + 运行后比对 |

两条抄自 `04_AKA` §3：
- **受保护集合按命名模式定义，不只按目录**（`*_test.py`、`*_harness.py`、`conftest.py` 这类文件无论放在哪都受保护）。
- agent 在别处**新建**的同类文件**丢弃而不判作弊** —— 分级处理，不要动不动上纲上线。

---

## 9. 项目结构与 CLI

```
kernel-opt-harness/
├─ spec.toml                  # 题目条件（人类所有，受保护）—— 精度阈值唯一来源
├─ env.lock.json              # 经你确认并冻结的软硬件事实（受保护）
├─ harness/                   # 核心（受保护）
│  ├─ interfaces.py           # ★ 可替换点抽象：换硬件只动这里
│  ├─ fsm.py                  # 确定性状态机
│  ├─ gates.py                # policy_gate（模型输出收紧）+ human_gate
│  ├─ probe/                  # 硬件·软件探针
│  ├─ verify/                 # 精度验证模块 + 变异哨兵
│  ├─ bench/                  # 唯一计时来源：5 配对交错 + bootstrap
│  ├─ adjudicate.py           # 判决（程序）
│  ├─ docgate.py              # 散文准入闸
│  ├─ guard.py                # 写权限 + 篡改检测
│  ├─ identity.py             # 测量身份哈希
│  └─ budget.py               # 预留式账本
├─ reference/                 # 参考实现（受保护）
├─ work/candidate/            # agent 唯一可写代码目录
├─ receipts/{verify,bench}/   # 机器写的证据（只追加）
├─ journal/ledger.jsonl       # 只追加流水账
├─ knowledge/laws.jsonl       # 从 receipt 幂等投影
├─ docs/
│  ├─ recon/                  # 环境探查报告
│  └─ opt/                    # ★ 人话散文
│     └─ rejected/            # 被否方向
├─ prompts/                   # analyze / propose / narrate / reflect 模板
└─ adapters/{dsh,claude,cli}/ # 宿主适配层（薄）
```

**`interfaces.py` 要定义的接缝**（抄 `01_KernelAgent` §1 规范 1，换硬件 = 换实现，主循环不动）：

```
HardwareProbe      软件探针           Builder/Toolchain
Verifier           Timer（唯一计时）   Profiler（可选，可能不存在）
Sandbox            ReferenceRunner
```

**CLI 面（唯一的真相来源，宿主只是调用它）**：

```bash
kopt probe                       # S0–S1，产出 docs/recon/<ts>.md
kopt gate-approve | gate-amend | gate-reject   # ⟦人类闸门 1⟧ → 冻结 env.lock.json
                                 #   实现在 probe-approve/-amend/-reject；
                                 #   单独起名是为了让宿主插件**不**把闸门动作
                                 #   暴露成 agent 工具（见 §9b D4'）
kopt calibrate                   # A/A 噪声地板 + 变异哨兵 + 基线收据
kopt verify   <candidate>        # 精度，→ receipt（PASS/FAIL/UNSUPPORTED/INCONCLUSIVE）
kopt bench    <candidate>        # 5 配对交错 + CI，→ receipt
kopt adjudicate <run>            # 判决
kopt narrate  <run>              # 生成冻结 front-matter + 散文骨架
kopt doc-gate <file>             # 散文准入校验，过了才落 docs/opt/
kopt guard verify                # 篡改检测
kopt budget status | reserve     # 预留式账本
kopt ledger append | list
```

**宿主适配为什么要薄**：`06_Xe-Forge` §1 同一套流水线做了 DSPy 全自动和 Claude Code 工作区两种形态，共享同一套工具，只有「谁来决策」不同。`07` §4 的结论是：

> 「轻」的那一版不是没有 harness，而是**把 harness 做成 CLI 工具，prompt 只负责决定何时调用**。

所以核心做成 CLI，DSH 插件 / Claude Code skill 都只是把 CLI 包一层 + 给一份 NEVER 清单。这也顺带解决了你上次遇到的那种宿主版本不兼容问题 —— 宿主换了，核心不用动。

---

## 9b. DSH 插件层（读过 DSH 0.2.1-rc.3 源码后的具体设计）

### 9b.1 DSH 的插件形态

DSH 用的是 **Cordis** 插件框架，一个插件就是一个导出四样东西的 ES 模块：

```ts
export const name = 'kopt'                       // 加载器身份
export const Config = z.object({ ... })          // schemastery 配置模式，挂载时校验
export const inject = ['tools', 'fs', 'subprocess', 'approval']   // 注入的服务
export function apply(ctx, config) { ... }       // 注册工具、挂事件
```

挂载是 profile 里的一段 YAML（`~/.dsh/profiles/<p>/cordis.patch.yml`），和官方插件完全同形：

```yaml
- name: '@you/dsh-kopt'
  config:
    projectRoot: /path/to/kernel-opt-harness
    python: python3
```

工具注册：

```ts
ctx.tools.register(defineTool({
  name: 'kopt_bench',
  description: '...',
  parameters: { ... },          // 类型化参数模式
  output: { schema, render },   // 给模型看的渲染
  async execute(args, exec) { ... },
}))
```

### 9b.2 最重要的发现：唯一计时来源在 DSH 里是**结构保证**，不是 prompt 承诺

`06_Xe-Forge` §6 那份 NEVER 清单（「永不自己写测速脚本」「唯一计时来源是一个命令」「永不把测量数字手打进结果」）之所以要写成 prompt，是因为它的宿主是 Claude Code —— **模型手里有自由的 bash，你只能请求它别用**。

DSH 的 `ctx.subprocess` 把这件事变成了物理约束。它的 README 写得很明确：

> 请求是完全显式的：程序与参数、工作目录、每个流的 stdio 处置、终止宽限期……**没有 shell 解释，也没有隐式的执行默认值**。

于是：

```ts
// core.ts —— 全插件唯一一处 spawn。工具只传结构化参数，永远不传命令字符串。
const exe = await ctx.subprocess.resolveExecutable(config.python)
const handle = ctx.subprocess.spawn({
  argv: [exe, '-I', '-m', 'kopt', 'bench', '--run', runId],   // 插件自己拼，模型给不了
  cwd: config.projectRoot,
  stdio: { stdin: 'ignore', stdout: { maxBytes: 262144 }, stderr: { maxBytes: 65536 } },
  graceMs: 5000,
  signal: exec.signal,
})
const { exitCode } = await handle.done
```

三点收益：

1. **argv 不经 shell**，模型无法注入；它能做的只是调用 `kopt_bench` 这个带类型参数的工具。
2. **`pairs` 不作为工具参数暴露**（§5.4），所以模型连"少测几次"都做不到。
3. `-I`（isolated 模式）顺手堵掉一个真实风险：被测目录里如果躺着一个 `json.py`，普通 `python -m` 会在 `import json` 时加载它。算子优化场景天天在读陌生仓库，这个必须默认开。

**但有一个前提必须说清楚**：这个保证成立的条件是 **agent preset 不同时挂 bash / pwsh**。如果挂了，模型照样能绕过去自己写测速脚本，保证就退化回 prompt 级别。所以建议给这个 harness 配**专用 preset**，只挂 `kopt_*` + 读写文件 + `ask_user`，不挂 shell。这是整个插件设计里最值得坚持的一条。

### 9b.3 人类闸门：`ctx.approval` 用对了地方才有用

我本来打算直接用 DSH 的审批服务做 recon 闸门，读了 `dsh-user-approval` 的源码后发现**不能那样用**。它自己列的限制：

> - **请求只在一个 open turn 内有效** —— 空闲或回合之间调用会直接抛错；**持久的跨回合审批流程尚未实现**。
> - **只有一次性授权** —— 没有 allow-always、没有记忆规则、没有授权存储。
> - **请求不携带工具参数** —— 应答方只看到工具名、理由和可选的 call id。

对照我的需求：闸门决定要**落盘、要能跨会话存活、要能让你逐字段修正**。`ctx.approval` 三条全不满足。**如果把闸门状态寄存在审批服务上，会话一恢复就全丢。**

但它在另一个位置正好合适。把「评审」和「授权」拆开：

| | 做什么 | 用什么 |
|---|---|---|
| **评审** | 把探查报告渲染给你看，你逐条核对 | 对话里渲染 + `ask_user`（修正走这里） |
| **授权** | 「冻结 env.lock」这个敏感动作本身 | `ctx.approval.request()` —— **你点一下，agent 伪造不了** |
| **状态** | 批准结果、署名、时间、修正项 | 落盘 `gate.json` + `env.lock.json`（唯一真相） |

这样拼起来恰好满足 `03_atrex` §5 那条最硬的规矩 —— **「agent 自己报的 Commit ID 永远不被接受」**。换到这里就是：**agent 说"用户已经同意了"不算数，必须有一次真实的审批决定。**

两个顺带的好处：

- DSH 的审批审计事件（`approval/asked` / `approval/decided`）是 **log-only、模型看不到**。验收记录天然在模型的叙述之外，正是定律 1 想要的。
- `policy: never` 时请求**直接 fail closed**。无人值守跑这个 harness，流程会**停在闸门上而不是默默放行** —— 这正是你要的行为，不用额外写代码。

`kopt_gate_amend`（逐字段人工修正）同样要过一次审批，每条修正单独授权，落盘时标 `source: human`。原因在 §3.4 说过：人也会记错，但至少要记清楚是谁记错的。

### 9b.4 包结构

```
dsh-kopt/                        # 插件仓库
├─ package.json                  # @you/dsh-kopt；peerDeps 见 §9b.5
├─ src/
│  ├─ index.ts                   # name / Config / inject / apply
│  ├─ core.ts                    # ★ 全局唯一 spawn 点，拼 argv
│  ├─ tools/                     # 每个工具一个文件，薄到只做参数校验 + 渲染
│  │  ├─ probe.ts  gate.ts       # kopt_probe / kopt_gate_{status,approve,amend}
│  │  ├─ verify.ts bench.ts      # kopt_verify / kopt_bench
│  │  └─ adjudicate.ts narrate.ts docgate.ts
│  └─ types.ts                   # 纯类型入口，不 import 宿主运行时代码
├─ skills/kopt/SKILL.md          # NEVER 清单 + 八段散文契约（按需加载，不占常驻上下文）
└─ python/kopt/                  # ★ Python 核心，随包分发（§9 的那棵树）
```

两条从官方包学来的做法：

- **`types.ts` 单独入口、不碰宿主代码**（`dsh-tool-present` 就是这么分的）。好处是 Python 核心的输出模式可以被别处复用，而不用把整个 DSH 依赖拖进去。
- **规则写成 skill 而不是常驻 instructions**。NEVER 清单 + 八段散文模板加起来不短，做成按需加载的 skill，不会每轮都烧上下文。

### 9b.5 版本策略：你上次踩的那个坑，这次要绕开

上次 `@xietwim/dsh-kernel-opt` 装不上，根因是它把 peer 范围的上界写成了 `<0.2`。我查了官方包的实际写法 —— 它们 peer 全部**精确钉死**：

```json
"peerDependencies": {
  "@deepseek-ai/dsh-tools": "0.2.1-rc.3",
  "@deepseek-ai/dsh-agent": "0.2.1-rc.3"
}
```

官方包能这么写，因为它们和宿主同仓同版本发布。**第三方插件照抄这个写法，等于每个 rc 都要跟一次版本**。

所以：

1. peer 范围按 minor 放宽（例如 `>=0.2.1-rc.3 <0.3`），**但必须实测**。只有 `tools / subprocess / fs / approval / agent` 这几个接缝是我们真正用到的，面小，跟版成本可控。
2. **真正的护城河不是版本范围，而是你选的那个架构** —— 逻辑全在 Python 里，TS 层只有拼 argv 和渲染。宿主接口变了，坏的是一层薄胶水，不是整个 harness。**这也是我赞同你选"Python 核心 + TS 适配层"的主要原因**，不只是因为 Python 离算子运行时近。
3. 装之前**先建一个空 profile 试**（`dsh plugin --profile <空 profile> add ...`），不要直接写进你在用的 web profile。

### 9b.6 工具面（对模型暴露的全部能力）

| 工具 | 模型能传什么 | 模型**不能**传什么 |
|---|---|---|
| `kopt_probe` | 无参数 | — |
| `kopt_gate_status` | 无参数（只读闸门状态） | — |
| `kopt_gate_approve` / `_amend` | 字段名、值、理由 | 不能跳过审批 |
| `kopt_calibrate` | 无参数 | 不能传 σ、不能传阈值 |
| `kopt_verify` | 候选路径 | **不能传容差、不能传 Python 代码** |
| `kopt_bench` | 候选路径 | **不能传配对数、不能传计时方法** |
| `kopt_adjudicate` | run id | 不能传判决结论 |
| `kopt_narrate` / `kopt_doc_gate` | 散文正文 | 不能改 front-matter |

这张表就是 §1 权威边界表的可执行版本。**每一个"不能传"都对应 `07` 里的一条定律**，而且是用参数模式堵死的，不是用措辞劝阻的。


---

## 10. 实施顺序

| 优先级 | 内容 | 不做会怎样 |
|---|---|---|
| **P0** | `spec.toml` + verify + 变异哨兵 | 没有可信的精度门，后面全是装饰 |
| **P0** | bench 5 配对交错 + bootstrap + 四种判决 | 「变快了」永远说不清是不是噪声 |
| **P0** | receipt + 测量身份哈希 + ledger | 三个月后没人能复现任何一条结论 |
| **P0** | probe + 人类闸门 1 + env.lock 冻结 | 结论不绑定环境，换台机器悄悄失效 |
| **P1** | narrate + doc-gate + `docs/opt{,/rejected}/` + 未归档闭锁 | 散文会慢慢退化成带数字的作文 |
| **P1** | guard 写权限 + 篡改检测 | agent 迟早会"顺手优化一下测量脚本" |
| **P1** | A/A 标定 → 动态接受阈值 + 配对数自动提升 | 阈值拍脑袋，5 对的分辨力是个谜 |
| **P2** | analyze/plan 分阶段 + 硬依赖 + policy_gate | 一次改多处，归因不清 |
| **P2** | budget 预留式账本 | 跑飞了才知道花了多少 |
| **P3** | knowledge 投影 + blacklist | — |

P0 这四项加起来就是一个**能用的最小系统**，而且它们**全是 Python，一行 TypeScript 都不需要**：先用裸 CLI 把流程跑顺，确认判决口径是你要的，再包插件。这符合 `07` §6 的投入产出排序。

**DSH 插件层单独排期**（§9b），建议放在 Python 核心的 P0 跑通之后：

| 顺序 | 内容 |
|---|---|
| D1 | `core.ts` 唯一 spawn 点 + `kopt_verify` / `kopt_bench` 两个工具 —— 最小可用插件 |
| D2 | `kopt_probe` + 闸门三件套（`ctx.approval` 接授权，`gate.json` 存状态） |
| D3 | `kopt_narrate` / `kopt_doc_gate` + skill 文件（NEVER 清单 + 八段模板） |
| D4 | 专用 agent preset（**不挂 shell**，这是 §9b.2 的结构保证能成立的前提） |

D4 容易被当成收尾的小事，实际上它是**整个防作弊设计的承重墙** —— 挂了 shell，前面所有的参数级约束都会退化成口头约定。

**实现后的三处偏离**（都在 `kopt-dsh/README.md` 里写了理由，这里只记结论）：

- **D1'**：`kopt_bench` 不作为独立工具存在，被折进 `kopt_verify`（S8 通过才计时）。
  理由：给了 agent 一个单独的计时按钮，它就多了一个「先测再说」的机会；
  而 S8 门在 Python 侧，只要它先跑过 `bench` 就绕开了。折起来以后，
  「只有在验证通过的候选上计时」是工具形状而不是纪律。
- **D2'**：闸门动作（approve / amend / reject）**不注册成工具**，只留 CLI 的
  `gate-*` 入口。`ctx.approval` 按 §9b.3 只能做「授权某一次不可逆动作」，
  做不了闸门状态；而把 `gate-approve` 暴露成工具，等于给了 agent 自己点通过的按钮。
- **D3'**：不做 `client.js`，没有 UI。这一层要的不是界面，是「没有第二条计时入口」。

---

## 11. 三个要提前知道的风险

**1. 5 配对的分辨力可能比你预期的粗。**
`05_cuda-opt-agent` §4 要求每个 shape 至少 3 个配对样本才给结论，5 对只是刚过及格线。真实后果是：**相当比例的改动会判 INDISTINGUISHABLE**。这是对的（假阴性远好过假阳性），但你得提前接受。A/A 标定会在第一天就把「5 对能分辨多少」这个数字摆在你面前；配对数自动提升（§5.4）能缓解，但代价是测得更慢 —— 这是个真实的取舍，不是免费的。

**2. 散文是整个设计里唯一不可机器校验的一环。**
doc-gate 能保证数字没编、结论没拧、结构齐全，但保证不了因果解释的正确性。所以 §7.1 的两条通道分离必须守住：**散文永远不能被下一轮当事实引用**。一旦有人图省事让 agent「读一下 docs/opt 再提方案」，这个设计的证据链就断了。真要给下一轮用，只能用 `knowledge/laws.jsonl`。

**3. 人类闸门会成为吞吐瓶颈。**
`06_Xe-Forge` 的 Claude 工作区模板里有一句「Nobody is watching this session, so never stop to ask」—— 它的假设和你完全相反。你要真人闸门，就意味着流程会停在那里等你。所以建议**只在两处强制停**（recon 批准、晋升主线），中间的 S4–S12 让它连续跑，跑完一轮把散文和判决一起端上来给你看。如果每轮都停，一天做不了几轮。

---

## 12. 四项决定（已定）

| # | 决定 | 落在哪 |
|---|---|---|
| 1 | 宿主 = **DSH 插件** | §9b 全节 |
| 2 | **Python 核心 + TypeScript 宿主适配层** | §9b.4 包结构、§9b.5 版本策略 |
| 3 | 被否方向与「分辨不出」**也写散文**，且**由程序闭锁** | §7.5 |
| 4 | 5 配对是**下限**，A/A 说不够时自动提到 7 / 9 | §5.4 |

### 还没定、但要动手前定的三件小事

1. **`projectRoot` 放哪**：harness 项目和被优化的算子仓库是同一个目录还是分开？分开更干净（受保护路径不和别人的源码混），但要多配一个路径。
2. **A/A 标定的有效期**：σ 会随机器负载变。建议绑在 `env.lock` 哈希 + 一个时限上（比如 24 小时），过期自动要求重标。时限多长要看你机器的实际稳定性。
3. **晋升闸门（人类闸门 2）要不要开**：开了更稳，但每轮都要你点一次。也可以先只开 recon 闸门，等流程跑顺再决定。

---

*设计依据全部来自 `/root/Agent_design/docs/`；DSH 接口部分来自 DSH 0.2.1-rc.3 实际源码。文中每条规矩都标了出处，方便你回查原文核对。*
