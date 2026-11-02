"""金额格式化 —— 全站唯一一处把数字变成「两位小数字符串」的地方。

为什么共享：消费模块与首页都要输出金额，两处各写一份 f-string，
将来要改成三位小数或加千分位时必然只改一处，页面上就出现两种精度。

为什么不用 float：0.1 + 0.2 != 0.3，账目上不能接受；Decimal 从入库到出库全程不丢精度。
"""

from decimal import Decimal


def money(value) -> str:
    """聚合结果 / Decimal / 数字 → 两位小数的字符串，例："620" → "620.00"。

    三件事：
      `or 0`     SUM() 没有匹配行时返回 None（不是 0），直接算会得到 "None"
      Decimal    避免 float 的二进制误差
      `:.2f`     "620" 也要输出成 "620.00"，小数位由 DECIMAL(12,2) 语义保证
    """
    return f"{Decimal(value or 0):.2f}"
