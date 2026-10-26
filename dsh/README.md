# kopt-dsh —— 把 kopt 包成 DSH 插件

这个目录是一个 **DSH 插件 bundle**（不是 app，不是客户端 UI）。它做的事只有一件：
把 Python 侧的 `kopt.cli` 子命令接到 DSH 的工具表上，并且**顺手关掉一条后门**。

```text
kopt-dsh/
├── package.json        # dsh.bundle.patch → cordis.patch.yml
├── cordis.patch.yml    # insert 一行：id=kopt-harness, name=@local/kopt-harness
├── index.js            # 唯一的宿主代码：注册工具、spawn CLI、注入一段系统提示
├── locale/{en,zh}.json # 插件管理器里显示的标题与描述
├── icon.svg            # 64×64
└── tools/smoke.mjs     # 不需要 DSH 也能跑的自检（见下文）
```

---

## 一、它为什么这么薄

设计文档 §9 的原话是：核心做成 CLI，宿主插件只是把 CLI 包一层。

薄的好处不只是省事。更关键的是**判定逻辑一份、计时器一份、收据格式一份**——
插件里没有任何一行代码知道「什么是快」，它只会问 Python。
所以你在终端里手敲 `kopt bench x.py` 和在 DSH 里让 agent 调 `kopt_verify`，
拿到的是同一套收据、同一套判决。

---

## 二、十一个工具，和那个故意不存在的

| 工具 | 对应阶段 | 说明 |
|---|---|---|
| `kopt_probe` | S0/S1 | 探查硬件软件，写 `docs/recon/`，闸门置 pending |
| `kopt_probe_status` | — | 看闸门状态与环境哈希 |
| `kopt_calibrate` | S3 | A/A 噪声地板 |
| `kopt_verify` | S8 → S9 | **验证，通过才计时**，一起返回 PASS/FAIL 与加速比 |
| `kopt_adjudicate` | S10 | ACCEPT / REJECT / INDISTINGUISHABLE / SUSPECT / INVALID |
| `kopt_narrate` | S11a | 写出 `front-matter.json` 与「正文只许用的数字」 |
| `kopt_doc_gate` | S11b | 校验散文：结构、冻结字段、数字出处 |
| `kopt_pending` | — | 还欠着没写的散文 |
| `kopt_propose` | S6 | 申请开始下一轮；有欠账就拒 |
| `kopt_guard` | — | 路径写权限裁决 |
| （无） | S9 | **没有 `kopt_bench` 工具** |

最后一行是这份插件里最重要的设计决定。Python 侧的 `bench` 子命令还在（人是可以用的），
但**不注册成工具**：候选只有在 `kopt_verify` 里、而且验证通过之后才会被计时。

如果给了 agent 一个独立的计时按钮，它就有了「先测再说」的机会，而 S8 门在 Python 侧——
只要它先跑 `bench`，验证通过与否就不再是计时的前提。折成一个工具以后，
「只在验证通过的候选上计时」是**工具的形状**，不是一条它可以选择遵守的纪律。

同理，闸门三件套（approve / amend / reject）也没有注册成工具。它们只在 CLI 上，
入口名叫 `gate-approve` / `gate-amend` / `gate-reject`。
`ctx.approval` 按设计 §9b.3 只适合给「一次不可逆动作」做授权，不适合存闸门状态；
而把 `gate-approve` 暴露成工具，等于给 agent 一个「自己点通过」的按钮。

---

## 三、承重墙：没有第二条计时入口

插件只通过 `ctx.subprocess.spawn({ argv })` 起进程，并且只起一个程序：
`<python> -m kopt.cli …`。

`spawn` 用的是显式 argv、**不经 shell**。所以：

- 候选文件名里写 `; rm -rf /` 也只是一个文件名，不会变成一条命令；
- agent 想自己写个 `time.perf_counter()` 的脚本测加速比，在**没有 shell 的 preset 里做不到**；
- 只要它做得到，配对 bootstrap 的判决就退化成「模型说它更快」。

这条保证的成立条件是**agent preset 不挂 bash/pwsh**。挂了 shell，前面所有参数级约束
都会退化成口头约定。这是设计 §9b.2 / D4 的原话，也是这个插件唯一依赖的外部条件。

---

## 四、安装

推荐用 `plugin_manager`，**不要**手写 profile 里的 `package.json` 或 `cordis.patch.yml`：

```text
plugin_manager action=install_bundle target=<kopt-dsh 的绝对路径>
```

安装前需要先在 `cordis.patch.yml` 里补上 `koptHome`（见下一节），因为默认值是空的，
插件会以 warn 启动、所有工具都会失败 —— 这是故意的：**配置缺失要吵，不要静默降级**。

之后用 `plugin_manager action=list_bundles` 拿准确的 id，用 `set_bundle` 开关。

---

## 五、配置

```yaml
# WSL / Linux
- insert:
    - id: kopt-harness
      name: '@local/kopt-harness'
      config:
        pythonPath: '/root/kopt-workspace/.venv313/bin/python'
        koptHome: '/root/kopt-workspace/kopt'

# Windows
#         pythonPath: 'C:\Users\abczzz\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
#         koptHome: 'C:\Users\abczzz\Documents\deepseek-harness\default-workspace\kopt'
```

| 字段 | 默认 | 含义 |
|---|---|---|
| `pythonPath` | `python` | 解释器。**建议写绝对路径**——这台机器 PATH 上没有 `python` |
| `koptHome` | 空 | `kopt` 包所在目录（含 `kopt/cli.py` 的那一层）。也作为子进程的 cwd 与 `PYTHONPATH` |

项目根：取当前会话的工作目录。所以「让 agent 优化哪个项目」= 「在哪个目录里开会话」。

---

## 六、自检（不需要装 DSH）

```bash
cd /root/kopt-workspace/kopt-dsh
export KOPT_PYTHON=/root/kopt-workspace/.venv313/bin/python
node tools/smoke.mjs /root/kopt-workspace/kopt /root/kopt-workspace/kopt/examples/demo
```

Windows 上把 `KOPT_PYTHON` 换成宿主解释器的绝对路径、路径换成 Windows 形式即可。

它用一个假的 `ctx` 记录注册结果，用一个真的子进程跑 CLI。验证的是：

- 工具名/描述/参数/execute 形状合法；
- `kopt_bench` **没有**出现在工具表里；
- 系统提示段注册成功；
- `kopt_guard` 对 `spec.toml` 返回 6、对 `work/candidate/` 返回 0；
- S8 失败时 `kopt_verify` 明确汇报「S9 跳过」，且输出里不出现任何加速比；
- argv 不被 shell 解释（`a;b.py` 原样传到 Python）。

它**不**验证：bundle 能否被 DSH 加载、模型看到的 schema 是否正确、UI 是否渲染。
那三件事只能装上以后看。

在沙箱里跑会失败（`spawn EPERM`，受控环境不允许 node 起子进程），需要完整权限。
