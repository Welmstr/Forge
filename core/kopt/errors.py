"""错误类型。

规矩（设计 §4.1 / 07 定律 3）：缺失必须显式。
凡是「算不出来」「没声明」「测不到」，一律抛错或返回显式的缺失标记，
绝不退化成 0、不退化成默认值、更不退化成「通过」。
"""

from __future__ import annotations


class KoptError(Exception):
    """所有 kopt 错误的基类。退出码 2。"""

    exit_code = 2


class SpecError(KoptError):
    """spec.toml 本身有问题（缺字段、字段非法、文件不存在）。"""


class UnsupportedError(KoptError):
    """题目条件没有覆盖到这个情况 —— 例如某个 dtype 没有声明容差。

    这**不是**「通过」，也**不是**「失败」。它是「这个 harness 没有被授权对此下结论」。
    设计 §4.1 规矩 2 / 03_atrex §6：没有可用 Shape ⇒ 显式 unsupported，而不是 passing test。
    """

    exit_code = 3


class GateError(KoptError):
    """人类闸门状态不允许当前操作（未探查、未批准、已冻结等）。"""

    exit_code = 4


class SentinelError(KoptError):
    """变异哨兵未通过 —— 校验器没有接上，整个 harness 必须停。

    设计 §4.3 / 02_AKO4ALL §3：一个永远返回 PASS 的校验器，比没有校验器更危险。
    """

    exit_code = 5


class ProtectedPathError(KoptError):
    """试图写入受保护路径。设计 §8 / 07 定律 4。"""

    exit_code = 6


class IdentityMismatchError(KoptError):
    """测量身份对不上 —— 引用的证据和当前内容不是同一份。设计 §6 / 07 定律 2。"""

    exit_code = 7


class ObjDumpError(KoptError):
    """反汇编没做成：目标文件不存在、不是受支持的目标文件、或者 objdump 不在。

    设计 §4.1 规矩 3（缺失显式）：拉不出汇编时说「拿不到」，
    **绝不返回空字符串**——空的"汇编"看起来就像"编译器什么都没生成"。"""

    exit_code = 8
