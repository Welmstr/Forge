# kopt

A harness for numeric-kernel optimization where **the program steers and
evidence decides**. The agent may propose and rewrite the kernel; it may never
decide what counts as correct or as faster.

Design document: `../kopt-harness-design.md` (Chinese, v0.2).

## The one-line contract

> 程序掌舵（流程、预算、验收、计分、权限），LLM 掌帆（诊断、提案、改写、反思），
> 记忆只从被验证过的证据里长出来。

Everything below exists to make that sentence mechanically true, not
aspirational.

## Why it is shaped this way

| Law | Where it lives | What it stops |
|---|---|---|
| Acceptance is programmatic | `adjudicate.py`, `verify.py` | The agent declaring victory |
| Identity is a content hash | `identity.py`, `store.py` | "Same code" claims that aren't |
| Missing is explicit | `verdicts.py`, `spec.py` | Silent defaults, zero-filled gaps |
| The agent edits the kernel, not the measurement | `project.py` (protected paths) | Weakening the test to pass it |
| Budgets only tighten, and are charged before the attempt | `bench.py` (`HARD_MIN_PAIRS`) | "Just one more run" |
| Experience only grows from adjudicated evidence | `flow.py`, `docgate.py` | Learnings invented after the fact |

## Stage machine

```
S0 probe ─ S1 report ─ ⟦gate 1: human freezes env⟧ ─ S2 env.lock
  ─ S3 calibrate (A/A) ─ S4 analyze ─ S5 plan ─ S6 propose ─ S7 build
  ─ S8 verify (hard gate) ─ S9 bench (ABBA, sole timing source)
  ─ S10 adjudicate ─ S11 narrate ─ S12 journal ─ ⟦gate 2: promotion⟧
```

S8 gates S9: an incorrect kernel is never timed. `adjudicate` re-checks this
and returns `INVALID` if the verification receipt for *that exact candidate*
did not pass.

## Verdicts are a closed set

| Verdict | Meaning | Prose goes to |
|---|---|---|
| `ACCEPT` | CI lower bound clears the accept threshold | `docs/opt/` |
| `REJECT` | CI upper bound ≤ 1.0 | `docs/opt/rejected/` |
| `INDISTINGUISHABLE` | CI spans the decision line | `docs/opt/rejected/` |
| `SUSPECT` | Faster than `suspect_speedup` — likely under-compute | (needs re-verify first) |
| `INVALID` | Precondition broken (no CI, verify failed, identity mismatch) | (none) |

There is no "roughly passed". A verdict that is not `ACCEPT` still gets prose —
"we tried this and could not tell" is knowledge, and losing it is how the same
dead end gets explored twice.

## Quick start

### WSL / Linux（推荐，这也是当前主用环境）

```bash
source /root/kopt-workspace/env.sh      # 定好 KOPT_PYTHON，别在别处再指一次
kopt --root $KOPT_WS/kopt/examples/demo probe
kopt --root $KOPT_WS/kopt/examples/demo gate-approve --by user   # 人的动作
kopt --root $KOPT_WS/kopt/examples/demo calibrate --case 512x512:fp32
kopt --root $KOPT_WS/kopt/examples/demo verify acceptable.py
kopt --root $KOPT_WS/kopt/examples/demo bench  acceptable.py
kopt --root $KOPT_WS/kopt/examples/demo adjudicate
```

整套跑：`bash examples/run_demo.sh`（输出逐段对齐 PowerShell 版）。

### Windows / PowerShell

```powershell
$env:PYTHONPATH = '<repo>/kopt'
$py = '<python>'

& $py -m kopt.cli --root examples\demo probe
& $py -m kopt.cli --root examples\demo gate-approve --by user   # 人的动作
& $py -m kopt.cli --root examples\demo calibrate --case 512x512:fp32
& $py -m kopt.cli --root examples\demo verify acceptable.py
& $py -m kopt.cli --root examples\demo bench  acceptable.py
& $py -m kopt.cli --root examples\demo adjudicate
```

`gate-approve` / `gate-amend` / `gate-reject` 是**只给人用**的入口：它们不在任何
宿主插件的工具表里。这不是命名偏好，是设计 §2 的闸门 1 靠它成立
（见 `../kopt-dsh/README.md` 第二节）。

Or run the whole thing, including all six acceptance cases, with
`examples\run_demo.ps1` / `examples/run_demo.sh`. See `examples/README.md`.

## Exit codes

The host uses these to decide whether *the command* succeeded; the agent uses
them to read *the conclusion*. Keeping those two things apart is the point.

| Code | Meaning |
|---|---|
| 0 | Command succeeded (and, for `adjudicate`, the verdict is `ACCEPT`) |
| 1 | Command succeeded, but the conclusion is negative |
| 2 | Usage / input error |
| 3 | Unsupported (e.g. dtype has no declared tolerance) |
| 4 | Gate not approved |
| 5 | Mutation sentinel failed |
| 6 | Protected path |
| 7 | Identity mismatch |

## Layout

```
kopt/
  errors.py      exit-coded exceptions
  verdicts.py    the closed vocabulary
  identity.py    canonical JSON + content hashing
  store.py       receipts (write-once) and the append-only journal
  spec.py        spec.toml, the only source of thresholds
  project.py     write-permission classification
  opload.py      loading reference vs candidate; candidate may not build inputs
  metrics.py     deviation metrics + the mutation perturbation
  verify.py      S8, with the per-case mutation sentinel
  timing.py      ABBA sequence construction and the only place time is read
  stats.py       paired bootstrap in log space
  bench.py       S9, pair-count resolution
  calibrate.py   S3, A/A noise floor
  adjudicate.py  S10, the verdict function
  probe.py       S0/S1, facts with confidence and provenance
  gate.py        env.lock freeze, human amendments
  flow.py        S6 entry gate: no new round while prose is owed
  docgate.py     S11, numbers in prose must trace to receipts
  cli.py         the only entry point; the host calls this
```

## Things that are deliberately not here

- No `input()`: gates are driven by subcommands, so the host can call them
  through a shell-free `spawn(argv)`.
- No network: probing runs local commands only, with a timeout.
- No threshold in code: every number that decides pass/fail comes from
  `spec.toml`, and an undeclared dtype is `UNSUPPORTED`, never a default.
