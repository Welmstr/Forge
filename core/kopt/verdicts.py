"""判决词表。

这个文件是整个 harness 的宪法。每一个取值都对应设计文档里的一条定律，
**不要**为了方便新增「大致通过」「基本持平」这类中间态。

07 定律 3：缺失必须显式，不能等于零，也不能等于「通过」。
"""

from __future__ import annotations

from enum import Enum


class Confidence(str, Enum):
    """探针事实的置信度。设计 §3.3。

    只有三档。拿不到就是 UNKNOWN，不许猜 —— 03_atrex §8 的 fail-loud：
    返回一条不相关的优化经验还能用，返回一个不相关的峰值 FLOPS 会污染所有利用率计算。
    """

    MEASURED = "measured"      # 本机实测，有命令和原始输出
    DOCUMENTED = "documented"  # 来自厂商文档/人工填写，未在本机实测
    UNKNOWN = "unknown"        # 测不到。不是 0，不是默认值。


class VerifyVerdict(str, Enum):
    """精度验证结论。设计 §4.2。

    没有第五种，尤其没有「大致通过」。
    """

    PASS = "PASS"                  # 所有选中覆盖全部通过
    FAIL = "FAIL"                  # 至少一个 case 超出声明容差（一个失败即可证伪）
    UNSUPPORTED = "UNSUPPORTED"    # 题目条件没覆盖（无阈值/无可用形状）
    INCONCLUSIVE = "INCONCLUSIVE"  # 执行出错，没拿到结论。不是 FAIL，也不是 PASS。

    @property
    def is_pass(self) -> bool:
        return self is VerifyVerdict.PASS


class PerfVerdict(str, Enum):
    """性能判决。设计 §5.2 / §5.3。

    判决由程序给出，不由 LLM 给出（07 定律 1）。
    """

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    # 既不等于 ACCEPT 也不等于 REJECT，在排行里单独占一档。
    # 06_Xe-Forge §7：只按 speedup 排序会让它隐身，导致最后交出更慢的内核。
    INDISTINGUISHABLE = "INDISTINGUISHABLE"
    # 加速比大得不像话 ⇒ 多半是算少了。强制回去复验精度（02_AKO4ALL §3）。
    SUSPECT = "SUSPECT"
    # 这次比较本身无效：缺一致性标记、有非有限数、配对不齐。
    # 04_AKA §4：显式的「无效」是权威的，不拿平均时间反推（规范 3）。
    INVALID = "INVALID"

    @property
    def requires_prose(self) -> bool:
        """是否必须归档散文才允许进行下一轮。设计 §7.5。

        INVALID / SUSPECT 不要求 —— 那是 harness 自己的故障，不是知识。
        逼模型为一次失效的测量硬写反思，只会产出编造的因果。
        """
        return self in (PerfVerdict.ACCEPT, PerfVerdict.REJECT,
                        PerfVerdict.INDISTINGUISHABLE)

    @property
    def prose_dir(self) -> str | None:
        if self is PerfVerdict.ACCEPT:
            return "docs/opt"
        if self in (PerfVerdict.REJECT, PerfVerdict.INDISTINGUISHABLE):
            return "docs/opt/rejected"
        return None

    @property
    def rank_tier(self) -> int:
        """排序档位。设计 §5.3 / 06_Xe-Forge §7。

        排序键按「门控等级」而不是按 speedup —— 「分辨不出」必须能占位，
        不能因为没有比值就被当成最差而隐身。

        注意：这个数**只用于排序，绝不回写成报告里的测量值**。
        """
        return {
            PerfVerdict.ACCEPT: 0,
            PerfVerdict.INDISTINGUISHABLE: 1,
            PerfVerdict.REJECT: 2,
            PerfVerdict.SUSPECT: 3,
            PerfVerdict.INVALID: 4,
        }[self]


class GateState(str, Enum):
    """人类闸门状态。设计 §3.4 / §9b.3。

    状态落盘，跨会话存活 —— 不能寄存在宿主的一次性审批服务上。
    """

    ABSENT = "absent"      # 还没探查过
    PENDING = "pending"    # 有草稿，等你过目
    APPROVED = "approved"  # 已批准并冻结 env.lock
    REJECTED = "rejected"  # 你推翻了，需要重探
