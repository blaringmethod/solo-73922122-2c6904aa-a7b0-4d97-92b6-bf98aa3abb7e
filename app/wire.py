"""API 出参的大整数序列化。

问题背景：
- 单格计数（人数/检出数）限制在有符号 64 位整数范围内；
- 但一次发布覆盖多个调查格时，检出"合计"可能超过 2^63-1，
  甚至超过 JavaScript 的安全整数上限 Number.MAX_SAFE_INTEGER = 2^53-1。
- JSON 数字经 JS JSON.parse 解析时，超出安全整数范围的值会丢失精度，
  导致内部台与公开页显示错误的计数。

约定（字段名完全保留，仅值的 JSON 类型可能变化）：
- 安全整数范围内的计数仍以 JSON number 返回；
- 超出安全整数范围的计数以"十进制字符串"返回，前端按字符串原样精确显示。
"""

from typing import Any

JS_SAFE_MAX = 2**53 - 1
JS_SAFE_MIN = -(2**53 - 1)

# 单格计数（人数、检出数）的有符号 64 位上限
INT64_MAX = 2**63 - 1


def to_wire(obj: Any) -> Any:
    """递归转换出参：把超出 JS 安全整数范围的 int 转为十进制字符串。

    bool 是 int 的子类，须先行排除，避免 True/False 被转成字符串。
    dict 的键保持原样（本应用键均为字符串），仅转换值。
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        if obj > JS_SAFE_MAX or obj < JS_SAFE_MIN:
            return str(obj)
        return obj
    if isinstance(obj, dict):
        return {k: to_wire(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_wire(v) for v in obj]
    return obj
