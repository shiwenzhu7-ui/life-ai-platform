"""时间处理的唯一入口。

约定（见 docs/数据库设计.md §1）：
- 数据库与内部计算：naive UTC
- API 输出：带 +08:00 偏移的 aware datetime
- 自然语言时间解析：以 Asia/Shanghai 为基准

禁止在业务代码里直接 `datetime.now()` 或手写时区换算，一律走本模块。
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

APP_TZ = ZoneInfo(settings.tz)
UTC = timezone.utc


def now_utc() -> datetime:
    """当前 UTC 时间（naive），用于写入数据库。"""
    return datetime.now(UTC).replace(tzinfo=None)


def now_tz() -> datetime:
    """当前带时区的时间，用于 API 响应与 AI 提示词注入。"""
    return datetime.now(APP_TZ)


def to_utc_naive(dt: datetime) -> datetime:
    """任意 datetime → naive UTC。

    naive 输入按 Asia/Shanghai 解释（前端传来的无偏移时间视为本地时间）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return dt.astimezone(UTC).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    """naive UTC → 带本地时区的 aware datetime，供 API 输出。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(APP_TZ)


def today_local() -> str:
    """今天的本地日期，格式 YYYY-MM-DD。"""
    return now_tz().strftime("%Y-%m-%d")


def current_month_local() -> str:
    """当前本地月份，格式 YYYY-MM。"""
    return now_tz().strftime("%Y-%m")


def month_range(period: str) -> tuple[datetime, datetime]:
    """把 YYYY-MM 转成该自然月的 [起, 止) naive UTC 区间。"""
    year, month = (int(x) for x in period.split("-"))
    start_local = datetime(year, month, 1, tzinfo=APP_TZ)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=APP_TZ)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=APP_TZ)
    return to_utc_naive(start_local), to_utc_naive(end_local)
