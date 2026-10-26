"""收据与流水账：机器写的证据，只追加，写完不改。

设计 §4.4 / §6 / §8。

两条规矩：
  1. 收据一旦写下就不可变 —— 用排他创建，重复写同一个 id 直接报错。
  2. 散文里出现的任何数字，必须能在收据里找到（doc-gate 据此校验）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import KoptError
from .identity import canonical_json, digest_obj, short


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Receipts:
    """收据库。`receipts/<kind>/<id>.json`。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def dir_for(self, kind: str) -> Path:
        return self.root / "receipts" / kind

    def write(self, kind: str, payload: dict[str, Any], *,
              receipt_id: str | None = None) -> tuple[str, Path]:
        """写一张收据，返回 (id, path)。

        id 默认取内容哈希前 12 位 —— 同样的证据写两次会得到同样的 id，
        于是第二次会因为文件已存在而失败。这正是我们要的：
        **同一身份的测量不该被重复记账**（07 定律 2）。
        """
        body = dict(payload)
        body.setdefault("recorded_at", utc_now())
        rid = receipt_id or short(digest_obj(body))
        d = self.dir_for(kind)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{rid}.json"
        try:
            # 'x' = 排他创建。收据不可变，不允许覆盖。
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(canonical_json({"id": rid, "kind": kind, **body}))
                fh.write("\n")
        except FileExistsError:
            # 内容完全相同 ⇒ 幂等返回；内容不同却撞 id ⇒ 这是真错误。
            existing = json.loads(path.read_text(encoding="utf-8"))
            trimmed = {k: v for k, v in existing.items()
                       if k not in {"id", "kind", "recorded_at"}}
            incoming = {k: v for k, v in body.items() if k != "recorded_at"}
            if trimmed != incoming:
                raise KoptError(
                    f"receipt id collision with different content: {path}"
                ) from None
        return rid, path

    def read(self, kind: str, receipt_id: str) -> dict[str, Any]:
        path = self.dir_for(kind) / f"{receipt_id}.json"
        if not path.exists():
            raise KoptError(f"no such receipt: {kind}/{receipt_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest(self, kind: str) -> dict[str, Any] | None:
        d = self.dir_for(kind)
        if not d.is_dir():
            return None
        items = [json.loads(p.read_text(encoding="utf-8"))
                 for p in d.glob("*.json")]
        if not items:
            return None
        return max(items, key=lambda r: r.get("recorded_at", ""))


class Ledger:
    """只追加的流水账 `journal/ledger.jsonl`。

    每一行是一个事件。永不改写、永不删行 —— 要更正，追加一条新的。
    """

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "journal" / "ledger.jsonl"

    def append(self, event: str, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json({"at": utc_now(), "event": event, **payload})
        # 以追加模式打开并一次写完整行：并发追加下不会撕裂。
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())

        def gen() -> Iterator[dict[str, Any]]:
            with open(self.path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if raw:
                        yield json.loads(raw)

        return gen()

    def last(self, event: str | None = None) -> dict[str, Any] | None:
        found = None
        for row in self.read():
            if event is None or row.get("event") == event:
                found = row
        return found
