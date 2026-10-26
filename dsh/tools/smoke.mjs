/**
 * Exercise the adapter without the Harness: a fake `ctx` that records tool
 * registrations and a fake `ctx.subprocess.spawn` that really runs the CLI.
 *
 * What this proves:
 *   - every tool registers with a well-formed schema;
 *   - argv reaches the child as a real array (no shell, no interpolation);
 *   - exit codes come back unmangled;
 *   - `kopt_verify` skips timing when S8 fails — the S8-gates-S9 rule enforced
 *     in the adapter, not just in the Python side.
 *
 * What it does NOT prove: that the Harness loads the bundle, that the model
 * sees the schemas, or that the UI renders. Those need installation.
 *
 * Usage:
 *   node tools/smoke.mjs <repo-root-containing-kopt> <project-root>
 */
import { spawn } from 'node:child_process'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const [koptHome, projectRoot] = process.argv.slice(2)
if (!koptHome || !projectRoot) {
  console.error('usage: node tools/smoke.mjs <koptHome> <projectRoot>')
  process.exit(2)
}

// The bundle imports '@deepseek-ai/dsh-tools', which only resolves inside the
// Harness. Shim it for the smoke test: defineTool is identity.
const shim = `
  export const defineTool = (def) => def
`
const { writeFileSync, mkdtempSync } = await import('node:fs')
const { tmpdir } = await import('node:os')
const { join } = await import('node:path')
const shimDir = mkdtempSync(join(tmpdir(), 'kopt-shim-'))
writeFileSync(join(shimDir, 'dsh-tools.mjs'), shim)

const tools = new Map()
const prompts = []

function fakeSpawn(request) {
  const [exe, ...rest] = request.argv
  // The plugin resolves the interpreter by name ("python"); the smoke test
  // pins an absolute one because this box has no `python` on PATH.
  const real = process.env.KOPT_PYTHON && exe === 'python' ? process.env.KOPT_PYTHON : exe
  const child = spawn(real, rest, {
    cwd: request.cwd,
    env: { ...process.env, ...(request.env ?? {}) },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', (d) => (stdout += d))
  child.stderr.on('data', (d) => (stderr += d))
  const done = new Promise((res) =>
    child.on('close', (code) => res({ exitCode: code, signal: null })),
  )
  return {
    done,
    collected: {
      stdout: { readFrom: () => ({ text: stdout }) },
      stderr: { readFrom: () => ({ text: stderr }) },
    },
  }
}

const fakeCtx = {
  logger: { warn: (m) => console.error('[warn]', m) },
  tools: { register: (def) => tools.set(def.name, def) },
  subprocess: { spawn: fakeSpawn },
  systemPrompt: { section: (s) => prompts.push(s) },
}

// Rewrite the bare import so the bundle loads outside the Harness.
const { readFileSync } = await import('node:fs')
const source = readFileSync(new URL('../index.js', import.meta.url), 'utf8').replace(
  "'@deepseek-ai/dsh-tools'",
  `'${pathToFileURL(join(shimDir, 'dsh-tools.mjs')).href}'`,
)
const modFile = join(shimDir, 'bundle.mjs')
writeFileSync(modFile, source)
const mod = await import(pathToFileURL(modFile).href)

mod.apply(fakeCtx, {
  koptHome,
  pythonPath: process.env.KOPT_PYTHON || 'python',
  logRoot: process.env.FORGE_LOG_ROOT || '',
  sessionId: 'smoke-test',
  agentName: 'smoke',
})

const exec = { agent: { session: { workspace: resolve(projectRoot) } } }
let failures = 0

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures++
}

console.log(`\nregistered tools: ${[...tools.keys()].join(', ')}\n`)

check('no bench tool exposed', !tools.has('bench') && !tools.has('kopt_bench'))
check('bench is folded into kopt_verify', tools.has('kopt_verify'))
check('system prompt section registered', prompts.length === 1)

for (const [name, def] of tools) {
  const okName = /^kopt_[a-z_]+$/.test(name)
  const okDesc = typeof def.description === 'string' && def.description.length > 20
  const okParams = typeof def.parameters === 'object'
  const okExec = typeof def.execute === 'function'
  check(
    `schema ${name}`,
    okName && okDesc && okParams && okExec,
    okName && okDesc && okParams && okExec ? '' : 'malformed',
  )
}

async function call(name, args) {
  const def = tools.get(name)
  const content = await def.execute(args ?? {}, exec)
  const value = content[0]?.text ?? ''
  const m = value.match(/\[exit: (\d+)\]/)
  // 兼容「只在行首写一次 [exit: N]」和「每行一条命令各带一次」两种渲染：
  // 末尾那个才是这条命令自己的退出码。
  const all = [...value.matchAll(/\[exit: (\d+)\]/g)]
  const exit = m ? Number(all[all.length - 1][1]) : 0
  return { text: value.replace(/\[exit: \d+\]/g, '').trim(), raw: value, exit }
}

const status = await call('kopt_probe_status')
check('probe-status runs', status.text.includes('gate='), status.text.split('\n')[0])

// 断言要跟**退什么**走，不要跟「恰好还没做过」走。
// probe 在闸门已批准时会以 4 退出并拒绝重写探查报告（设计 §3.4：换环境要重走闸门，
// 不能悄悄覆盖人工核对过的那份），那不是故障，是规矩在生效 —— 一样是 PASS。
const gateApproved = /gate=approved/.test(status.text)
const probe = await call('kopt_probe')
const probeOk = gateApproved
  ? probe.exit === 4 && /already frozen/.test(probe.raw)
  : probe.exit === 0 && /docs[\\/]recon/.test(probe.raw)
check(
  'probe writes a report (or refuses to when the env is already frozen)',
  probeOk,
  `exit=${probe.exit} ${probe.text.split('\n')[0]}`,
)

const guardProtected = await call('kopt_guard', { paths: ['spec.toml'] })
check('guard refuses spec.toml with exit 6', guardProtected.exit === 6, guardProtected.text.trim())

const guardOk = await call('kopt_guard', { paths: ['work/candidate/x.py'] })
check('guard allows work/candidate', guardOk.exit === 0, guardOk.text.trim())

const fail = await call('kopt_verify', { candidate: 'wrong_math.py' })
check(
  'S8 failure is reported as a refusal, not a timing',
  fail.text.includes('S9 is skipped') && !fail.text.toLowerCase().includes('speedup'),
  fail.text.split('\n')[0],
)

// pending 的两种正确形状：0 = 没有欠账；1 = 有欠账，而且它把欠了哪一轮说出来了。
// 后者是设计 §7.5 在生效，不能算失败 —— 判据是「它有没有在装哑巴」。
const pending = await call('kopt_pending')
const pendingOk =
  pending.exit === 0 ||
  (pending.exit === 1 && /docs\/opt/.test(pending.raw) && /run r\d+/.test(pending.raw))
check('pending runs', pendingOk, `exit=${pending.exit} ${pending.text.split('\n')[0]}`)

const argvCheck = await call('kopt_guard', { paths: ['work/candidate/a;b.py'] })
check(
  'argv is not shell-interpreted',
  argvCheck.exit === 0 && argvCheck.text.includes('a;b.py'),
  argvCheck.text.trim(),
)

// ---- 只读审阅：设计 §9c ----
// 判据是「拿不到就明说」，不是「拿不到就返回空」。
const inspectSrc = await call('kopt_inspect', { target: 'reference/reference.py' })
check(
  'inspect refuses source files with exit 8 (never an empty dump)',
  inspectSrc.exit === 8 && /not a compiled artifact/.test(inspectSrc.raw),
  inspectSrc.text.split('\n')[0],
)

const inspectMissing = await call('kopt_inspect', { target: 'work/candidate/nope.so' })
check(
  'inspect on a missing artifact is exit 8, not silence',
  inspectMissing.exit === 8,
  inspectMissing.text.split('\n')[0],
)

// ---- 日志：程序写，agent 改不了 ----
if (process.env.FORGE_LOG_ROOT) {
  const { readdirSync, readFileSync, existsSync } = await import('node:fs')
  const globSession = (root) => {
    const hits = []
    const walk = (d) => {
      for (const e of readdirSync(d, { withFileTypes: true })) {
        const p = join(d, e.name)
        if (e.isDirectory()) walk(p)
        else if (e.name === 'calls.jsonl') hits.push(d)
      }
    }
    walk(root)
    return hits
  }
  const dirs = globSession(process.env.FORGE_LOG_ROOT)
  const sessionDir = dirs.find((d) => d.includes('smoke-test'))
  const present = Boolean(sessionDir) && existsSync(join(sessionDir, 'manifest.json'))
  check('event log directory written for the session', present, sessionDir ?? '(none)')
  if (present) {
    const lines = readFileSync(join(sessionDir, 'calls.jsonl'), 'utf8')
      .trim()
      .split('\n')
      .filter(Boolean)
    const rows = lines.map((l) => JSON.parse(l))
    const tools = rows.map((r) => r.tool)
    // 每一次工具调用都必须留下一条，包括那次 exit 8 的 inspect。
    check(
      'every tool call left a calls.jsonl row',
      tools.length >= 6 && tools.includes('kopt_inspect') && tools.includes('kopt_probe_status'),
      `${tools.length} rows: ${[...new Set(tools)].join(',')}`,
    )
    const manifest = JSON.parse(readFileSync(join(sessionDir, 'manifest.json'), 'utf8'))
    check(
      'manifest records agent + host (who and where)',
      manifest.agent === 'smoke' && typeof manifest.host === 'string' && manifest.host.length > 0,
      `${manifest.agent} @ ${manifest.host}`,
    )
  }
} else {
  console.log('SKIP  log assertions — set FORGE_LOG_ROOT to a scratch dir to enable them')
}

console.log(`\n${failures === 0 ? 'all checks passed' : `${failures} check(s) failed`}`)
process.exit(failures === 0 ? 0 : 1)
