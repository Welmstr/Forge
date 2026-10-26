"""只读的产物审阅：看编译器到底生成了什么。

设计依据：`kopt-harness-design.md` §9c（本轮新增）。
分类依据：编译参数属于**性能优化**（agent 可以选），产物汇编码属于**验证**
（agent 只能看，不能选）。

这个模块存在的唯一理由是——**给 agent 一只眼睛，不给它一只手。**

为什么不开一个通用 shell 工具：一旦 agent 能跑任意命令，它就能自己写
`time.perf_counter()` 的脚本，配对 bootstrap 的判决立刻退化成「模型说它更快」。
所以这里不是「跑命令」，是**打开一个文件看一眼**。命令行参数由本模块拼死，
外部传不进来任何东西，也就不存在拼命令这回事。

只做只读。没有 `--compile`，没有 `--run`，没有 `--perf`。
想看某个编译参数的效果，路径是：改 spec → 重编译 → 再来看这里。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import ObjDumpError, ProtectedPathError

# 只允许看这几类目标文件。不在表里的一律拒绝，而不是「试试看」。
ALLOWED_SUFFIXES = (".so", ".o", ".a", ".elf", ".node", ".pyd")

# 反汇编命令的固定选项：只要指令本身，不要重定位和杂项。
OBJDUMP_FLAGS = ("-d", "--no-show-raw-insn")


@dataclass(frozen=True)
class AsmView:
    path: Path
    size_bytes: int
    text: str
    truncated: bool
    tool: str

    def render(self) -> str:
        head = (f"asm view: {self.path}  ({self.size_bytes} bytes)\n"
                f"tool: {self.tool} {' '.join(OBJDUMP_FLAGS)}\n")
        if self.truncated:
            head += "(output truncated to 400 lines — narrow the target to see the rest)\n"
        return head + "\n" + self.text


def _resolve_target(root: Path, relative: str) -> Path:
    """把用户给的路径解析成项目里的真实文件，并挡掉越界和受保护路径。"""
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ProtectedPathError(
            f"{relative}: outside the project root. Inspect only works on files "
            f"this project produced.") from exc

    if not target.is_file():
        raise ObjDumpError(
            f"{relative}: no such file. If nothing was compiled yet, there is "
            f"nothing to inspect — that is not the same as an empty result.")
    if target.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ObjDumpError(
            f"{relative}: not a compiled artifact (suffix {target.suffix!r}). "
            f"Allowed: {', '.join(ALLOWED_SUFFIXES)}. Source files are read as "
            f"source, not disassembled — and this harness never compiles for you.")
    return target


def read_assembly(root: Path, relative: str, *, max_lines: int = 400) -> AsmView:
    """反汇编一个目标文件。只读，不改任何东西，不生成任何收据。

    为什么不留收据：汇编码属于**验证证据**，不属于判决。判决只能由
    verify+bench+adjudicate 这条链产生。把每次 `objdump` 都写成收据，
    会让「收据」这个概念从「可判决的证据」稀释成「一切发生过的事」。
    想留痕的话，那是日志的事（Forge-Logs），不是收据的事。
    """
    target = _resolve_target(root, relative)

    tool = shutil.which("objdump")
    if tool is None:
        raise ObjDumpError(
            "objdump is not installed. Installing toolchain binaries is a human "
            "decision; the harness will not shell out to apt or pip to get one.")

    try:
        proc = subprocess.run(
            [tool, *OBJDUMP_FLAGS, str(target)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ObjDumpError(f"objdump timed out after 60s on {relative}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise ObjDumpError(f"objdump refused {relative}: {stderr or 'no message'}")

    lines = proc.stdout.splitlines()
    truncated = len(lines) > max_lines
    text = "\n".join(lines[:max_lines])

    return AsmView(
        path=target, size_bytes=target.stat().st_size,
        text=text or "(objdump produced no disassembly)",
        truncated=truncated, tool=tool,
    )
