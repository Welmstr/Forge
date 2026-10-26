"""题目条件 `spec.toml` —— 精度阈值的唯一来源。

设计 §4.1。最硬的一条规矩来自 03_atrex §6：

    数值规划器**不能提供阈值**。

所以：
  * `spec.toml` 在受保护路径里，agent 不可写。阈值是题目条件，不是可调参数。
  * dtype 没有声明容差 ⇒ UNSUPPORTED，流程停。不是默认值，不是通过。
  * agent 不许提交 Python 校验代码，只能提交声明式用例计划，由程序去执行。
"""

from __future__ import annotations

try:                                    # Python >= 3.11
    import tomllib
except ModuleNotFoundError:             # pragma: no cover - 3.10 及更早
    # tomllib 是 3.11 才进标准库的。Ubuntu 22.04 自带的 python3 是 3.10，
    # 装不了 kopt 会很尴尬，所以这里退一步用 tomli（同一个作者、同一套 API）。
    # 缺了就说清楚缺什么，而不是让人对着 ImportError 猜。
    try:
        import tomli as tomllib      # type: ignore[no-redef]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ModuleNotFoundError(
            "kopt needs TOML support. On Python >= 3.11 it uses the stdlib "
            "`tomllib`; on older interpreters install `tomli` "
            "(pip install 'tomli>=2.0')."
        ) from exc

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SpecError, UnsupportedError
from .identity import digest_file

# 支持的偏差度量。想加新的，改这里并写清楚语义 —— 不接受 spec 里现编的名字。
METRICS = ("rel_l2", "max_abs", "max_rel", "ulp")

# numpy dtype 名 → spec 里允许的写法
DTYPES = {
    "fp16": "float16", "float16": "float16",
    "bf16": "bfloat16",
    "fp32": "float32", "float32": "float32",
    "fp64": "float64", "float64": "float64",
    "int8": "int8", "int32": "int32", "int64": "int64",
}

# 配对数的硬下限。设计 §5.4：只能变严，不能被调松。
# 写进代码而不是写进配置 —— 配置能被改，常量不能。
HARD_MIN_PAIRS = 5


@dataclass(frozen=True)
class Tolerance:
    dtype: str
    metric: str
    threshold: float

    def as_dict(self) -> dict[str, Any]:
        return {"dtype": self.dtype, "metric": self.metric,
                "threshold": self.threshold}


@dataclass(frozen=True)
class Case:
    shape: tuple[int, ...]
    dtype: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{'x'.join(map(str, self.shape))}:{self.dtype}"

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"shape": list(self.shape), "dtype": self.dtype}
        if self.params:
            d["params"] = self.params
        return d


@dataclass(frozen=True)
class BenchConfig:
    warmup: int
    min_pairs: int
    bootstrap_resamples: int
    bootstrap_seed: int
    # 加速比高于这个值就判 SUSPECT，强制回去复验精度（02_AKO4ALL §3）。
    suspect_speedup: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "warmup": self.warmup,
            "min_pairs": self.min_pairs,
            "bootstrap_resamples": self.bootstrap_resamples,
            "bootstrap_seed": self.bootstrap_seed,
            "suspect_speedup": self.suspect_speedup,
        }


@dataclass(frozen=True)
class Spec:
    path: Path
    digest: str
    name: str
    reference: Path
    candidate_entry: str
    min_speedup: float
    tolerances: dict[str, Tolerance]
    cases: tuple[Case, ...]
    seeds: tuple[int, ...]
    fresh_inputs: bool
    bench: BenchConfig

    # ---- 精度阈值查询：唯一入口，查不到就抛 ----

    def tolerance_for(self, dtype: str) -> Tolerance:
        """取某个 dtype 的容差。**没有声明就抛 UnsupportedError。**

        这是设计 §4.1 规矩 2 的执行点。整个代码库里只有这一处能决定「用什么阈值」，
        而它没有任何 fallback 分支 —— 想加 default 的话，先回去读 07 定律 3。
        """
        tol = self.tolerances.get(dtype)
        if tol is None:
            declared = ", ".join(sorted(self.tolerances)) or "(none)"
            raise UnsupportedError(
                f"spec.toml declares no tolerance for dtype '{dtype}'. "
                f"Declared: {declared}. "
                f"Add a [tolerance.{dtype}] section — the harness will not "
                f"invent a threshold."
            )
        return tol

    def cases_as_list(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.cases]


def _req(table: dict[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise SpecError(f"spec.toml: missing required key '{key}' in [{where}]")
    return table[key]


def load_spec(root: str | Path) -> Spec:
    """读取并校验 `<root>/spec.toml`。任何不合规都在这里炸掉，不往后传。"""
    root = Path(root)
    path = root / "spec.toml"
    if not path.exists():
        raise SpecError(
            f"no spec.toml at {path}. The problem conditions (tolerances, "
            f"cases, target) are human-owned and must exist before any "
            f"verification can run."
        )

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    problem = raw.get("problem", {})
    name = _req(problem, "name", "problem")
    ref_rel = _req(problem, "reference", "problem")
    reference = (root / ref_rel).resolve()
    if not reference.exists():
        raise SpecError(f"spec.toml: reference implementation not found: {reference}")
    entry = problem.get("entry", "run")

    # ---- 目标收益 ----
    target = raw.get("target", {})
    min_speedup = float(target.get("min_speedup", 1.01))
    if min_speedup <= 1.0:
        raise SpecError(
            f"spec.toml: target.min_speedup must be > 1.0, got {min_speedup}"
        )

    # ---- 容差表 ----
    tol_table = raw.get("tolerance", {})
    if not isinstance(tol_table, dict) or not tol_table:
        raise SpecError(
            "spec.toml: [tolerance.<dtype>] is required. The harness refuses "
            "to pick accuracy thresholds on your behalf."
        )
    tolerances: dict[str, Tolerance] = {}
    for dtype, body in tol_table.items():
        if dtype not in DTYPES:
            raise SpecError(
                f"spec.toml: unknown dtype '{dtype}' in [tolerance]. "
                f"Known: {', '.join(sorted(DTYPES))}"
            )
        if not isinstance(body, dict):
            raise SpecError(f"spec.toml: [tolerance.{dtype}] must be a table")
        metric = _req(body, "metric", f"tolerance.{dtype}")
        if metric not in METRICS:
            raise SpecError(
                f"spec.toml: [tolerance.{dtype}].metric='{metric}' is not one of "
                f"{', '.join(METRICS)}"
            )
        threshold = float(_req(body, "threshold", f"tolerance.{dtype}"))
        if not (threshold > 0):
            raise SpecError(
                f"spec.toml: [tolerance.{dtype}].threshold must be > 0, "
                f"got {threshold}"
            )
        tolerances[dtype] = Tolerance(dtype, metric, threshold)

    # ---- 用例 ----
    case_list = raw.get("case", [])
    if not case_list:
        raise SpecError("spec.toml: at least one [[case]] is required")
    cases: list[Case] = []
    for i, c in enumerate(case_list):
        shape = _req(c, "shape", f"case[{i}]")
        if (not isinstance(shape, list) or not shape
                or not all(isinstance(x, int) and x > 0 for x in shape)):
            raise SpecError(
                f"spec.toml: case[{i}].shape must be a non-empty list of "
                f"positive integers, got {shape!r}"
            )
        dtype = _req(c, "dtype", f"case[{i}]")
        if dtype not in DTYPES:
            raise SpecError(f"spec.toml: case[{i}].dtype='{dtype}' is unknown")
        cases.append(Case(tuple(shape), dtype, dict(c.get("params", {}))))

    # 每个用例的 dtype 都必须有容差 —— 在加载期就查一遍，不要等跑到一半才炸。
    for c in cases:
        if c.dtype not in tolerances:
            raise UnsupportedError(
                f"spec.toml: case {c.label} uses dtype '{c.dtype}' but no "
                f"[tolerance.{c.dtype}] is declared. The harness will not "
                f"invent a threshold."
            )

    # ---- 验证设置 ----
    verify = raw.get("verify", {})
    seeds = tuple(verify.get("seeds", [0, 1]))
    if len(seeds) < 2:
        raise SpecError(
            f"spec.toml: verify.seeds needs at least 2 seeds (got {list(seeds)}). "
            f"A single seed cannot distinguish a correct kernel from one that "
            f"happens to work on one input draw."
        )
    fresh_inputs = bool(verify.get("fresh_inputs", True))
    if not fresh_inputs:
        raise SpecError(
            "spec.toml: verify.fresh_inputs=false is not supported. Reusing "
            "inputs lets a candidate cache or constant-fold its way to a pass."
        )

    # ---- 计时设置 ----
    b = raw.get("bench", {})
    min_pairs = int(b.get("min_pairs", HARD_MIN_PAIRS))
    if min_pairs < HARD_MIN_PAIRS:
        raise SpecError(
            f"spec.toml: bench.min_pairs={min_pairs} is below the hard floor of "
            f"{HARD_MIN_PAIRS}. Statistical rigour only tightens — see design "
            f"§5.4."
        )
    bench = BenchConfig(
        warmup=int(b.get("warmup", 3)),
        min_pairs=min_pairs,
        bootstrap_resamples=int(b.get("bootstrap_resamples", 2000)),
        bootstrap_seed=int(b.get("bootstrap_seed", 17)),
        suspect_speedup=float(b.get("suspect_speedup", 10.0)),
    )
    if bench.warmup < 0:
        raise SpecError("spec.toml: bench.warmup must be >= 0")

    return Spec(
        path=path,
        digest=digest_file(path),
        name=name,
        reference=reference,
        candidate_entry=entry,
        min_speedup=min_speedup,
        tolerances=tolerances,
        cases=tuple(cases),
        seeds=seeds,
        fresh_inputs=fresh_inputs,
        bench=bench,
    )
