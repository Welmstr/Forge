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
import { resolve } from 'node:path'

/** Python interpreter and CLI package root; overridable through Config. */
const DEFAULTS = {
  pythonPath: 'python',
  koptHome: '',
  // Where the event log is checked out. This is the Forge-Logs repo, NOT the
  // project: 知识库装结论，日志装过程，两个库不混。空 = 日志关闭（并且会吵）。
  logRoot: '',
}

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

export const inject = ['tools', 'subprocess']

export function apply(ctx, config = {}) {
  const pythonPath = config.pythonPath || DEFAULTS.pythonPath
  const koptHome = config.koptHome || DEFAULTS.koptHome
  const logRoot = config.logRoot || DEFAULTS.logRoot
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
      argv: [pythonPath, '-m', 'kopt.cli', ...args],
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
  ctx.tools.register(
    defineTool({
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
    }),
  )

  // ---------------------------------------------------------------- S0/S1
  ctx.tools.register(
    defineTool({
      name: 'kopt_probe',
      description:
        'Stage S0/S1. Probe hardware and software, write a report to docs/recon/, and leave the human gate pending. Run this first, then STOP and let the human read the report.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_probe', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'probe'], exec)
        return render(r)
      }),
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'kopt_probe_status',
      description: 'Show the human gate state and the frozen environment digest.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_probe_status', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'probe-status'], exec)
        return render(r)
      }),
    }),
  )

  // --------------------------------------------------------- S3 calibration
  ctx.tools.register(
    defineTool({
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
    }),
  )

  // ------------------------------------------------------ S8 verify + S9 bench
  //
  // One tool, on purpose. See the module comment.
  ctx.tools.register(
    defineTool({
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
    }),
  )

  // ---------------------------------------------------------- S10 adjudicate
  ctx.tools.register(
    defineTool({
      name: 'kopt_adjudicate',
      description:
        'Stage S10. Turn the most recent bench into a verdict: ACCEPT / REJECT / INDISTINGUISHABLE / SUSPECT / INVALID. The verdict is computed from the receipts, not chosen. Do not argue with it.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_adjudicate', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'adjudicate'], exec)
        return render(r)
      }),
    }),
  )

  // ------------------------------------------------------------- S11 prose
  ctx.tools.register(
    defineTool({
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
    }),
  )

  ctx.tools.register(
    defineTool({
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
    }),
  )

  // --------------------------------------------------------------- budget
  ctx.tools.register(
    defineTool({
      name: 'kopt_pending',
      description:
        'List verdicts whose prose is still owed. Writing them is a closure condition, not a courtesy — `kopt_propose` refuses while any remain.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_pending', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'pending'], exec)
        return render(r)
      }),
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'kopt_propose',
      description:
        'Stage S6. Ask to start the next optimization round. Refused with exit 2 while previous results are not archived. If allowed, you may then edit work/candidate/ only.',
      parameters: {},
      output: textOutput,
      execute: logged('kopt_propose', async (_args, exec) => {
        const r = await runKopt(['--root', rootOf(exec), 'propose'], exec)
        return render(r)
      }),
    }),
  )

  // ----------------------------------------------------------- write policy
  ctx.tools.register(
    defineTool({
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
    }),
  )

  // ------------------------------------------------------- system prompt
  //
  // The harness's rules belong in the prompt too, but as *pointers to the
  // program* — never as a second copy of the rules that could drift.
  if (ctx.systemPrompt?.section) {
    ctx.systemPrompt.section({
      name: 'kopt-harness',
      content: [
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
      ].join('\n'),
    })
  }
}
