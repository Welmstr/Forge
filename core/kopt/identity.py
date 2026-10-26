"""测量身份 = 内容哈希，不是「第 N 次实验」。

设计 §6 / 07 定律 2（atrex、cuda-opt-agent、Xe-Forge、AKA 四家独立出现）。

一个机制同时解决三件事：
  缓存   —— 同一身份直接返回旧记录，不重跑
  去重   —— 不重复花钱
  复现   —— 能证明当时用的到底是哪份代码

关键后果：机器一变、spec 一变、参考实现一变，身份就变，**旧结论自动失效**，
而不是悄悄继续被引用。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def canonical_json(obj: Any) -> str:
    """稳定的 JSON 序列化 —— 同样的内容必须产生同样的字节。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def digest_obj(obj: Any) -> str:
    return digest_text(canonical_json(obj))


def digest_file(path: str | Path) -> str:
    """单个文件的 sha256。文件不存在直接抛 —— 不返回空哈希。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def digest_path(path: str | Path) -> str:
    """文件或目录的内容摘要。

    目录按「相对路径 + 文件内容」排序后逐一喂进去，所以改名也会改哈希。
    06_Xe-Forge §7：trial 记录源码 sha256，单文件或整个目录都要能算。
    """
    p = Path(path)
    if p.is_file():
        return digest_file(p)
    if not p.is_dir():
        raise FileNotFoundError(f"cannot digest missing path: {p}")

    h = hashlib.sha256()
    for child in sorted(p.rglob("*")):
        if not child.is_file():
            continue
        if any(part in {"__pycache__", ".git"} for part in child.parts):
            continue
        rel = child.relative_to(p).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest_file(child).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def short(digest: str, n: int = 12) -> str:
    return digest[:n]


def measurement_identity(
    *,
    candidate_digest: str,
    reference_digest: str,
    spec_digest: str,
    env_lock_digest: str,
    cases: list[dict[str, Any]],
    timing: dict[str, Any],
    harness_version: str,
) -> str:
    """测量任务的身份。

    03_atrex §4 的原话：
      测量任务的标识 = 源字节 + 输入 + 操作 + 测量选项 + 硬件/传输
                     + 评测器代码哈希 + 重复策略。

    这里逐项对应：
      candidate/reference_digest  源字节
      cases                       输入与操作（形状/dtype/种子）
      timing                      测量选项与重复策略（warmup/配对数/交错顺序）
      env_lock_digest             硬件与软件事实（已被人类批准并冻结）
      spec_digest                 评测口径（容差、metric、目标收益）
      harness_version             评测器代码版本
    """
    payload = {
        "candidate": candidate_digest,
        "reference": reference_digest,
        "spec": spec_digest,
        "env_lock": env_lock_digest,
        "cases": cases,
        "timing": timing,
        "harness": harness_version,
    }
    return digest_obj(payload)
