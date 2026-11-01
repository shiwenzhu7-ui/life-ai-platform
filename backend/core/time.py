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
