# Forge 知识库 设计 v0.1

> 与 `kopt-harness-design.md` 配套。那份管「怎么测量、怎么判决」，这份管
> 「判决产出的经验怎么存、怎么找、怎么用、怎么防止污染」。
>
> 名字：**Forge**（Ag=Agent 层，见 §0.3 命名对照）。仓库：`Forge-KnowledgeHub`。

---

## 0. 一句话

> **知识库不是「写到什么」，是「什么有资格进来」。**

你这套构思（硬件目录 / 策略目录 / 算子目录 + 总索引 + 细分索引）解决的是
**存储与检索**。它缺的那一半是**准入与衰减**——什么条件下一条经验算成立、
什么条件下它失效、失效了谁来关掉它。缺了这一半，知识库会变成一份越写越长、
越写越像、但越来越不能信的文档集合。这就是本设计的**主轴**。

---

## 0.1 你提的原始构思（照录，不改写）

1. 分别创建：硬件信息目录、针对不同硬件总结的不同优化策略、针对不同算子的不同项目的优化策略；
2. 管理访问参考数据库：创建各级索引文档（总索引 + 细分索引）；

    - 总索引 = 指示需要查找的知识的大概方向、以及所归档的文件夹名称；
    - 细分索引 = 定位具体文章。

## 0.2 我补的三件（以及为什么）

| 补什么 | 你的构思里缺的位置 | 不补的后果（不是理论，是能预见的） |
|---|---|---|
| **准入闸** | 怎么进来 | 一条没测过的「经验」和一条测过 5 对的结论并排躺在同一个目录里，读者分不出来 |
| **有效期** | 什么时候失效 | 上个月在 32 核赛扬上测得 12x，这个月换机器，索引还在指它 |
| **失效处置** | 失效了怎么办 | 只增不删 → 索引越全越不能信，人最后靠记忆而不是靠索引 |

另外补两件结构性的：

- **一条经验只能有一个家**（§3）：你的三个目录（硬件 / 硬件策略 / 算子×项目）
  本质上是在给同一样东西建三次索引。正确做法是**只存一份，多路索引**。
- **索引要能生成**（§6）：索引手写必腐烂。程序生成，人只写「导读」。

---

## 0.3 命名对照（避免和旧名字混淆）

| 层 | 名字 | 位置 |
|---|---|---|
| Agent 整体 | **Forge** | — |
| 工具链 / 内核（现 `kopt` Python 包 + `kopt-dsh` 插件） | **Forge Core** | `/root/forge/core`（本轮未改目录名，见 §10） |
| 知识库 | **Forge KnowledgeHub** | `https://github.com/Welmstr/Forge-KnowledgeHub` |
| 单个优化项目 | Forge Project | 任意目录，内含 `spec.toml` |

代码标识符（`kopt.cli`、环境变量 `KOPT_PYTHON`）本轮**刻意不改**：改名字是
机械劳动，但会让已经跑通的证据链（收据、`measurement_id`、散文里的
`run_id`）指向一个不存在的命令。见 §10。

---

## 1. 先划三刀：哪些东西**不是**知识

这是整套设计的成败处。Forge 自己产出的东西目前是三类，混在一起就全废：

| 产物 | 谁写 | 能不能进知识库 | 为什么 |
|---|---|---|---|
| 收据 `receipts/*.json` | 程序 | **不进**（它是原件，留在项目里） | 知识库引用它，不复制它 |
| 散文 `docs/opt/*.md` | 模型写、程序校验 | **不进**（见下） | 设计 §7.1：散文是给**人**看的，不可被当事实引用 |
| 知识条目 `knowledge/*.jsonl` | 程序从收据投影 | **进** | 唯一有资格被下一轮检索的东西 |

### 1.1 为什么散文不能进知识库

`kopt-harness-design.md` §7.1 写死了两条通道分离，§11 风险 2 又说了一遍：
doc-gate 能保证「数字没编、结论没拧、八段齐全」，**保证不了「因果解释对不对」**。
一旦让 agent「先读一下散文再提方案」，证据链当场断掉：因为你分不清它读到的是
「实测 1.03–1.07 倍」这个事实，还是「因为访存是瓶颈」这个**没被测过**的解释。

散文进知识库的正确方式只有一种：**人读完之后，把可复用的部分重述成一条
`HUMAN` 条目**（§4）。此时作者是人，责任在人，可以标注、可以撤回。

### 1.2 三个「不许」

1. 知识库里**不许出现**「估计 / 大概 / 应该是 / 一般来说」修饰一个测量值。
   要写测量值就给区间和出处；给不出就不许写。
2. 知识库里**不许**出现没有 `anchors`（出处）的条目。查不到出处的经验等于
   谣言，哪怕它是对的。
3. 知识库里**不许**只增不删。有 `expires_when` 就必须有人能触发它。

---

## 2. 三层内容 + 三层索引

### 2.1 内容分三层（你的三个目录）

```
L0 硬件层   一个硬件配置 = 一台机器/一个集群节点的事实
           来源：Forge 的 probe（S0）自动产出，几乎不需要人写
L1 策略层   一类硬件上「什么手段有效/无效」
           例子：AVX-512 上 gather 慢于连续访存 + permute
L2 项目层   某个算子在某个项目里的具体做法
           例子：kernel-X 的 reduce 在 shape [1024,256] 下分块 8 倍最稳
```

三层之间是**引用**关系，不是复制关系：

```
L0 硬件事实  ←───  L1 策略（写死它只在哪些 L0 上成立）
                     ↑
L2 项目条目（引用 L1 的条目 id，并写清楚自己偏离了什么）
```

### 2.2 三条索引正交（对应你说的「总索引/细分索引」，但要拆细）

一个非技术读者最容易在这里误解：**「总索引」不是一个文件，是一条轴**。
有三条正交的轴，每条轴都有自己的总览和细分：

| 轴 | 回答的问题 | 总览 | 细分 |
|---|---|---|---|
| **A. 硬件轴** | 我这台机器上什么结论成立？ | `00-START-HERE.md` | `index/by-hw/<hw_id>.md` |
| **B. 算子轴** | 这个算子在别的项目里怎么做的？ | 同上第 2 节 | `index/by-op/<op>.md` |
| **C. 手段轴** | 这个手段在什么情况下有效？ | 同上第 3 节 | `index/by-technique/<tag>.md` |

外加两条「安全网」索引：

| 轴 | 回答的问题 | 文件 |
|---|---|---|
| **D. 状态轴** | 哪些结论已经过期/被推翻？ | `index/expired.md`、`index/superseded.md` |
| **E. 待裁决** | 有没有冲突条目？ | `index/conflicts.md` |

这五条索引**全部由程序生成**（§6）。你只需要维护每篇文章自己的 front-matter。

---

## 3. 一条经验只能有一个家（唯一性规则）

这是本设计对你的构思唯一的**结构性改动**，其余都是补充。

### 3.1 规则

> 每一条知识条目有且只有一个**物理位置**（它属于哪个 `hw / op / technique / 项目`），
> 其余的归属全部通过 front-matter 字段表达，由索引多路暴露。

### 3.2 怎么决定它住哪

问一个问题：**这条经验改变的是「在哪做」还是「怎么做」？**

```
「换个硬件就换策略」→ 这条住 L1，hw 字段列出它适用的所有硬件
「换个项目就换做法」→ 这条住 L2，op 字段标算子，hw 字段标测它的机器
「这是某台机器本身的事实」→ 这条住 L0，由 probe 生成，人只在修正时动手
```

### 3.3 为什么不能抄三份

抄三份的后果不是「多占空间」，是**三个副本会分叉**：改了其中一份，另外两份
变成幽灵。数据库不会为同一条记录建三个主键；这里同理，多命中靠索引，不靠复制。

---

## 4. 条目格式

### 4.1 存储 `entries/*.md`（人读）+ `manifest.json`（程序读）

**一条经验 = 一个 md 文件 + 在 manifest 里的一行**。md 给人看，manifest 给索引
生成器和校验器看，两者由程序保证一致（`forge kb check` 会比对 md 头的
front-matter 与 manifest 行）。

`entries/hw/<hw_id>.md` —— L0 硬件事实：

```markdown
---
id: HW-L0-0001
layer: L0
hw_id: amd-epyc-9654-qemu32
provenance: MEASURED
measured_at: 2026-10-26T10:41:00+08:00
measurement_env_lock: 3202b8af193a          # ← kopt env.lock 摘要
---

## 这台机器是什么
32 逻辑核，AVX-512F 可用，AVX-512 BF16 不可用（见下）。

## 事实表
| 键 | 值 | 置信度 | 出处 |
|---|---|---|---|
| cpu.model | AMD EPYC 9654 32-Core | MEASURED | `kopt probe` |
| cpu.avx512bf16 | False | MEASURED | `kopt probe` |
| mem.bandwidth_gbs | unknown | UNKNOWN | 未测 |
```

`entries/strategy/<slug>.md` —— L1 策略：

```markdown
---
id: STR-0007
layer: L1
title: AVX-512 上 gather 慢于「连续访存 + permute」
techniques: [memory-access, avx512, simd]
applies_when:
  hw_features: [avx512f]
  hw_excludes: []
verdict: SUPPORTED          # SUPPORTED | REFUTED | OPEN
provenance: MEASURED
evidence:
  - run: r0002
    project: demo
    measurement_id: m-7c31be
    speedup_ci95: [1.034, 1.071]
    env_lock: 3202b8af193a
expires_when:
  - env_lock_changed
  - not_retested_for_days: 180
supersedes: []
superseded_by: null
reviewed_by: human
---

## 这条经验说什么
…（三到五句，必须含一个可证伪句和一个边界句）

## 在什么条件下成立
…（必须写：硬件特征、数据规模、编译器版本）

## 什么会推翻它
…（具体的、可观测的反例条件）

## 出处
…（复述 evidence 里的 run_id，并说明读收据时看到了什么）
```

`entries/project/<project>-<op>-<slug>.md` —— L2 项目条目：

```markdown
---
id: PRJ-demo-reduce-0003
layer: L2
project: demo
op: reduce
techniques: [loop-unroll, cache-blocking]
applies_when:
  hw_features: [avx512f]
  dtype: [fp32]
  shape: "1024x256"
verdict: INCONCLUSIVE       # 注意：这里可以出现 INCONCLUSIVE，它也是知识
provenance: MEASURED
evidence: [...]
expires_when: [env_lock_changed, spec_min_speedup_changed]
---

## 做法
## 有效/无效到什么程度（给区间，不给点估计）
## 在别的算子/项目上试过吗
## 边界
```

### 4.2 五条字段规矩

1. **`verdict` 是封闭集合**：`SUPPORTED | REFUTED | OPEN`（策略层）/
   `ACCEPT | INDISTINGUISHABLE | REJECT | INCONCLUSIVE`（项目层）。
   没有「大致成立」。这一条和 kopt 的判决词表同源。
2. **`applies_when` 是准入条**：它为空的条目**不许**被检索命中（见 §5）。理由是
   没有适用条件的经验等于「什么时候都对」，而那种话通常是错的。
3. **`evidence` 每条都必须有 `measurement_id` 或 `env_lock`**：查不到就拒收。
4. **`expires_when` 至少一项**：这个字段存在的唯一目的就是让知识会死。
5. **`reviewed_by` 只有两个值**：`probe`（程序自动生成）/ `human`（人确认过）。
   `human` 的条目在被推翻时需要人再确认一次，这是刻意的摩擦。

---

## 5. 写入路径：只有四条能进库

这部分是「数据库管理办法」里最像 DBA 权限模型的地方。

| 入口 | 谁能写 | 准入闸 | 产出 |
|---|---|---|---|
| **P1 自动投影** | 程序 | `kopt` 收据 + 判决 | L0 硬件事实、L2 带证据条目（`provenance: MEASURED`） |
| **P2 人写** | 你 | `forge kb check` 静态校验 | L1 策略条目 |
| **P3 外部资料** | 你 | 人工标注来源 | `ref/` 参考资料，**默认不可被检索命中**（见 §5.2） |
| **P4 投稿** | agent 提案 | **必须人批**才变 P1/P2 | 草稿区 `inbox/`，过闸前不进索引 |

### 5.1 P1 自动投影怎么保证幂等

复用 kopt 的做法（设计 §7.1 的 `project_knowledge`）：`kopt/laws.jsonl` 由
收据**投影**而来，删掉可以重建。知识库里对应的部分是
`entries/project/**` 里 `provenance: MEASURED` 的条目——它们由
`forge kb project --from <项目目录>` 生成，**不许手改**（手改了下次投影会被覆盖，
并且 `check` 会报冲突）。

推论：一条 L2 条目如果既要程序数字又要人话解释，拆成两条：
`provenance: MEASURED`（程序管） + `provenance: HUMAN`（你管，引用前者）。

### 5.2 P3 外部资料为什么要隔离

你以后一定会往库里塞论文、博客、别人家的 kernel 代码。这些**不是**你的证据。
把它们和实测结论并排放，读者分不出哪条是自己测的、哪条是抄的。

所以：外部资料一律进 `ref/`，`provenance: EXTERNAL`，**默认不参与检索**，
只有显式 `--include-external` 时才出现，并且每条都标 `not_measured_here: true`。

---

## 6. 索引：生成，不手写

### 6.1 硬规矩

> 索引是**派生**的。任何一个 `index/*.md` 都不许手改，`forge kb index` 会覆盖它。
> 人只有两个可写的地方：`00-START-HERE.md` 顶部的导读段，和条目本身。

理由：你原来的方案里「总索引负责指引方向」是**判断**，判断要人写；但「文件
列表、命中计数、过期标记」是**事实**，事实该由程序算。手写必腐烂，且腐烂时
没有任何信号。

### 6.2 生成的索引长什么样

`00-START-HERE.md`（总索引，半自动：导读段人写，下面四节程序生成）：

```markdown
# Forge 知识库 · 从这里开始

<!-- 人写，约 10 行：这个库怎么用、最近在攻什么、哪些结论你亲自确认过 -->
本周在攻 reduce 的分块，重点看 STR-0007 和 PRJ-demo-reduce-0003。

## 1. 按硬件找
| 硬件 | 平台上有什么 | 有效策略数 | 未复核 | 最后更新 |
|---|---|---|---|---|
| amd-epyc-9654-qemu32 | Zen4/AVX-512/32 核 | 12 | 3 | 2026-10-26 |

## 2. 按算子找
| 算子 | 项目条目 | 其中 ACCEPT / IND / REJECT | 最近一条 |
|---|---|---|---|
| reduce | 4 | 1 / 2 / 1 | PRJ-demo-reduce-0003 |

## 3. 按手段找
| 手段 | 条目 | 结论分布 | 最容易踩的坑 |
|---|---|---|---|
| gather | 3 | 1 SUPPORTED / 2 REFUTED | 见 STR-0007 |

## 4. 需要处理
- **已过期**：5 条（[明细](index/expired.md)）
- **有冲突**：1 对（[明细](index/conflicts.md)）
- **待审投稿**：2 条（[明细](inbox/README.md)）
```

`index/by-hw/<hw_id>.md`（细分索引，纯程序生成）：

```markdown
# 硬件 amd-epyc-9654-qemu32

> env_lock `3202b8af193a` · 机器事实见 [HW-L0-0001](../../entries/hw/amd-epyc-9654-qemu32.md)
> ⚠ 有 3 条结论的 env_lock 与当前不符 —— 它们不是在这台机器上测的

## 这台机器上成立（SUPPORTED / ACCEPT）
| id | 一句话 | 置信 | 出处 |
|---|---|---|---|
| STR-0007 | AVX-512 上 gather 慢于连续访存 | MEASURED | r0002 |

## 只在这里试过、结论是否定（REFUTED / REJECT）
| id | 一句话 | 出处 |
|---|---|---|

## 没测出差别（INDISTINGUISHABLE / INCONCLUSIVE）
…
```

**「⚠ env_lock 不符」那一行是本设计最想让你看的一行**：它把「换机器后旧结论
自动显形」做成了索引里的固定栏目，而不是靠人记得。

### 6.3 索引规模的问题（什么时候该换方案）

平原文件 + 索引适合几十到几百条。量化边界：

- 条目 **< 300**：现在这套就够，`grep` 比什么都快。
- **300–3000**：生成本地 `index/manifest.sqlite`（同源生成），检索仍走索引文档，
  sqlite 只用来做「没有索引命中的兜底查找」。
- **> 3000**：这时候才谈向量检索。先问一句：是不是该删了？

我会把这条写进 `forge kb doctor` 的输出里，让它自己提醒。

---

## 7. 检索：给 agent 和给人的两条路

### 7.1 给人：从总索引往下点

和图书馆一致：总索引 → 细分索引 → 条目。不需要额外工具。

### 7.2 给 Forge：`forge kb find`（**唯一的检索入口**）

```bash
forge kb find --hw amd-epyc-9654-qemu32 --op reduce --technique cache-blocking
forge kb find --tags gather --status valid --limit 10
```

输出不是「一堆文件路径」，而是**带状态的结构化结果**：

```
STR-0007  SUPPORTED   MEASURED   applies: avx512f     evidence: r0002 (m-7c31be)
  ⚠ not retested for 121 days; expires in 59 days
```

### 7.3 检索时必过的三道闸（这是「访问」最容易被漏掉的部分）

| 闸 | 规则 | 为什么 |
|---|---|---|
| **G1 硬件匹配** | `applies_when.hw_features` 与当前 `env.lock` 不匹配 ⇒ 降权并标 `not_for_this_machine` | 在别人机器上测的结论，别直接套 |
| **G2 状态过滤** | 默认只返回 `valid`；`EXPIRED` 需 `--include-expired` | 过期结论不是错误，是目前不成立 |
| **G3 默认排除外部** | `EXTERNAL` 需 `--include-external` | 见 §5.2 |

**禁止的用法**（要写进 agent 的 NEVER 清单）：让 agent 直接读 `entries/**` 或
`index/**` 文件。它必须走 `forge kb find`，这样三道闸才拦得住。这和 kopt
「没有第二条计时入口」是同一种结构。

---

## 8. 失效与冲突：知识必须会死

### 8.1 四种失效触发器

| 触发器 | 由谁算 | 例子 |
|---|---|---|
| `env_lock_changed` | 程序（比对摘要） | 换了机器/编译器 ⇒ 所有绑旧 `env_lock` 的条目降级为 `stale` |
| `not_retested_for_days: N` | 程序（看 `measured_at`） | 180 天没复核 ⇒ 标黄，仍可读 |
| `spec_min_speedup_changed` | 程序（比对项目 spec） | 目标从 2x 提到 4x ⇒ 原来 ACCEPT 的项目条目要重判 |
| `human_revoked` | 你 | 你发现解释错了 ⇒ `REVOKED`（比删除好，见 8.3） |

### 8.2 冲突：同 `applies_when` 下两条相反结论

不是错误，是**信号**——它通常说明某条经验的条件写漏了。处理流程：

1. `conflicts.md` 自动列出冲突对；
2. 你只做一件事：**找出那条该被细化或推翻的条目**（往往是 `applies_when` 少写了一条）；
3. 修完跑 `forge kb check`，冲突消失说明条件补全了。

### 8.3 为什么是 `REVOKED` 而不是删除

因为「这条被推翻过」本身是知识。删掉以后下一轮很可能重新踩一遍——
这正是 kopt 里 `docs/opt/rejected/` 存在的理由，同一条道理在这里也成立。

---

## 9. 和 Forge 的接口

| 时机 | 动作 |
|---|---|
| S0 probe 出了 `env.lock` | `forge kb upsert-hw --from-env-lock` → 落 L0 条目，或发现已有更准的常识时只做比对 |
| S4 分析前 | `forge kb find --hw ... --op ... --technique ...` 取先验，**作为假设来源、不作为结论** |
| S6 提案时 | 每条提案必须写 `based_on: [STR-0007]` 或 `based_on: []`（明说自己没参考） |
| S10 判决后 | `forge kb project --from <项目>` 把 `MEASURED` 条目投影进库 |
| 每轮结尾 | `forge kb check` 进 CI，红则不许合并 |

**一条铁律**：`based_on` 引用的条目如果后来被 `REVOKED`，当初引它的那个提案
会被标出来（`grep` 一下就知道谁建立在废墟上）。

---

## 10. 仓库落地形态与迁移

### 10.1 已落地（2026-10-26，WSL `/root/kopt-workspace/forge-kb/`）

```
forge-kb/
├─ README.md                    # 人入口：这个库装什么、什么不许进
├─ 00-START-HERE.md             # ★ 总索引（导读段人写，其余生成）
├─ entries/
│  ├─ hw/                       # L0（现在为空，等 probe 第一条）
│  ├─ strategy/                 # L1（现在为空，第一条得你写）
│  └─ project/                  # L2：PRJ-demo-r0002 / r0003 / r0004 ← 真收据投影
├─ index/
│  ├─ by-hw/avx512f.md          # 三个维度的细分索引，全部生成
│  ├─ by-op/reduce.md
│  ├─ by-technique/cache-blocking.md
│  └─ expired.md  superseded.md  conflicts.md
├─ ref/_inbox/                  # 外部资料暂存（默认不检索）
├─ inbox/                       # agent 投稿
├─ schema/  docs/
└─ tools/kbcheck.py             # check / index / find / project
```

`tools/kbcheck.py` 现在真的能跑，四条命令都验过：

| 命令 | 验证方式 | 结果 |
|---|---|---|
| `project` | 直接读 `kopt/examples/demo/receipts/adjudicate/` | 投影 3 条，SUSPECT 的 r0001 被跳过 |
| `check` | 塞进一条**故意写坏**的条目 | 抓出 4 个错误（来源非法、无适用条件、无有效期、无 timestamp）+ 2 个提示 |
| `find` | 换一个假 env_lock / 换一组硬件特征 | 旧结论标 `stale`；硬件不够的标 `not_for_this_machine` |
| `index` | 改 env_lock 后重生成 | `index/by-hw/*.md` 顶部出现「⚠ N 条不是在这个环境上测的」 |

**投影时撞到两处现实，值得记下来**（都属于「想当然的格式 vs 真实数据」）：

1. 我原以为收据是嵌套的 `{"bench": {"point": ...}}`，
   实际 kopt 的判决收据是扁平的 `{"point": ..., "ci_low": ..., "ci_high": ...}`。
   第一次投影出来 `unknown`，`check` 没报错——**因为它不检查项目条目里的加速比**。
   这是个真缺口，已补：找不到就写 `unknown` 并在正文里明说。
2. id 原设计是 `PRJ-<project>-0003`（纯数字序号），但序号天然是 `run_id`（`r0002`）。
   改成 `PRJ-demo-r0002` 并放宽正则——**编一个序号等于编一个回溯不了的主键**。

### 10.2 还没做的

- **没往 GitHub 推**。这台 WSL 里 `git config user.name` 是 `Zhang San` /
  `zhangsan@qq.com`，本地也没确认过有能推 `Welmstr/Forge-KnowledgeHub` 的凭据。
  在作者身份是假的情况下 commit，等于给你留一堆假署名。所以本地 `git init` 好、
  `origin` 也配好了，**推送这一步等你确认身份**。
- L0 条目一条都没有：需要先把 `kopt probe` 的 `env.lock` 接到
  `kb upsert-hw`（现在只能靠人照着 probe 报告手写）。
- `check` 目前**不校验**「正文里说的加速比和 evidence 里的区间是否一致」。
  这是该补的下一条规则（散文有 doc-gate 管，知识条目还没有）。

---

## 11. 下一步（按投入产出排序）

| 顺序 | 做什么 | 为什么先做这个 |
|---|---|---|
| 1 | 落地目录骨架 + `00-START-HERE.md` + 空的三层 `entries/` | 先有个能放东西的地方 |
| 2 | 写 `tools/kbcheck.py`（校验 + 生成索引），接进 Forge | 没有闸的库三个月后就是作文集 |
| 3 | 把 `kopt examples/demo` 那四次真实判决投影成 L2 条目 | 用真数据验证格式，别用假数据 |
| 4 | 用 `probe` 的真实输出生成第一条 L0 条目 | 验证 `env_lock → hw_id` 这条链 |
| 5 | 你亲手写 3 条 L1 策略 | 检验 `applies_when` 够不够用 |
| 6 | `forge kb find` 出现并接进 agent 的 NEVER 清单 | 完成闭环 |

**明确不做**：向量库、Web 界面、自动抓取论文。理由是 §6.3 —— 在条目数到 300
之前，这些都是把简单问题复杂化。

---

## 12. 需要你拍板的四件事

1. **`entries` 三层够不够**？是否需要第四层「编译器/工具链」维度
   （同一个核在 `gcc -O3` 和 `clang -O3` 下的结论不同，常见的坑）。
2. **P3 外部资料要不要现在就开**？开了就意味着你得为每条标注来源可信度。
3. **`not_retested_for_days` 取多少**？我默认 180，你如果有更保守的直觉就改。
4. **推送权限**：给你的是本地骨架，要不要我配好远程并推第一次（需要你确认
   git 身份用哪个）。
