"""探针 —— S0。现场取证，不猜。

设计 §3 / 03_atrex §8 的 fail-loud 规矩：

    「记住的硬件细节只是假设」（记忆会过期）。
    返回一条不相关的优化经验还能用，
    返回一个不相关的峰值 FLOPS 会污染所有利用率计算。

所以这里的每一个 Fact 都必须带着：
  * 一条**实际执行过**的命令（或明确标注为「未执行」）
  * 命令的真实退出码
  * 原始输出片段
  * 置信度（measured / documented / unknown）

没有执行过的，不许写成事实；拿不到的，写 unknown，不写 0。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from .verdicts import Confidence

# 命令超时。探针不该把流程卡住 —— 卡住就是拿不到，标 unknown。
PROBE_TIMEOUT_S = 10.0
SNIPPET_LIMIT = 2000


@dataclass
class Fact:
    key: str
    value: Any
    confidence: Confidence
    source: str                 # 实际执行的命令，或 "not run: <原因>"
    excerpt: str = ""           # 原始输出片段
    exit_code: int | None = None
    origin: str = "probe"       # probe | human
    why: str | None = None      # 人工修正时必填

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence.value,
            "source": self.source,
            "exit_code": self.exit_code,
            "excerpt": self.excerpt,
            "origin": self.origin,
            "why": self.why,
        }


def _run(cmd: list[str]) -> tuple[int | None, str]:
    """执行一条命令，返回 (退出码, 输出片段)。失败不抛，回 (None, 原因)。

    注意 `shutil.which` 找不到时返回 None 这条路径**不算测过**
    （例如精简容器里根本没有 `gcc`），所以它落到 UNKNOWN 而不是 false。
    但一个跑起来了、只是退出码非零的命令（比如 `perf` 存在却无权限）
    是**测到的事实**：能力不可用。
    """
    exe = shutil.which(cmd[0])
    if exe is None:
        return None, f"executable not found: {cmd[0]}"
    try:
        proc = subprocess.run(  # noqa: S603 - argv 由本模块决定，不经 shell
            [exe, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {PROBE_TIMEOUT_S:g}s"
    except OSError as exc:  # noqa: BLE001
        return None, f"failed to start: {exc!r}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _fact_from_cmd(key: str, cmd: list[str],
                   transform: Callable[[str], Any] | None = None) -> Fact:
    """跑命令并把输出变成一个 Fact。命令没跑成 ⇒ UNKNOWN。"""
    shown = " ".join(cmd)
    code, out = _run(cmd)
    if code is None:
        return Fact(key, None, Confidence.UNKNOWN, f"not run: {shown}", out)
    if code != 0:
        return Fact(key, None, Confidence.UNKNOWN, shown, out[:SNIPPET_LIMIT], code)
    text = out.strip()
    value = transform(text) if transform else (text.splitlines()[0] if text else "")
    return Fact(key, value, Confidence.MEASURED, shown, text[:SNIPPET_LIMIT], code)


def _fact_from_probe(key: str, cmd: list[str],
                     transform: Callable[[str], Any]) -> Fact:
    """像 `_fact_from_cmd`，但「程序存在、跑起来失败」也算测到。

    专门给探测能力用：`perf` 装了却没有权限，结论是「profiler 不可用」——
    这是一条实测事实，不是「我们不知道」。把这两种情形混在一起，
    报告就会把「确认没有」写成「没查」。
    """
    shown = " ".join(cmd)
    code, out = _run(cmd)
    if code is None:
        return Fact(key, None, Confidence.UNKNOWN, f"not run: {shown}", out)
    if code == 0:
        try:
            value = transform(out)
        except (ValueError, IndexError):
            return Fact(key, None, Confidence.UNKNOWN, shown,
                        out[:SNIPPET_LIMIT], code)
        return Fact(key, value, Confidence.MEASURED, shown,
                    out.strip()[:SNIPPET_LIMIT], code)
    # 跑起来了、失败了：能力不可用，是确定的
    return Fact(f"{key}.available", False, Confidence.MEASURED, shown,
                out.strip()[:SNIPPET_LIMIT], code)


# ---------------------------------------------------------------- 硬件


def probe_hardware() -> list[Fact]:
    facts: list[Fact] = []
    uname = platform.uname()

    facts.append(Fact("hardware.hostname", uname.node, Confidence.MEASURED,
                      "platform.uname()", uname.node, 0))
    facts.append(Fact("hardware.os", f"{uname.system} {uname.release}",
                      Confidence.MEASURED, "platform.uname()", uname.release, 0))
    facts.append(Fact("hardware.machine", uname.machine, Confidence.MEASURED,
                      "platform.uname()", uname.machine, 0))

    # CPU 型号 —— 各平台命令不同，拿不到就 unknown
    if sys.platform.startswith("linux"):
        f = _fact_from_cmd(
            "hardware.cpu.model",
            ["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"],
        )
        facts.append(f)
        facts.append(_fact_from_cmd(
            "hardware.cpu.logical_cores",
            ["sh", "-c", "nproc"],
            transform=lambda t: int(t),
        ))
        facts.append(_fact_from_cmd(
            "hardware.memory.total_bytes",
            ["sh", "-c", "awk '/MemTotal/{print $2*1024}' /proc/meminfo"],
            transform=lambda t: int(t),
        ))
        facts.append(_fact_from_cmd(
            "hardware.processor_count",
            ["sh", "-c", "nproc"],
            transform=lambda t: int(t),
        ))
    elif sys.platform == "win32":
        facts.append(Fact("hardware.cpu.model", platform.processor() or None,
                          Confidence.MEASURED if platform.processor()
                          else Confidence.UNKNOWN,
                          "platform.processor()", platform.processor(), 0))
        facts.append(Fact("hardware.cpu.logical_cores", os.cpu_count(),
                          Confidence.MEASURED, "os.cpu_count()",
                          str(os.cpu_count()), 0))
        # Windows 上不跑 wmic（已弃用）、也不猜 —— 直接标 unknown。
        facts.append(Fact(
            "hardware.memory.total_bytes", None, Confidence.UNKNOWN,
            "not run: no portable command on this platform", "",
            None))
    else:
        facts.append(Fact("hardware.cpu.model", platform.processor() or None,
                          Confidence.MEASURED if platform.processor()
                          else Confidence.UNKNOWN,
                          "platform.processor()", platform.processor(), 0))
        facts.append(Fact("hardware.cpu.logical_cores", os.cpu_count(),
                          Confidence.MEASURED, "os.cpu_count()",
                          str(os.cpu_count()), 0))

    # 负载 —— 直接影响 5 配对的噪声。拿不到就 unknown，不假装空闲。
    if sys.platform.startswith("linux"):
        facts.append(_fact_from_cmd(
            "hardware.loadavg.1min",
            ["sh", "-c", "cut -d' ' -f1 /proc/loadavg"],
            transform=lambda t: float(t),
        ))
    else:
        facts.append(Fact("hardware.loadavg.1min", None, Confidence.UNKNOWN,
                          "not run: no /proc/loadavg on this platform", "", None))

    return facts


# ---------------------------------------------------------------- 软件


def probe_software() -> list[Fact]:
    facts: list[Fact] = []
    facts.append(Fact(
        "software.python.version", platform.python_version(), Confidence.MEASURED,
        "platform.python_version()", sys.version.split()[0], 0))
    facts.append(Fact(
        "software.python.executable", sys.executable, Confidence.MEASURED,
        "sys.executable", sys.executable, 0))

    # numpy 是硬依赖；拿不到版本说明环境有问题，要显式暴露
    try:
        import numpy

        facts.append(Fact("software.numpy.version", numpy.__version__,
                          Confidence.MEASURED, "numpy.__version__",
                          numpy.__version__, 0))
    except Exception as exc:  # noqa: BLE001
        facts.append(Fact("software.numpy.version", None, Confidence.UNKNOWN,
                          "not run: import numpy", repr(exc), None))

    # profiler 是否存在、是否有权限（很多容器里没有）—— 一律按「可选」处理。
    # 装了但没权限，结论是「不可用」，那是测到的，不是 unknown。
    facts.append(_fact_from_probe(
        "software.perf", ["perf", "--version"],
        transform=lambda t: t.splitlines()[0] if t.strip() else ""))
    facts.append(_fact_from_probe(
        "software.gcc", ["gcc", "--version"],
        transform=lambda t: t.splitlines()[0] if t.strip() else ""))

    # 沙箱能力：拿不到不影响正确性，但要写进报告让人知道隔离强度
    bwrap = shutil.which("bwrap")
    facts.append(Fact(
        "software.sandbox.bwrap", bool(bwrap),
        Confidence.MEASURED if bwrap else Confidence.MEASURED,
        "shutil.which('bwrap')", str(bwrap), 0))

    # 环境变量：只记录可能影响计时的，不 dump 全部（避免泄漏凭证）
    #
    # 「没设置」本身就是一个测到的事实（value=None），不是「没测到」。
    # 这两者混淆会让报告把「这台机器是干净默认」误报成「我们不知道」。
    interesting = ("OMP_NUM_THREADS", "KMP_AFFINITY", "MKL_NUM_THREADS",
                   "OPENBLAS_NUM_THREADS")
    for key in interesting:
        val = os.environ.get(key)
        facts.append(Fact(
            f"software.env.{key}", val,
            Confidence.MEASURED,           # 查过 os.environ 了，结论是确定的
            f"os.environ.get({key!r})",
            val if val is not None else "(unset)", 0))

    return facts


def probe_all() -> list[Fact]:
    return [*probe_hardware(), *probe_software()]


# ---------------------------------------------------------------- 渲染


def to_env_lock_preview(facts: list[Fact]) -> dict[str, Any]:
    return {f.key: f.value for f in facts}


def render_report(facts: list[Fact], *, project_name: str = "",
                  generated_at: str = "") -> str:
    """渲染《环境探查报告》。两段式：人话段 + 事实表（设计 §3.3）。"""
    hw = [f for f in facts if f.key.startswith("hardware.")]
    sw = [f for f in facts if f.key.startswith("software.")]
    unknown = [f for f in facts if f.confidence is Confidence.UNKNOWN]
    human_edits = [f for f in facts if f.origin == "human"]

    lines: list[str] = []
    lines.append(f"# 环境探查报告{(' — ' + project_name) if project_name else ''}")
    lines.append("")
    if generated_at:
        lines.append(f"> 生成于 {generated_at}。**这份报告需要你过目并批准后才能继续。**")
        lines.append("")

    # ---- 人话段 ----
    lines.append("## 一、这台机器是什么")
    lines.append("")
    measurable = [f for f in facts if f.confidence is Confidence.MEASURED]
    lines.append(
        f"共采集到 {len(facts)} 条事实，其中 {len(measurable)} 条是本机实测，"
        f"{len(unknown)} 条没能测到。"
        f"下面这张表里的每一条都可以追溯到一条实际执行过的命令 —— "
        f"没执行过的不会被写成事实。"
    )
    lines.append("")
    if unknown:
        lines.append(
            f"**有 {len(unknown)} 项没能测到**，它们被显式标成 `unknown` 而不是填 0 或默认值。"
            f"原因是这些数字一旦是猜的，后面所有基于它们的利用率推算都会跟着错，"
            f"而且是那种看不出来的错。"
        )
        lines.append("")
    if human_edits:
        lines.append(
            f"其中 {len(human_edits)} 条来自人工修正（标了 `source: human` 和理由）。"
            f"人也会记错，所以修正和实测在表里是分开记的。"
        )
        lines.append("")

    if sw:
        py = next((f for f in facts if f.key == "software.python.version"), None)
        np_ = next((f for f in facts if f.key == "software.numpy.version"), None)
        if py and py.value:
            lines.append(
                f"软件侧：Python {py.value}"
                + (f"，numpy {np_.value}" if np_ and np_.value else "")
                + "。"
            )
            lines.append("")

    # ---- 硬件表 ----
    lines.append("## 二、硬件事实")
    lines.append("")
    lines.append(_fact_table(hw))
    lines.append("")

    lines.append("## 三、软件事实")
    lines.append("")
    lines.append(_fact_table(sw))
    lines.append("")

    lines.append("## 四、这一份报告会被冻结成什么")
    lines.append("")
    lines.append(
        "批准之后，上面这些事实会被写进 `env.lock.json` 并计算内容哈希。"
        "这个哈希会进入之后**每一次测量的身份**。"
        "所以换一台机器、换一个工具链版本，旧结论会自动失效，"
        "而不是悄悄继续被引用。"
    )
    lines.append("")
    return "\n".join(lines)


def _fact_table(facts: list[Fact]) -> str:
    if not facts:
        return "_（无）_"
    rows = ["| 事实 | 值 | 置信 | 来源命令 |", "|---|---|---|---|"]
    for f in facts:
        val = "`unknown`" if f.value is None else _md_cell(f.value)
        src = _md_cell(f.source)
        if f.origin == "human":
            src += f" · 人工修正：{_md_cell(f.why or '')}"
        rows.append(f"| `{f.key}` | {val} | {f.confidence.value} | {src} |")
    return "\n".join(rows)
def _md_cell(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s.replace("|", "\\|").replace("\n", " ")[:200] or "_(空)_"
