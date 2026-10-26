"""CLI —— 唯一的真相来源，宿主只是调用它。

设计 §9 / §9b.2。核心做成 argv 友好的命令行：

  * 不读 stdin、不交互 —— 闸门由 `probe approve/amend/reject` 三个子命令驱动，
    而不是靠 `input()`。DSH 插件通过 `ctx.subprocess.spawn({argv: [...]})`
    调用，argv 不经 shell，模型无法注入。
  * 每个子命令都支持 `--json`，给宿主和后续散文校验用。
  * 退出码有语义：0 成功／1 结论为否／2 用法或输入错误／3 未覆盖／4 闸门未批准／
    5 哨兵失败／6 受保护路径／7 身份不符／8 拉不出产物。宿主据此判「命令是否成功」，
    模型据此判「结论是什么」—— 两件事分开。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .adjudicate import adjudicate
from .bench import resolve_pairs, run_bench
from .calibrate import run_calibration
from .errors import GateError, KoptError, ProtectedPathError, SpecError
from .flow import next_run_id, pending_obligations, require_prose_clear
from .gate import (Gate, amend as gate_amend, approve as gate_approve,
                   load_gate, reject as gate_reject, save_gate)
from .identity import digest_path, measurement_identity, short
from .opload import Candidate, Reference
from .probe import probe_all, render_report
from .project import Project
from .spec import load_spec
from .store import Ledger, Receipts, utc_now
from .verdicts import PerfVerdict, VerifyVerdict


# ---------------------------------------------------------------- 公共

def _project(args) -> Project:
    p = Project.at(args.root)
    p.ensure_dirs()
    return p


def _emit(args, text: str, payload: dict[str, Any] | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload if payload is not None else {"output": text},
                         ensure_ascii=False, indent=2))
    else:
        print(text)


def _calibration_for(project: Project, case_label: str) -> dict[str, Any] | None:
    receipts = Receipts(project.root)
    best: dict[str, Any] | None = None
    d = receipts.dir_for("calibrate")
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            row = json.loads(p.read_text(encoding="utf-8"))
            if row.get("case") != case_label:
                continue
            if best is None or row.get("recorded_at", "") > best.get("recorded_at", ""):
                best = row
    return best


def _verify_for_candidate(project: Project, candidate_path: str
                          ) -> dict[str, Any] | None:
    """取**这一份候选**最近的验证收据。

    身份按路径的下标比较 —— 文件内容改了而同名，旧收据就不该再被引用。
    设计 §6 / 07 定律 2：测量身份是内容哈希，不是「文件名」。
    """
    receipts = Receipts(project.root)
    want = str(Path(candidate_path).resolve())
    best: dict[str, Any] | None = None
    d = receipts.dir_for("verify")
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            row = json.loads(p.read_text(encoding="utf-8"))
            have = row.get("candidate_path")
            if not have:
                continue
            try:
                if str(Path(have).resolve()) != want:
                    continue
            except OSError:
                continue
            if best is None or row.get("recorded_at", "") > best.get("recorded_at", ""):
                best = row
    return best


# ---------------------------------------------------------------- probe

def cmd_probe(args) -> int:
    project = _project(args)
    gate = load_gate(project.gate_path)

    if gate.state.value == "approved" and not args.force:
        raise GateError(
            "environment is already frozen (gate=approved). Approving again "
            "would change the measurement identity of every prior result. "
            "Use --force only if you intend to invalidate them."
        )

    facts = probe_all()
    stamp = utc_now().replace(":", "").replace("-", "")
    report_path = project.docs_recon / f"{stamp}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    body = render_report(facts, project_name=project.root.name, generated_at=utc_now())
    report_path.write_text(body, encoding="utf-8")

    gate = Gate(state=gate.state.__class__.PENDING, facts=facts,
                report_path=str(report_path.relative_to(project.root)))
    gate.report_digest = digest_path(report_path)
    save_gate(project.gate_path, gate)

    Ledger(project.root).append("probe", {
        "report": gate.report_path,
        "facts": len(facts),
        "unknown": sum(1 for f in facts if f.confidence.value == "unknown"),
    })

    _emit(args, body, {"report": gate.report_path, "facts": gate.as_dict()["facts"]})
    if not getattr(args, "json", False):
        print(f"\n---\n报告已写入 {gate.report_path}，闸门状态 pending。")
        print("请过目后执行其一：")
        print("  kopt gate-approve")
        print("  kopt gate-amend <key> <value> --why \"...\"")
        print("  kopt gate-reject --why \"...\"")
    return 0


def cmd_probe_approve(args) -> int:
    return _gate_approve(args)


def _gate_approve(args, note: str | None = None) -> int:
    project = _project(args)
    gate = load_gate(project.gate_path)
    gate, env_lock = gate_approve(gate, by=args.by, note=note)
    save_gate(project.gate_path, gate)
    project.env_lock.write_text(
        json.dumps(env_lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    # 草稿不再需要
    if project.env_lock_draft.exists():
        project.env_lock_draft.unlink()

    Ledger(project.root).append("gate_approve", {
        "by": args.by, "env_lock_digest": gate.env_lock_digest,
        "note": note,
    })
    _emit(args, f"gate=approved  env_lock_digest={short(gate.env_lock_digest)}",
          {"state": "approved", "env_lock_digest": gate.env_lock_digest,
           "env_lock": env_lock})
    return 0


def cmd_probe_amend(args) -> int:
    return _gate_amend(args)


def _gate_amend(args) -> int:
    project = _project(args)
    gate = load_gate(project.gate_path)
    value: Any = args.value
    # 尝试把字面量解析成数字/布尔/JSON，失败就当字符串
    try:
        value = json.loads(args.value)
    except (json.JSONDecodeError, TypeError):
        pass
    gate = gate_amend(gate, args.key, value, why=args.why, by=args.by)
    save_gate(project.gate_path, gate)
    Ledger(project.root).append("gate_amend", {
        "key": args.key, "value": value, "why": args.why, "by": args.by,
    })
    _emit(args, f"amended {args.key} = {value!r} (source: human, why: {args.why})",
          {"state": "pending", "key": args.key, "value": value})
    return 0


def cmd_probe_reject(args) -> int:
    return _gate_reject(args)


def _gate_reject(args) -> int:
    project = _project(args)
    gate = load_gate(project.gate_path)
    gate = gate_reject(gate, why=args.why, by=args.by)
    save_gate(project.gate_path, gate)
    Ledger(project.root).append("gate_reject", {"why": args.why, "by": args.by})
    _emit(args, f"gate=rejected ({args.why})", {"state": "rejected"})
    return 0


# ---- 人闸门的独立入口 -------------------------------------------------
#
# `probe-approve` 是给手里有终端的人用的；`gate-approve` 是给**接口**用的：
# agent 的工具表里没有它，只有 `probe`/`probe-status`。两条路写同一份
# gate.json，但只有人能走到后一条。设计 §2 的闸门 1 靠的就是这一点，
# 而不是靠提示词里写一句「请先征求同意」。


def cmd_gate_approve(args) -> int:
    return _gate_approve(args, note=args.note)


def cmd_gate_amend(args) -> int:
    return _gate_amend(args)


def cmd_gate_reject(args) -> int:
    return _gate_reject(args)


def cmd_probe_status(args) -> int:
    project = _project(args)
    gate = load_gate(project.gate_path)
    facts = gate.facts
    by_conf: dict[str, int] = {}
    for f in facts:
        by_conf[f.confidence.value] = by_conf.get(f.confidence.value, 0) + 1
    payload = {
        "state": gate.state.value,
        "report_path": gate.report_path,
        "env_lock_digest": gate.env_lock_digest,
        "decided_at": gate.decided_at,
        "decided_by": gate.decided_by,
        "facts": len(facts),
        "by_confidence": by_conf,
        "human_edits": sum(1 for f in facts if f.origin == "human"),
    }
    text = (f"gate={gate.state.value}  facts={len(facts)}  "
            f"measured={by_conf.get('measured', 0)} "
            f"unknown={by_conf.get('unknown', 0)}  "
            f"human={payload['human_edits']}")
    if gate.env_lock_digest:
        text += f"\n  env_lock={short(gate.env_lock_digest)}"
    _emit(args, text, payload)
    return 0


# ---------------------------------------------------------------- calibrate

def cmd_calibrate(args) -> int:
    project = _project(args)
    spec = load_spec(project.root)
    from .gate import require_approved

    env_digest = require_approved(load_gate(project.gate_path))

    reference = Reference.load(spec.reference, spec.candidate_entry)
    case = None
    if args.case:
        case = next((c for c in spec.cases if c.label == args.case), None)
        if case is None:
            raise SpecError(f"unknown case {args.case}")
    case = case or spec.cases[0]

    cal = run_calibration(spec, reference, case=case, pairs=args.pairs)
    payload = cal.as_dict()
    payload["spec_digest"] = spec.digest
    payload["env_lock_digest"] = env_digest
    receipts = Receipts(project.root)
    rid, _ = receipts.write("calibrate", payload)
    Ledger(project.root).append("calibrate", {
        "receipt": rid, "case": cal.case, "sigma": cal.sigma,
        "usable": cal.usable,
    })
    _emit(args, cal.render() + f"\n  receipt: {rid}", {"receipt": rid, **payload})
    return 0 if cal.usable else 1


# ---------------------------------------------------------------- verify

def _load_pair(project: Project, spec, cand_path: str):
    from .gate import require_approved

    env_digest = require_approved(load_gate(project.gate_path))
    reference = Reference.load(spec.reference, spec.candidate_entry)
    # 绝对路径直接用；相对路径先按「candidate 目录」解析，找不到再按项目根解析。
    # 只认 candidate 目录一种解释会让示例和基线自检都写不进去。
    cand_file = Path(cand_path)
    if not cand_file.is_absolute():
        in_work = project.candidate_dir / cand_path
        in_root = project.root / cand_path
        cand_file = in_work if in_work.exists() else in_root
    cand = Candidate.load(cand_file, spec.candidate_entry)
    return reference, cand, cand_file, env_digest


def cmd_verify(args) -> int:
    project = _project(args)
    spec = load_spec(project.root)

    if args.baseline:
        from .gate import require_approved
        from .verify import run_verification

        env_digest = require_approved(load_gate(project.gate_path))
        reference = Reference.load(spec.reference, spec.candidate_entry)
        cand_file = spec.reference
        rep = run_verification(spec, reference, run=reference.run,
                               case_filter=args.case)
    else:
        from .verify import run_verification

        reference, cand, cand_file, env_digest = _load_pair(project, spec, args.candidate)
        rep = run_verification(spec, reference, candidate=cand,
                               case_filter=args.case)

    payload = rep.as_dict()
    payload.update({
        "candidate_path": str(cand_file),
        "candidate_digest": digest_path(cand_file),
        "reference_digest": digest_path(spec.reference),
        "spec_digest": spec.digest,
        "env_lock_digest": env_digest,
    })
    rid, _ = Receipts(project.root).write("verify", payload)
    Ledger(project.root).append("verify", {
        "receipt": rid, "verdict": rep.verdict.value,
        "candidate": str(cand_file.name),
    })
    _emit(args, rep.render() + f"\n  receipt: {rid}", {"receipt": rid, **payload})
    return 0 if rep.verdict is VerifyVerdict.PASS else 1


# ---------------------------------------------------------------- bench

def cmd_bench(args) -> int:
    project = _project(args)
    spec = load_spec(project.root)
    reference, cand, cand_file, env_digest = _load_pair(project, spec, args.candidate)

    case = spec.cases[0]
    if args.case:
        case = next((c for c in spec.cases if c.label == args.case), None)
        if case is None:
            raise SpecError(f"unknown case {args.case}")

    cal = _calibration_for(project, case.label)
    n_pairs, reason = resolve_pairs(spec, cal, case)

    rep = run_bench(spec, reference, cand, case=case, pairs=n_pairs,
                    pairs_reason=reason,
                    sigma=(cal or {}).get("sigma"))

    cases = [case.as_dict()]
    timing = {
        "warmup": spec.bench.warmup,
        "pairs": n_pairs,
        "abba": True,
        "bootstrap_resamples": spec.bench.bootstrap_resamples,
        "bootstrap_seed": spec.bench.bootstrap_seed,
    }
    mid = measurement_identity(
        candidate_digest=digest_path(cand_file),
        reference_digest=digest_path(spec.reference),
        spec_digest=spec.digest,
        env_lock_digest=env_digest,
        cases=cases,
        timing=timing,
        harness_version=__version__,
    )

    payload = rep.as_dict()
    payload.update({
        "case": case.as_dict(),
        "candidate_path": str(cand_file),
        "measurement_id": mid,
        "timing": timing,
        "sigma_aa": (cal or {}).get("sigma"),
        "calibration_id": (cal or {}).get("id"),
    })
    rid, _ = Receipts(project.root).write("bench", payload)
    Ledger(project.root).append("bench", {
        "receipt": rid, "measurement_id": mid, "pairs": n_pairs,
    })
    _emit(args, rep.render() + f"\n  measurement_id: {mid}\n  receipt: {rid}",
          {"receipt": rid, **payload})
    return 0


# ---------------------------------------------------------------- adjudicate

def cmd_adjudicate(args) -> int:
    project = _project(args)
    spec = load_spec(project.root)
    receipts = Receipts(project.root)

    bench = None
    bench_receipt = None
    if args.bench:
        bench_receipt = receipts.read("bench", args.bench)
    else:
        bench_receipt = receipts.latest("bench")
    if bench_receipt is not None:
        from .bench import BenchReport
        from .stats import Interval
        from .timing import Sample

        iv = bench_receipt.get("interval")
        interval = Interval(
            point=iv["point"], low=iv["ci_low"], high=iv["ci_high"],
            confidence=iv["confidence"], resamples=iv["resamples"],
            seed=iv["seed"]) if iv else None
        samples = [Sample(s["label"], s["ms"], s["pair"])
                   for s in bench_receipt.get("samples", [])]
        bench = BenchReport(
            samples=samples,
            ratios=bench_receipt.get("ratios", []),
            interval=interval,
            pairs_used=int(bench_receipt.get("pairs_used", 0)),
            pairs_reason=bench_receipt.get("pairs_reason", ""),
            order=bench_receipt.get("order", []),
        )

    verify_receipt = None
    if args.verify:
        verify_receipt = receipts.read("verify", args.verify)
    else:
        # 只认**同一份候选**的验证收据。
        # 取「最后一张 verify 收据」会拿别的候选的结论来判这一份 ——
        # 那正是 07 定律 2「测量身份 = 内容哈希」要防的事。
        target = (bench_receipt or {}).get("candidate_path")
        if target:
            verify_receipt = _verify_for_candidate(project, str(target))

    verify_passed: bool | None = None
    if verify_receipt is not None:
        verify_passed = verify_receipt.get("verdict") == VerifyVerdict.PASS.value

    cal = None
    if bench_receipt is not None and bench_receipt.get("case"):
        cal = _calibration_for(project, _case_label(bench_receipt["case"]))

    adj = adjudicate(spec, bench, calibration=cal,
                     timing_consistent=not args.timing_inconsistent,
                     verify_passed=verify_passed)

    run_id = args.run or next_run_id(project.root, Ledger(project.root))
    payload = adj.as_dict()
    payload.update({
        "run_id": run_id,
        "bench_receipt": bench_receipt.get("id") if bench_receipt else None,
        "verify_receipt": verify_receipt.get("id") if verify_receipt else None,
        "measurement_id": bench_receipt.get("measurement_id") if bench_receipt else None,
        "pairs_used": bench.pairs_used if bench else None,
        "pairs_reason": bench.pairs_reason if bench else None,
        "spec_digest": spec.digest,
        # 判决的两项输入在收据里留值，不再只有 digest。
        # min_speedup 决定「门槛在哪」，sigma 回答「这次没分辨出来是改动没用
        # 还是精度不够」——两者都是判决的输入，不记下来，事后就无从复核。
        "min_speedup": spec.min_speedup,
        "sigma": (cal or {}).get("sigma") or (bench_receipt or {}).get("sigma"),
    })
    rid, _ = Receipts(project.root).write("adjudicate", payload, receipt_id=run_id)
    Ledger(project.root).append("adjudicate", {
        "run_id": run_id, "verdict": adj.verdict.value,
        "receipt": rid, "note": adj.reason,
    })
    _emit(args, f"{adj.render()}\n  run: {run_id}\n  receipt: {rid}",
          {"run_id": run_id, "receipt": rid, **payload})
    return 0 if adj.verdict is PerfVerdict.ACCEPT else 1


def _case_label(case: dict[str, Any]) -> str:
    shape = "x".join(str(x) for x in case["shape"])
    return f"{shape}:{case['dtype']}"


# ---------------------------------------------------------------- proposal gate

def cmd_propose(args) -> int:
    """S6 的入口闸：有欠账就不放行。设计 §7.5。"""
    project = _project(args)
    ledger = Ledger(project.root)
    require_prose_clear(project.root, ledger)
    run_id = next_run_id(project.root, ledger)
    Ledger(project.root).append("propose", {"run_id": run_id})
    _emit(args, f"propose allowed; next run_id={run_id}\n"
                f"  write the candidate under work/candidate/ only.",
          {"allowed": True, "run_id": run_id})
    return 0


def cmd_narrate(args) -> int:
    """把散文所需的全部机器事实打成一张「写散文用的底稿」。

    设计 §7.2：front-matter 由程序写、程序冻结，
    正文由模型写。这个子命令产出的就是那一半程序负责的部分，
    以及正文里**允许出现的数字**。模型不许自己算这些数，只许引用。
    """
    project = _project(args)
    spec = load_spec(project.root)
    receipts = Receipts(project.root)

    run_id = args.run
    adj = receipts.read("adjudicate", run_id) if run_id else receipts.latest("adjudicate")
    if adj is None:
        raise KoptError("no adjudication found; run `kopt adjudicate` first")
    run_id = adj["run_id"]

    verdict = PerfVerdict(adj["verdict"])
    bench = receipts.read("bench", adj["bench_receipt"]) if adj.get("bench_receipt") else None
    verify = (receipts.read("verify", adj["verify_receipt"])
              if adj.get("verify_receipt") else None)

    cal = None
    if bench and bench.get("case"):
        cal = _calibration_for(project, _case_label(bench["case"]))

    candidate = (bench or {}).get("candidate_path")
    front = {
        "run_id": run_id,
        "verdict": verdict.value,
        "stage": args.stage or "unspecified",
        "created_at": utc_now(),
        "speedup": adj.get("point"),
        "speedup_ci95": [adj.get("ci_low"), adj.get("ci_high")],
        "accept_threshold": adj.get("accept_threshold"),
        "pairs_used": adj.get("pairs_used"),
        "pairs_reason": adj.get("pairs_reason"),
        "noise_floor_sigma": (cal or {}).get("sigma"),
        "calibration_id": (cal or {}).get("id"),
        "correctness": (verify or {}).get("verdict"),
        "verify_receipt": adj.get("verify_receipt"),
        "bench_receipt": adj.get("bench_receipt"),
        "measurement_id": adj.get("measurement_id"),
        "source_sha256": digest_path(candidate) if candidate else None,
        "source_path": candidate,
        "spec_digest": spec.digest,
        "rank_tier": verdict.rank_tier,
        # 只让程序写、程序冻 —— 正文不得改写其中任何字段
        "written_by": "kopt",
    }
    path = project.docs_opt / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "front-matter.json").write_text(
        json.dumps(front, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    # 正文里允许出现、且必须对得上的数字
    allowed = {k: v for k, v in front.items()
               if isinstance(v, (int, float)) and v is not None}
    if verify:
        for r in verify.get("results", []):
            dev = r.get("deviation") or {}
            if dev.get("value") is not None:
                allowed[f"dev[{r['case']['shape']}:{r['case']['dtype']}:seed{r['seed']}]"] = dev["value"]
    if bench:
        for i, ratio in enumerate(bench.get("ratios", [])):
            allowed[f"ratio[pair{i}]"] = ratio

    target_dir = verdict.prose_dir or "(none — this verdict needs no prose)"
    lines = [
        f"NARRATE {verdict.value} run={run_id}",
        f"  front-matter written → {path.relative_to(project.root)}/front-matter.json",
        f"  prose must be written to {target_dir}/",
        f"  filename: {run_id}-<slug>.md",
        "",
        "  正文只许使用下列数字（程序给的，不是你算的）：",
    ]
    for k, v in allowed.items():
        lines.append(f"    {k} = {v}")
    ledger = Ledger(project.root)
    ledger.append("narrate", {"run_id": run_id, "verdict": verdict.value})
    _emit(args, "\n".join(lines), {"run_id": run_id, "front_matter": front,
                                   "allowed_numbers": allowed,
                                   "target_dir": target_dir})
    return 0


def cmd_doc_gate(args) -> int:
    """校验归档散文。设计 §7.4。"""
    from .docgate import check_prose, flatten_numbers

    project = _project(args)
    receipts = Receipts(project.root)
    run_id = args.run
    adj = receipts.read("adjudicate", run_id) if run_id else receipts.latest("adjudicate")
    if adj is None:
        raise KoptError("no adjudication found; nothing to check against")
    run_id = adj["run_id"]

    fm_path = project.docs_opt / run_id / "front-matter.json"
    front_matter = (json.loads(fm_path.read_text(encoding="utf-8"))
                    if fm_path.exists() else {"run_id": run_id,
                                              "verdict": adj["verdict"]})

    allowed: dict[str, Any] = {}
    for kind, rid in (("bench", adj.get("bench_receipt")),
                      ("verify", adj.get("verify_receipt"))):
        if not rid:
            continue
        # 用扁平化后的数字，不要放整张收据 —— check_prose 会把字面量当数字，
        # 放整张收据等于把文件名、路径、路径里的数字一起放行。
        for k, v in flatten_numbers(receipts.read(kind, rid)).items():
            allowed[f"{kind}.{k}"] = v

    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = project.root / args.file
        checks = [check_prose(target, front_matter=front_matter,
                              allowed=allowed)]
    else:
        d = project.root / (PerfVerdict(adj["verdict"]).prose_dir or "docs/opt")
        files = sorted(d.glob(f"{run_id}-*.md"))
        if not files:
            check = check_prose(d / f"{run_id}-missing.md",
                                front_matter=front_matter, allowed=allowed)
            checks = [check]
        else:
            checks = [check_prose(f, front_matter=front_matter,
                                  allowed=allowed) for f in files]

    ok = all(c.ok for c in checks)
    Ledger(project.root).append("doc_gate", {
        "run_id": run_id, "ok": ok,
        "checked": [str(c.path.name) for c in checks],
    })
    _emit(args, "\n".join(c.render() for c in checks),
          {"run_id": run_id, "ok": ok,
           "checks": [{"file": str(c.path.name), "ok": c.ok,
                       "problems": c.problems,
                       "unverified_numbers": c.unverified_numbers}
                      for c in checks]})
    return 0 if ok else 1


def cmd_pending(args) -> int:
    project = _project(args)
    pend = pending_obligations(project.root, Ledger(project.root))
    if not pend:
        _emit(args, "no pending prose obligations", {"pending": []})
        return 0
    text = "\n".join(p.render() for p in pend)
    _emit(args, text, {"pending": [p.__dict__ for p in pend]})
    return 1


def cmd_inspect(args) -> int:
    """只读审阅：看编译器生成了什么。设计 §9c。

    注意这里**没有** `_emit(args, ..., payload)` 的收据：汇编码不进收据，
    它是 agent 的判断辅助材料，不是判决证据。见 inspect.py 的模块注释。
    """
    from .inspect import read_assembly

    project = _project(args)
    view = read_assembly(project.root, args.target, max_lines=args.max_lines)
    _emit(args, view.render(), {"path": str(view.path),
                                "truncated": view.truncated,
                                "text": view.text})
    return 0


def cmd_log_event(args) -> int:
    """把一条事件写进会话日志。设计 `forge-kb-design.md` §13。

    这条命令存在的意思就一句话：**日志由程序写，agent 改不了**。
    插件每调一次工具就会顺手调一次它，参数由插件组装，agent 传不进来。

    但要说清楚它的真实分量：它**不是**一段不可篡改的审计链。agent 理论上
    能自己拼出同样的命令，写一条假事件。它防的是**遗忘和偷懒**，不是
    恶意伪造；后者要靠 git 历史 + 人复核。别把它当成比它更强的东西。
    """
    from .eventlog import EventLog

    payload = json.loads(args.payload or "{}")
    if not isinstance(payload, dict):
        raise KoptError("--payload must be a JSON object")
    log_root = args.log_root or os.environ.get("FORGE_LOG_ROOT")
    session = args.session or os.environ.get("FORGE_SESSION")
    if not log_root or not session:
        # 规矩：没有日志仓库就明说，不要写到某个默认位置去。
        # 「以为记了其实没记」比「没记」更难查。
        raise SpecError(
            "log-event needs --log-root and --session (or $FORGE_LOG_ROOT / "
            "$FORGE_SESSION). The log lives in the Forge-Logs repository, not "
            "next to the project — so there is no sensible default.")
    log = EventLog(root=Path(log_root), session_id=session,
                   agent=args.agent or "unknown")
    if args.kind == "note_attempt":
        path = log.note(str(payload.get("text", "")))
        _emit(args, f"note appended to {path}", {"path": str(path)})
        return 0
    if args.kind == "tool_call":
        log.call(**payload)
    else:
        log.action(args.kind, **payload)
    _emit(args, f"logged {args.kind} -> {log.directory}", {"dir": str(log.directory)})
    return 0


# ---------------------------------------------------------------- 其它

def cmd_guard(args) -> int:
    """受保护路径的写权限裁决（供外部检查用）。"""
    project = _project(args)
    results = []
    for rel in args.path:
        kind = project.classify(project.root / rel)
        results.append({"path": rel, "class": kind})
        if kind != "writable":
            print(f"{rel}: {kind} — DISALLOWED")
        else:
            print(f"{rel}: writable")
    if any(r["class"] != "writable" for r in results):
        raise ProtectedPathError("one or more paths are not writable by agents")
    return 0


def cmd_version(args) -> int:
    _emit(args, f"kopt {__version__}", {"version": __version__})
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kopt",
        description="Operator-optimization harness. Program steers, evidence decides.",
    )
    p.add_argument("--root", default=".", help="project root (default: cwd)")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--version", action="version", version=f"kopt {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("probe", help="S0/S1: probe hardware and software, render report")
    s.add_argument("--force", action="store_true",
                   help="re-probe even if the environment is already frozen")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("probe-approve", help="human gate: freeze env.lock")
    s.add_argument("--by", default="user")
    s.add_argument("--note", default=None)
    s.set_defaults(func=cmd_probe_approve)

    s = sub.add_parser("probe-amend", help="human gate: correct one fact")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--why", required=True)
    s.add_argument("--by", default="user")
    s.set_defaults(func=cmd_probe_amend)

    s = sub.add_parser("probe-reject", help="human gate: reject the report")
    s.add_argument("--why", required=True)
    s.add_argument("--by", default="user")
    s.set_defaults(func=cmd_probe_reject)

    s = sub.add_parser("probe-status", help="show gate state")
    s.set_defaults(func=cmd_probe_status)

    s = sub.add_parser("calibrate", help="A/A noise floor")
    s.add_argument("--case", default=None)
    s.add_argument("--pairs", type=int, default=5)
    s.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("verify", help="precision verification (hard gate)")
    s.add_argument("candidate", nargs="?")
    s.add_argument("--baseline", action="store_true",
                   help="verify the reference implementation against itself")
    s.add_argument("--case", default=None)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("bench", help="paired ABBA timing (sole timing source)")
    s.add_argument("candidate")
    s.add_argument("--case", default=None)
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("adjudicate", help="programmatic verdict")
    s.add_argument("--bench", default=None)
    s.add_argument("--verify", default=None)
    s.add_argument("--run", default=None)
    s.add_argument("--timing-inconsistent", action="store_true")
    s.set_defaults(func=cmd_adjudicate)

    s = sub.add_parser("propose", help="start a new round (refused while prose is owed)")
    s.set_defaults(func=cmd_propose)

    s = sub.add_parser("pending", help="list owed prose write-ups")
    s.set_defaults(func=cmd_pending)

    s = sub.add_parser("narrate", help="emit the machine-written half of the prose")
    s.add_argument("--run", default=None)
    s.add_argument("--stage", default=None)
    s.set_defaults(func=cmd_narrate)

    s = sub.add_parser("doc-gate", help="validate archived prose")
    s.add_argument("--run", default=None)
    s.add_argument("--file", default=None)
    s.set_defaults(func=cmd_doc_gate)

    s = sub.add_parser("inspect",
                       help="[read-only] disassemble a compiled artifact (design §9c)")
    s.add_argument("target", help="project-relative path to a .so/.o/.a/.elf")
    s.add_argument("--max-lines", type=int, default=400)
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("log-event",
                       help="[program-only] append to the session event log "
                            "(never a knowledge entry)")
    s.add_argument("kind", choices=["tool_call", "guard", "inspect", "gate_refused",
                                    "note_attempt", "kb_action", "prose"])
    s.add_argument("--payload", default="{}",
                   help="JSON object; free-form, the log records it verbatim")
    s.add_argument("--log-root", default=None,
                   help="log repository root (default: $FORGE_LOG_ROOT)")
    s.add_argument("--session", default=None,
                   help="session id (default: $FORGE_SESSION)")
    s.add_argument("--agent", default=None,
                   help="agent name recorded in manifest.json")
    s.set_defaults(func=cmd_log_event)

    s = sub.add_parser("guard", help="classify paths for write permission")
    s.add_argument("path", nargs="+")
    s.set_defaults(func=cmd_guard)

    # 人闸门的三个动作，走独立入口而不是 `probe approve`。
    # 原因：agent 手上的 `probe` 工具只发射探针；冻结环境这件事必须由人触发，
    # 否则「先报告、后批准」退化成模型自己点通过。
    s = sub.add_parser("gate-approve", help="[human] approve the probe report, freeze env.lock")
    s.add_argument("--note", default=None)
    s.add_argument("--by", default="user")
    s.set_defaults(func=cmd_gate_approve)

    s = sub.add_parser("gate-amend", help="[human] correct one probed fact, then freeze")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--why", required=True)
    s.add_argument("--by", default="user")
    s.set_defaults(func=cmd_gate_amend)

    s = sub.add_parser("gate-reject", help="[human] reject the probe report")
    s.add_argument("--why", required=True)
    s.add_argument("--by", default="user")
    s.set_defaults(func=cmd_gate_reject)

    s = sub.add_parser("version", help="print version")
    s.set_defaults(func=cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KoptError as exc:
        # 错误一律走 stderr，退出码有意义 —— 宿主据此判「命令是否成功」。
        print(f"kopt: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return exc.exit_code
    except FileNotFoundError as exc:
        print(f"kopt: missing file: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
