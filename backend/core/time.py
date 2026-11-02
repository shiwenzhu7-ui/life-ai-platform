"""时间转换 —— 只负责「UTC ↔ 北京时间」这一件事。

约定：
  数据库里：naive UTC（不带时区信息，但含义就是 UTC）
  对外输出：带 +08:00 偏移的 ISO8601 字符串，例：2026-10-30T21:56:48+08:00

为什么单独一个文件：全站只有这里允许做时区换算。
业务代码里出现 datetime.now() 或手写 offset，时区 bug 就会散落到各处。
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

# Windows 上没有系统时区库，靠 tzdata 这个 pip 包提供
BEIJING = ZoneInfo("Asia/Shanghai")


def to_beijing_iso(dt: datetime | None) -> str | None:
    """出库：库里读出的 naive UTC → 北京时间的 ISO 字符串。

    三步缺一不可：
      1. replace(tzinfo=UTC)  ← 贴上「我是 UTC」的标签（漏了就白转）
      2. astimezone(BEIJING)  ← 换算到东八区
      3. isoformat()          ← 输出成 "2026-10-30T21:56:48+08:00"
    """
    if dt is None:
        return None
    aware_utc = dt.replace(tzinfo=UTC)
    return aware_utc.astimezone(BEIJING).isoformat()


def to_utc_naive(dt: datetime | None) -> datetime | None:
    """入库：前端传来的时间 → naive UTC。

    两种输入都要能处理：
      带时区   2026-10-31T15:00:00+08:00  → 直接换算成 07:00:00
      不带时区 2026-10-31T15:00:00        → 当作北京时间解释，再换算
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING)
    return dt.astimezone(UTC).replace(tzinfo=None)


def now_utc() -> datetime:
    """当前 UTC 时间（naive），用于写入数据库。"""
    return datetime.now(UTC).replace(tzinfo=None)


def beijing_day_start_utc(day: date) -> datetime:
    """北京时间某一天的 00:00，换算成 naive UTC —— 给「按天筛选」用。

    用户说的「11 月 1 日」指的是北京时间那一整天，
    对应到库里（存 UTC）是 2026-10-31 16:00 到 2026-11-01 16:00。
    直接用 due_at >= '2026-11-01' 会漏掉当天 00:00~08:00 的记录。

    用法是半开区间：>= beijing_day_start_utc(day) 且 < beijing_day_start_utc(day + 1)
    """
    return to_utc_naive(datetime.combine(day, time.min, tzinfo=BEIJING))


def now_beijing() -> datetime:
    """当前北京时间（aware）。

    只用来取「今天」「本月」这类日历概念，**不要**拿它去写库 ——
    入库一律走 now_utc()。分开的理由：一个带时区一个不带，混用会悄悄丢 8 小时。
    """
    return datetime.now(BEIJING)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """月份加减 —— 纯算术，不碰 date/datetime，也不引入 dateutil。

    为什么要它：首页要算「上月支出」，而 1 月的上个月是去年 12 月。
    `month - 1` 在 1 月会得到 0，必须进位到年份；写错就是「上月永远是 0 元」。

    为什么自己算而不用 relativedelta：只关心「年 + 月」两个整数，
    引入第三方库不值当；而且 relativedelta 面对「1 月 31 日减一个月」有一套
    自己的取舍规则，我们根本用不到，记错了反而会误判。

    示例：
        shift_month(2026,  1, -1) -> (2025, 12)
        shift_month(2026, 12,  1) -> (2027,  1)
        shift_month(2026,  3, -15) -> (2024, 12)
    """
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def beijing_month_range(year: int, month: int) -> tuple[datetime, datetime]:
    """北京时间某个月的半开区间 [当月 1 日 00:00, 次月 1 日 00:00) → naive UTC。

    用户说的「11 月」= 北京时间 11-01 00:00 起、12-01 00:00 止（不含），
    对应库里（存 UTC）是 2026-10-31 16:00 到 2026-11-30 16:00。

    为什么强调半开：区间两端都用 >= / <，不用 BETWEEN ——
    BETWEEN 两端都含，会把 12-01 00:00 整点那一笔算进 11 月，
    而它在北京时间已经是 12 月 1 日 08:00 了。
    """
    start = to_utc_naive(datetime(year, month, 1, tzinfo=BEIJING))
    next_year, next_month = shift_month(year, month, 1)
    end = to_utc_naive(datetime(next_year, next_month, 1, tzinfo=BEIJING))
    return start, end
