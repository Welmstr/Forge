/**
 * Host half of the kopt harness bundle.
 *
 * The harness itself is a Python CLI (`kopt.cli`). This plugin is a *thin
 * adapter*: it turns the CLI's subcommands into tools and — more importantly —
 * makes the model unable to measure time any other way.
 *
 * The load-bearing decision: there is no `bench` tool. A candidate is timed
 * only as a side effect of `kopt_verify`, and only after that same verification
 * passed. The model may decide *what to try*; it may never decide *what counts
 * as correct* or *how fast it was*. If the model can run its own timing loop,
 * the whole paired-bootstrap verdict degenerates into "the model says it's
 * faster". And it cannot run one: this plugin shells out through
 * `ctx.subprocess.spawn({ argv })`, never through a shell, and a preset that
 * also mounts bash/pwsh would break the d4 guarantee — see README.
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import z from '@deepseek-ai/schemastery'
import { appendFileSync, existsSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { tmpdir } from 'node:os'

/**
 * 把「裸命令名」变成真实路径。
 *
 * 为什么不能直接把配置里的字符串交给 spawn：Windows 上如果有 `python`
 * （比如 Store 版的别名或某个 .cmd 垫片），它解析出来的**不是**我们装
 * `kopt` 的那个解释器。插件里能看到的一切，都是这个进程跑出来的——
 * 所以要钉死在真实路径上，包括 `.exe` 后缀。
 */
function resolveExe(cmd, pythonPath, wslStyle) {
  const bare = String(cmd).split(/[\\/]/).pop() ?? ''
  const isPython = ['python', 'python3', pythonPath.split(/[\\/]/).pop()].includes(
    bare.toLowerCase(),
  )
  if (!isPython) return cmd
  const wantsExe = (p) => (wslStyle ? p : /\.exe$/i.test(p) ? p : `${p}.exe`)
  const candidates = []
  if (isAbsolute(pythonPath)) {
    candidates.push(wantsExe(pythonPath))
  } else {
    for (const dir of (process.env.PATH ?? '').split(wslStyle ? ':' : ';')) {
      if (!dir) continue
      candidates.push(`${dir}${wslStyle ? '/' : '\\'}${wantsExe(pythonPath)}`)
    }
  }
  for (const c of candidates) {
    if (existsSync(c)) return c
  }
  return pythonPath
}

/** Python interpreter and CLI package root; overridable through Config. */
const DEFAULTS = {
  pythonPath: 'python',
  koptHome: '',
  // Where the event log is checked out. This is the Forge-Logs repo, NOT the
  // project: 知识库装结论，日志装过程，两个库不混。空 = 日志关闭（并且会吵）。
  logRoot: '',
}

/**
 * 配置形状。
 *
 * 宿主会拿这份声明去校验 `cordis.patch.yml` 里写的 `config`，所以字段名
 * 写错、类型写错（比如 logRoot 写成 `~/.dsh` 这种字符串以外的类型）会
 * **在启动时**报出来，而不是在第一次调用工具时才炸。没有这段声明，宿主
 * 只会记一句"这是未知配置"，然后把错的东西原样交给我们。
 *
 * 注意：这是 Schemastery 形状（宿主用的就是它），不是 JSON Schema。
 * 包名是 `@deepseek-ai/schemastery`，默认导出就是 `z`。
 */
export const Config = z.object({
  pythonPath: z
    .string()
    .description('跑 kopt 的解释器。会被解析成真实路径（Windows 上补 .exe）。')
    .default(DEFAULTS.pythonPath),
  koptHome: z
    .string()
    .description('包含 kopt 包的那个目录（注意：是 kopt 的父目录，不是 kopt 本身）。')
    .default(DEFAULTS.koptHome),
  logRoot: z
    .string()
    .description('日志与注册证据写在哪。空 = 日志关闭。')
    .default(DEFAULTS.logRoot),
  sessionId: z.string().description('日志会话 id。留空则自动生成。').default(''),
})

const ENV = {
  // Force UTF-8 so Chinese prose survives the pipe on Windows.
  PYTHONIOENCODING: 'utf-8',
  PYTHONUTF8: '1',
}

function stripAnsi(text) {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\u001b\[[0-9;]*m/g, '')
}

function tail(text, maxChars = 12000) {
  if (text.length <= maxChars) return text
  return `…(${text.length - maxChars} chars trimmed)…\n${text.slice(-maxChars)}`
}

export const name = 'kopt-harness'

export const inject = ['tools', 'subprocess', 'systemPrompt']

export function apply(ctx, config = {}) {
  // 优先级：配置文件 > 环境变量 > 内置默认值。
  // 环境变量留着只是为了临时试错（换一个解释器），配置文件才是唯一记在
  // 纸面上的真相——重启之后环境变量就没了，配置文件还在。
  const pythonPath = config.pythonPath || process.env.KOPT_PYTHON || DEFAULTS.pythonPath
  const koptHome = config.koptHome || process.env.KOPT_HOME || DEFAULTS.koptHome
  const logRoot = config.logRoot || process.env.KOPT_LOG_ROOT || DEFAULTS.logRoot
  // 解释器路径里是 `/`（WSL 风格）还是 `\`（Windows 风格），决定怎么解析成真实路径。
  const wslStyle = pythonPath.includes('/')
  const launch = (args) => {
    if (!args.length) return args
    const exe = resolveExe(args[0], pythonPath, wslStyle)
    return exe === args[0] ? args : [exe, ...args.slice(1)]
  }
  if (!koptHome) {
    ctx.logger?.warn?.(
      'kopt-harness: koptHome is empty; set it in cordis.patch.yml to the directory that contains the kopt package.',
    )
  }
  if (!logRoot) {
    ctx.logger?.warn?.(
      'kopt-harness: logRoot is empty — the event log is DISABLED. Set logRoot to a Forge-Logs checkout.',
    )
  }
  // 一个会话 = 一个日志目录。id 只在这里生成一次，所以「哪些事件属于同一次
  // 运行」永远只有一个答案。
  const sessionId = config.sessionId || `s-${Date.now().toString(36)}`

  /** Project root the model works in: the session cwd by default. */
  function rootOf(exec) {
    const cwd = exec?.agent?.session?.workspace || process.cwd()
    return resolve(cwd)
  }

  /**
   * Run one kopt subcommand. Explicit argv, no shell, no interpolation:
   * a candidate filename containing `; rm -rf /` is just a filename.
   *
   * This is the only place the adapter starts a process, and it is the reason
   * the verdict is not the model's opinion: every millisecond the harness
   * believes came out of this child's clock, and the model cannot reach a
   * shell to produce a second one.
   */
  async function runKopt(args, exec) {
    const handle = ctx.subprocess.spawn({
      argv: launch([pythonPath, '-m', 'kopt.cli', ...args]),
      cwd: koptHome || rootOf(exec),
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: 1 << 20 },
        stderr: { maxBytes: 1 << 20 },
      },
      // The child inherits no ambient credentials, so the package path must be
      // stated explicitly. PYTHONPATH is relative to cwd above unless the
      // configured home is already absolute.
      env: {
        ...ENV,
        PYTHONPATH: koptHome || rootOf(exec),
      },
      graceMs: 5000,
      signal: exec?.signal,
    })
    const outcome = await handle.done
    const stdout = stripAnsi(handle.collected.stdout?.readFrom(0)?.text ?? '')
    const stderr = stripAnsi(handle.collected.stderr?.readFrom(0)?.text ?? '')
    return { exitCode: outcome.exitCode ?? -1, signal: outcome.signal ?? null, stdout, stderr }
  }

  /** Render one CLI result the way the model should read it. */
  function render(r) {
    const body = [r.stdout.trim(), r.stderr.trim()].filter(Boolean).join('\n')
    const code = r.exitCode === 0 ? '' : `\n[exit: ${r.exitCode}]`
    return [{ type: 'text', text: `${body || '(no output)'}${code}` }]
  }

  /**
   * 往会话日志里追加一条事件。
   *
   * 这里编码了两条规矩：
   *   1. **日志只有程序能写。** 它由适配器在子进程退出后调用，字段是适配器
   *      算出来的。agent 写不了自己要拿来被检查的东西。
   *   2. **没有 logRoot 就吵，不静默写到别处。** 「以为记了其实没记」比
   *      「没记」更难查。
   *
   * 日志失败**不让工具调用失败**：模型的结论是产品，日志是影子。
   * 写日志出错只往会话 stderr 报一声，不把一个好判决变成一次报错。
   */
  async function logEvent(kind, payload, exec) {
    if (!logRoot) return
    const r = await runKopt(
      [
        'log-event', kind,
        '--payload', JSON.stringify(payload),
        '--log-root', logRoot,
        '--session', sessionId,
        '--agent', config.agentName || name,
      ],
      exec,
    )
    if (r.exitCode !== 0) {
      ctx.logger?.warn?.(
        `kopt-harness: log-event ${kind} failed (exit ${r.exitCode}): ${(r.stderr || r.stdout).trim()}`,
      )
    }
  }

  /**
   * 注册探针：把每一次 register 的名字写进一份证据文件。
   *
   * 存在的理由：「工具注册好了没有」不能靠界面看着像、也不能靠模型嘴上说，
   * 必须由程序在**真实运行的那一份代码**里留下痕迹。这个文件由 harness 写、
   * 人和 agent 都改不了，是"程序掌舵"用在自校验上的那一面。
   *
   * 默认落在 <logRoot>/_register-evidence.log。logRoot 为空时（用户就是想
   * 关日志）退回临时目录，绝不写死某个人的家目录。
   */
  function traceRegistration(toolName) {
    try {
      const target = logRoot
        ? resolve(logRoot, '_register-evidence.log')
        : resolve(tmpdir(), 'kopt-register-evidence.log')
      appendFileSync(
        target,
        `${new Date().toISOString()} pid=${process.pid} cwd=${process.cwd()} register=${toolName}\n`,
      )
    } catch {
      /* 证据写不进去不能拖垮注册本身 */
    }
  }

  function registerTool(definition) {
    traceRegistration(definition.name)
    try {
      const handle = ctx.tools.register(defineTool(definition))
      // register 返回的是注销函数本身。如果它变成 undefined，说明"注册
      // 成功"只是我以为的——所以把类型也记下来，出问题时有据可查。
      if (typeof handle !== 'function') {
        traceRegistration(`WARN ${definition.name} register returned ${typeof handle}`)
      }
      return handle
    } catch (err) {
      traceRegistration(`ERROR ${definition.name} ${err?.message ?? err}`)
      ctx.logger?.warn?.(`kopt-harness: register ${definition.name} failed: ${err?.message ?? err}`)
      return undefined
    }
  }

  /** 包一层：任何工具调用都会留下一条 calls.jsonl，包括失败的那次。 */
  function logged(toolName, body) {
    return async (args, exec) => {
      const started = Date.now()
      let exitCode = 0
      try {
        const out = await body(args, exec)
        const text = out?.[0]?.text ?? ''
        const codes = [...text.matchAll(/\[exit: (\d+)\]/g)]
        exitCode = codes.length ? Number(codes[codes.length - 1][1]) : 0
        return out
      } catch (err) {
        exitCode = -1
        throw err
      } finally {
        await logEvent(
          'tool_call',
          { tool: toolName, args: args ?? {}, exit: exitCode, ms: Date.now() - started },
          exec,
        )
      }
    }
  }

  const textOutput = {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: tail(value) }],
  }

  const EXIT_MEANING = `
Exit codes are part of the protocol, not noise:
0 = ok (and for adjudicate: ACCEPT), 1 = ok but the conclusion is negative,
2 = usage/input error, 3 = unsupported, 4 = gate not approved,
5 = mutation sentinel failed, 6 = protected path, 7 = identity mismatch,
8 = cannot read the artifact (inspect).`

  // ------------------------------------------------------- read-only inspect
  //
  // 设计 §9c：编译参数是**优化手段**（agent 可以选），产物汇编码是**验证材料**
  // （agent 只能看）。所以这里给的是眼睛，不是手：
  //   - 不编译、不运行、不 perf，只对一个已存在的目标文件做反汇编；
  //   - 参数由 CLI 拼死，agent 传不进来任何东西；
  //   - 调不调用由 agent 自己决定 —— 它不是流程的一步，是它判断时的证据来源。
  // 「看一眼」不会削弱判决的可信度：读不出加速比。
  registerTool({
      name: 'kopt_inspect',
      description:
        'Read-only. Disassemble a compiled artifact (.so/.o/.a/.elf) so you can check what the compiler actually emitted — vectorisation, unrolling, spills, whether the march flag took effect. This never compiles, never runs, never times anything. Call it when you need evidence about codegen, not as a workflow step.',
      parameters: {
        target: {
          type: 'string',
          required: true,
          description: 'Project-relative path to a compiled artifact.',
        },
        maxLines: {
          type: 'number',
          description: 'Cap on disassembly lines (default 400). Narrow the target rather than raising this.',
        },
      },
      output: textOutput,
      execute: logged('kopt_inspect', async (args, exec) => {
        const argv = ['--root', rootOf(exec), 'inspect', args.target]
        if (args.maxLines) argv.push('--max-lines', String(args.maxLines))
        const r = await runKopt(argv, exec)
        return render(r)
      }),
  })

  // ---------------------------------------------------------------- S0/S1
  registerTool({
      name: 'kopt_probe',
      description:
        'Stage S0/S1. Probe hardware and software, write a report to docs/recon/, and leave the human gate pending. Run this first, then STOP and let the human read the report.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_probe', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'probe'], exec)
        return render(r)
      }),
  })

  registerTool({
      name: 'kopt_probe_status',
      description: 'Show the human gate state and the frozen environment digest.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_probe_status', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'probe-status'], exec)
        return render(r)
      }),
  })

  // --------------------------------------------------------- S3 calibration
  registerTool({
      name: 'kopt_calibrate',
      description:
        'Stage S3. A/A calibration: run the reference against itself in interleaved pairs to estimate the noise floor (sigma). Every performance verdict depends on this; run it after the gate is approved and again whenever the environment changes.',
      parameters: {
        case: {
          type: 'string',
          description: 'Case label, e.g. "512x512:fp32". Defaults to the first case in spec.toml.',
        },
        pairs: { type: 'number', description: 'Pairs to run. 5 is the floor; more only if asked.' },
      },
      output: textOutput,
      execute: logged('kopt_calibrate', async (args, exec) => {
        const argv = ['--root', rootOf(exec), 'calibrate']
        if (args.case) argv.push('--case', args.case)
        if (args.pairs) argv.push('--pairs', String(args.pairs))
        const r = await runKopt(argv, exec)
        return render(r)
      }),
  })

  // ------------------------------------------------------ S8 verify + S9 bench
  //
  // One tool, on purpose. See the module comment.
  registerTool({
      name: 'kopt_verify',
      description:
        'Stage S8 (and, only if S8 passes, S9). Verify a candidate against the reference with the mutation sentinel, then time it with interleaved ABBA pairs. Returns PASS/FAIL and the measured speedup. Pass candidate "__baseline__" to verify the reference against itself.',
      parameters: {
        candidate: {
          type: 'string',
          required: true,
          description:
            'Candidate filename inside work/candidate/, e.g. "good.py". Pass "__baseline__" for the reference.',
        },
        case: {
          type: 'string',
          description: 'Restrict to one case label. Omit to verify every case in spec.toml.',
        },
      },
      output: textOutput,
      execute: logged('kopt_verify', async (args, exec) => {
        const root = rootOf(exec)
        const baseline = args.candidate === '__baseline__'
        const vArgs = ['--root', root, 'verify', baseline ? '--baseline' : args.candidate]
        if (args.case) vArgs.push('--case', args.case)
        const v = await runKopt(vArgs, exec)
        // Exit 1 here is a *negative conclusion* (FAIL / INCONCLUSIVE), not a
        // tool failure: report it, and do not spend pairs timing bad code.
        if (v.exitCode !== 0) {
          return [
            {
              type: 'text',
              text: [
                'S8 did not pass — S9 is skipped by design (S8 gates S9).',
                `verify exit: ${v.exitCode}`,
                v.stdout.trim(),
                v.stderr.trim(),
              ]
                .filter(Boolean)
                .join('\n'),
            },
          ]
        }
        const bArgs = ['--root', root, 'bench']
        if (!baseline) bArgs.push(args.candidate)
        if (args.case) bArgs.push('--case', args.case)
        const b = await runKopt(bArgs, exec)
        const text = [v.stdout.trim(), '', b.stdout.trim(), b.stderr.trim()].filter(Boolean).join('\n')
        return [{ type: 'text', text: tail(text) }]
      }),
  })

  // ---------------------------------------------------------- S10 adjudicate
  registerTool({
      name: 'kopt_adjudicate',
      description:
        'Stage S10. Turn the most recent bench into a verdict: ACCEPT / REJECT / INDISTINGUISHABLE / SUSPECT / INVALID. The verdict is computed from the receipts, not chosen. Do not argue with it.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_adjudicate', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'adjudicate'], exec)
        return render(r)
      }),
  })

  // ------------------------------------------------------------- S11 prose
  registerTool({
      name: 'kopt_narrate',
      description:
        'Stage S11a. Emit the machine-written half of the write-up: docs/opt/<run_id>/front-matter.json plus the exact list of numbers the prose may contain. Call before writing prose; never hand-write these fields.',
      parameters: {
        run: { type: 'string', description: 'Run id, e.g. "r0002". Defaults to the latest adjudication.' },
        stage: { type: 'string', description: 'Optimization stage, e.g. "memory_access".' },
      },
      output: textOutput,
      execute: logged('kopt_narrate', async (args, exec) => {
        const argv = ['--root', rootOf(exec), 'narrate']
        if (args.run) argv.push('--run', args.run)
        if (args.stage) argv.push('--stage', args.stage)
        const r = await runKopt(argv, exec)
        return render(r)
      }),
  })

  registerTool({
      name: 'kopt_doc_gate',
      description:
        'Stage S11b. Validate the prose written for a run: structure, frozen front-matter, and that every bare number traces to a receipt. Write to docs/opt/ for ACCEPT and docs/opt/rejected/ for the others.',
      parameters: {
        run: { type: 'string', description: 'Run id. Defaults to the latest adjudication.' },
        file: { type: 'string', description: 'Check only this file (project-relative).' },
      },
      output: textOutput,
      execute: logged('kopt_doc_gate', async (args, exec) => {
        const argv = ['--root', rootOf(exec), 'doc-gate']
        if (args.run) argv.push('--run', args.run)
        if (args.file) argv.push('--file', args.file)
        const r = await runKopt(argv, exec)
        return render(r)
      }),
  })

  // --------------------------------------------------------------- budget
  registerTool({
      name: 'kopt_pending',
      description:
        'List verdicts whose prose is still owed. Writing them is a closure condition, not a courtesy — `kopt_propose` refuses while any remain.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_pending', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'pending'], exec)
        return render(r)
      }),
  })

  registerTool({
      name: 'kopt_propose',
      description:
        'Stage S6. Ask to start the next optimization round. Refused with exit 2 while previous results are not archived. If allowed, you may then edit work/candidate/ only.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_propose', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'propose'], exec)
        return render(r)
      }),
  })

  // ----------------------------------------------------------- write policy
  registerTool({
      name: 'kopt_guard',
      description:
        'Check whether paths are writable by you. Protected: spec.toml, reference/, receipts/, harness/. Writable: work/candidate/, scratch/. Append-only: docs/, journal/, knowledge/. Refuses with exit 6 if anything is protected.',
      parameters: {
        paths: {
          type: 'array',
          required: true,
          description: 'Project-relative paths.',
          items: { type: 'string' },
        },
      },
      output: textOutput,
      execute: logged('kopt_guard', async (args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'guard', ...args.paths], exec)
        return render(r)
      }),
  })

  // ------------------------------------------------------- system prompt
  //
  // The harness's rules belong in the prompt too, but as *pointers to the
  // program* — never as a second copy of the rules that could drift.
  //
  // 教训（2026-10 实测，四条腿的消融实验）：
  //   1. 这一段原本写的是 `content: [...]`。宿主 `systemPrompt.section()`
  //      只认 `{ name, order, text }`，`content` 会被忽略，`text` 于是是
  //      undefined。
  //   2. 紧接着 `renderPrompt` 对每段做 `text.indexOf("{{")`，于是整轮会话
  //      死在 `Cannot read properties of undefined (reading 'indexOf')`——
  //      而且发生在 step/start 之后、request/header 之前，所以模型永远收
  //      不到工具表，看起来就像"插件没注册上"。
  //   3. 脚本里那段 `content: [...].join('\n')` 并没有被替换掉，所以即使
  //      把 content 换成 text，只要不显式写 `interpolate: false`，宿主还会
  //      在文本里找 `{{变量}}`，踩到同一个坑。
  // 现在：文本是纯字符串、显式 interpolate:false（内容里没有任何变量）、
  // 并且加了类型检查——写错就自己报出来，绝不再让宿主悄悄崩在 indexOf。
  if (ctx.systemPrompt?.section) {
    const promptText = [
      'This workspace uses the kopt optimization harness. The program steers;',
      'you sail. You decide what to try. You never decide what counts as correct',
      'or as faster.',
      '',
      'Workflow: kopt_probe -> (human approves) -> kopt_calibrate -> write a',
      'candidate in work/candidate/ -> kopt_verify -> kopt_adjudicate ->',
      'kopt_narrate -> write the prose -> kopt_doc_gate -> kopt_propose.',
      '',
      'Rules you cannot bend:',
      '- Never edit spec.toml, reference/, receipts/, or the harness. If a test',
      '  seems wrong, say so; do not weaken it.',
      '- Do not run your own timing. kopt_verify is the only timing source; a',
      '  speedup you measured yourself is not evidence.',
      '- Every verdict needs prose, including REJECT and INDISTINGUISHABLE.',
      '  "We tried this and could not tell" is knowledge.',
      '- Numbers in prose must come from receipts. Quote anything else with',
      '  backticks and say where it came from.',
      EXIT_MEANING,
    ].join('\n')
    if (typeof promptText !== 'string' || promptText.length === 0) {
      traceRegistration('ERROR promptSection text is not a non-empty string')
    } else {
      ctx.systemPrompt.section({
        name: 'kopt-harness',
        // order 是必填的有限数：位置由它决定，缺了 DSH 直接拒绝注册。
        order: 50,
        // 字段名必须是 text。content 是空想出来的名字，宿主不认。
        text: promptText,
        // 这段是死文本，没有 {{变量}}。显式关掉插值，避免宿主再去解析。
        interpolate: false,
      })
      traceRegistration(`PROMPT ok ${promptText.length}B`)
    }
  }

  // 启动时把真正生效的配置记一行。下次"为什么工具跑不起来"，第一眼看这里，
  // 而不是去猜环境变量有没有传进来。
  traceRegistration(
    `CONFIG pythonPath=${pythonPath} koptHome=${koptHome || '(空)'} logRoot=${logRoot || '(空)'} sessionId=${sessionId}`,
  )
}
