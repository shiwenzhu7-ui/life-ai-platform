"""首页总览：一个请求拼起 4 张卡片的数据。

这个接口最容易「看起来对、其实错」的地方：
- 逾期与今日到期**不重叠**（相加才是列表候选数）；
- `focus_tasks` 是**配额制**（逾期最多 3 + 今日最多 2），条数可能少于两个数字之和；
- `recent_7d` 恒 7 个点、末位是今天，与月份无关；
- 三个金额各走一个时间区间，不能合并成一条 SQL。
"""

from datetime import timedelta

from conftest import API, make_expense, make_task
from core.time import beijing_day_start_utc, now_utc


def beijing(offset_days: int = 0, hour: int = 12):
    base = (now_utc() + timedelta(hours=8)).replace(minute=0, second=0, microsecond=0)
    return base.replace(hour=hour) + timedelta(days=offset_days) - timedelta(hours=8)


def due_later_today():
    """今天还没到点的一个时刻（23:59 之前安全），用来验 today_due。"""
    today = now_utc() + timedelta(hours=8)
    tomorrow_start = beijing_day_start_utc(today.date()) + timedelta(days=1)
    candidate = now_utc() + timedelta(minutes=30)
    return min(candidate, tomorrow_start - timedelta(minutes=1))


def test_overview_shape(client):
    body = client.get(f"{API}/dashboard/overview").json()

    assert set(body) == {"tasks", "expense", "recent_7d", "focus_tasks"}
    assert set(body["tasks"]) == {"todo", "doing", "done", "cancelled", "overdue", "today_due"}
    assert set(body["expense"]) == {"current_month_total", "last_month_total", "today_total"}
    assert set(body["recent_7d"][0]) == {"date", "expense"}
    assert body["focus_tasks"] == []


def test_overview_counts_match_data(client, db):
    make_task(db, title="待办 1", due_at=beijing(-2))  # 逾期
    make_task(db, title="待办 2", due_at=beijing(-1))  # 逾期
    make_task(db, title="待办 3", due_at=due_later_today())  # 今日到期
    make_task(db, title="在做", status="doing", due_at=beijing(-3))  # 逾期
    make_task(db, title="已完成", status="done")
    make_task(db, title="已取消", status="cancelled")

    tasks = client.get(f"{API}/dashboard/overview").json()["tasks"]

    assert tasks == {
        "todo": 3,
        "doing": 1,
        "done": 1,
        "cancelled": 1,
        "overdue": 3,
        "today_due": 1,
    }


def test_overview_today_due_does_not_overlap_overdue(client, db):
    """今天的 00:01 已经过了 → 只算逾期，不算今日到期。"""
    make_task(db, title="今天凌晨", due_at=beijing(0, hour=0))

    tasks = client.get(f"{API}/dashboard/overview").json()["tasks"]

    assert tasks["overdue"] == 1
    assert tasks["today_due"] == 0


def test_overview_focus_uses_quota(client, db):
    """逾期 5 条 + 今日 2 条 → 列表只给「逾期 3 + 今日 2」= 5 条，且逾期在前。"""
    for i in range(5):
        make_task(db, title=f"逾期{i}", due_at=beijing(-6 + i))
    make_task(db, title="今日A", due_at=due_later_today())
    make_task(db, title="今日B", due_at=due_later_today())

    body = client.get(f"{API}/dashboard/overview").json()

    focus = body["focus_tasks"]
    assert len(focus) == 5
    assert [item["title"] for item in focus[:3]] == ["逾期0", "逾期1", "逾期2"]
    assert {item["title"] for item in focus[3:]} == {"今日A", "今日B"}
    assert all(item["is_overdue"] is False for item in focus[3:])


def test_overview_focus_does_not_pad_when_one_side_is_empty(client, db):
    for i in range(5):
        make_task(db, title=f"逾期{i}", due_at=beijing(-6 + i))

    focus = client.get(f"{API}/dashboard/overview").json()["focus_tasks"]

    assert len(focus) == 3, "今日没有事时不该拿逾期去补满 5 条"


def test_overview_expense_totals_use_three_windows(client, db):
    today = (now_utc() + timedelta(hours=8)).date()
    this_month = today.replace(day=1)

    make_expense(db, amount="100.00", occurred_at=now_utc() - timedelta(minutes=5))  # 今天
    make_expense(db, amount="20.00", occurred_at=beijing(-2))  # 本月早些时候
    make_expense(
        db,
        amount="50.00",
        occurred_at=(this_month - timedelta(days=1)).replace(day=15) - timedelta(hours=8),
    )  # 上个月

    expense = client.get(f"{API}/dashboard/overview").json()["expense"]

    assert expense["today_total"] == "100.00", "只有 5 分钟前那笔算今天"
    assert expense["current_month_total"] == "120.00", "今天 100 + 前天 20"
    assert expense["last_month_total"] == "50.00"


def test_overview_recent_7d_is_seven_points_ending_today(client, db):
    today = (now_utc() + timedelta(hours=8)).date()
    make_expense(db, amount="42.00", occurred_at=now_utc() - timedelta(minutes=2))

    days = client.get(f"{API}/dashboard/overview").json()["recent_7d"]

    assert len(days) == 7
    assert days[-1]["date"] == today.isoformat()
    assert days[-1]["expense"] == "42.00"
    assert all(item["expense"] == "0.00" for item in days[:-1])


def test_overview_ignores_other_users_data(client, db):
    make_task(db, user_id=999, title="别人的逾期", due_at=beijing(-2))
    make_expense(db, user_id=999, amount="777.00", occurred_at=now_utc() - timedelta(minutes=1))

    body = client.get(f"{API}/dashboard/overview").json()

    assert body["tasks"]["overdue"] == 0
    assert body["expense"]["today_total"] == "0.00"
    assert body["focus_tasks"] == []


def test_overview_ignores_soft_deleted_rows(client, db):
    task = make_task(db, title="删掉的逾期", due_at=beijing(-2))
    client.delete(f"{API}/tasks/{task.id}")

    assert client.get(f"{API}/dashboard/overview").json()["tasks"]["overdue"] == 0
