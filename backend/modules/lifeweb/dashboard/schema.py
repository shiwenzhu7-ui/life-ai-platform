"""首页仪表盘的响应形状 —— 只定义「长什么样」，取数在 queries.py。

两条字段类型约定（与全站一致）：
  金额一律 str：两位小数，来自 modules/shared/money.py；前端直接显示，不参与计算
  时间一律带时区偏移的 ISO 字符串：来自 core/time.py 的 to_beijing_iso()

形状见 docs/接口设计.md §7；「逾期与今日到期」的口径见 §7 的说明。
"""

from pydantic import BaseModel

from modules.lifeweb.expenses.schema import DayTotal


class TaskStats(BaseModel):
    """待办卡片的六个数字。

    前四个是「按状态计数」，后两个是「按时间计数」——
    它们不互斥：一条今天到期且已逾期的待办，会同时进 todo 和 overdue。
    """

    todo: int
    doing: int
    done: int
    cancelled: int
    overdue: int
    today_due: int


class ExpenseStats(BaseModel):
    """消费卡片的三个金额，全是两位小数的字符串。"""

    current_month_total: str
    last_month_total: str
    today_total: str


class FocusTask(BaseModel):
    """「逾期与今日到期」列表的一条。

    `is_overdue` 由服务器时间判断后下发，前端只负责标红 ——
    不让浏览器自己拿 due_at 和本地时间比，否则手机时间不准就会标错。
    """

    id: int
    title: str
    due_at: str | None
    status: str
    is_overdue: bool


class DashboardOverview(BaseModel):
    """GET /dashboard/overview 的响应体。

    recent_7d 复用消费模块的 DayTotal：形状一样（{date, expense}），
    复用才能保证「消费页补零、首页不补零」这种偏差不会出现。
    """

    tasks: TaskStats
    expense: ExpenseStats
    recent_7d: list[DayTotal]
    focus_tasks: list[FocusTask]
