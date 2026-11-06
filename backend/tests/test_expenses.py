"""消费模块（记一笔 / 列表 / 详情 / 改 / 删 / 月度汇总）。

这里的重点和待办不同：
- 金额是 `DECIMAL`，接口进出都是**字符串**（不经过 float）；
- 收支共用一张表，所以 `type` 与 `category_id` 必须成对合法；
- 汇总有「本月」「最近 7 天」两个不同的时间口径。
"""

from datetime import datetime

import pytest

from conftest import API, make_expense, new_session


def utc(y, m, d, hour=9, minute=0):
    """北京时间 hour:minute → 直接入库的 naive UTC。"""
    return datetime(y, m, d, hour, minute) - _EIGHT_HOURS


from datetime import timedelta  # noqa: E402

_EIGHT_HOURS = timedelta(hours=8)


# ---------- 记一笔 ----------


def test_create_expense_defaults_and_string_amount(client):
    res = client.post(
        f"{API}/expenses",
        json={
            "amount": "1280.5",
            "category_id": 1,
            "occurred_at": "2026-11-01T12:00:00+08:00",
        },
    )

    assert res.status_code == 201
    body = res.json()
    assert body["type"] == "expense"
    assert body["account"] == "现金"
    assert body["source"] == "manual"
    assert body["amount"] == "1280.50", "金额必须补到两位小数再返回"
    assert body["category_name"] == "餐饮"
    assert body["occurred_at"] == "2026-11-01T12:00:00+08:00"


def test_create_expense_rejects_three_decimals(client):
    res = client.post(
        f"{API}/expenses",
        json={"amount": "12.345", "category_id": 1, "occurred_at": "2026-11-01T12:00:00+08:00"},
    )

    assert res.status_code == 422


@pytest.mark.parametrize("amount", ["0", "-1", "10000000"])
def test_create_expense_rejects_out_of_range_amount(client, amount):
    res = client.post(
        f"{API}/expenses",
        json={"amount": amount, "category_id": 1, "occurred_at": "2026-11-01T12:00:00+08:00"},
    )

    assert res.status_code == 422


def test_create_expense_rejects_category_from_other_domain(client):
    """收入不能用支出分类：id=1 是「餐饮」，属于 expense 域。"""
    res = client.post(
        f"{API}/expenses",
        json={
            "type": "income",
            "amount": "100",
            "category_id": 1,
            "occurred_at": "2026-11-01T12:00:00+08:00",
        },
    )

    assert res.status_code == 422
    assert res.json()["message"] == "分类不存在"


def test_create_expense_rejects_far_future_time(client):
    res = client.post(
        f"{API}/expenses",
        json={
            "amount": "10",
            "category_id": 1,
            "occurred_at": "2099-01-01T00:00:00+08:00",
        },
    )

    assert res.status_code == 422
    assert "发生时间不能晚于当前时间" in res.json()["message"]


def test_create_expense_cleans_tags(client):
    res = client.post(
        f"{API}/expenses",
        json={
            "amount": "10",
            "category_id": 1,
            "occurred_at": "2026-11-01T12:00:00+08:00",
            "tags": [" 聚餐 ", "聚餐", "", "   "],
            "note": "   ",
        },
    )

    body = res.json()
    assert body["tags"] == ["聚餐"]
    assert body["note"] is None, "全空白备注要归一成 null"


def test_create_expense_rejects_too_many_tags(client):
    res = client.post(
        f"{API}/expenses",
        json={
            "amount": "10",
            "category_id": 1,
            "occurred_at": "2026-11-01T12:00:00+08:00",
            "tags": [f"tag{i}" for i in range(11)],
        },
    )

    assert res.status_code == 422


# ---------- 列表 ----------


def test_list_expenses_sorted_by_time_desc(client, db):
    make_expense(db, amount="10.00", occurred_at=utc(2026, 11, 1))
    make_expense(db, amount="20.00", occurred_at=utc(2026, 11, 3))
    make_expense(db, amount="30.00", occurred_at=utc(2026, 11, 2))

    body = client.get(f"{API}/expenses").json()

    assert [item["amount"] for item in body["items"]] == ["20.00", "30.00", "10.00"]


def test_list_expenses_filters_by_type_and_keyword(client, db):
    make_expense(db, amount="10.00", note="午饭", occurred_at=utc(2026, 11, 1))
    make_expense(
        db, type_="income", amount="8000.00", category_id=10, note="工资", occurred_at=utc(2026, 11, 2)
    )

    incomes = client.get(f"{API}/expenses", params={"type": "income"}).json()
    keyword = client.get(f"{API}/expenses", params={"keyword": "午饭"}).json()

    assert incomes["total"] == 1
    assert incomes["items"][0]["category_name"] == "工资"
    assert keyword["total"] == 1


def test_list_expenses_rejects_reversed_range(client):
    res = client.get(
        f"{API}/expenses", params={"date_from": "2026-11-30", "date_to": "2026-11-01"}
    )

    assert res.status_code == 422
    assert res.json()["message"] == "date_from 不能晚于 date_to"


def test_list_expenses_hides_soft_deleted(client, db):
    row = make_expense(db, amount="10.00", occurred_at=utc(2026, 11, 1))
    client.delete(f"{API}/expenses/{row.id}")

    assert client.get(f"{API}/expenses").json()["total"] == 0


# ---------- 详情 ----------


def test_get_expense_detail_and_404(client, db):
    row = make_expense(db, amount="12.50", occurred_at=utc(2026, 11, 1))

    ok = client.get(f"{API}/expenses/{row.id}")
    missing = client.get(f"{API}/expenses/999999")

    assert ok.status_code == 200
    assert ok.json()["amount"] == "12.50"
    assert missing.status_code == 404
    assert missing.json()["message"] == "收支记录不存在"


def test_get_expense_of_another_user_is_404(client, db):
    row = make_expense(db, user_id=999, occurred_at=utc(2026, 11, 1))

    assert client.get(f"{API}/expenses/{row.id}").status_code == 404


# ---------- 改一笔 ----------


def test_patch_normalizes_amount_and_keeps_others(client, db):
    row = make_expense(db, amount="28.50", note="午饭", occurred_at=utc(2026, 11, 1))

    res = client.patch(f"{API}/expenses/{row.id}", json={"amount": "28.5"})

    body = res.json()
    assert body["amount"] == "28.50"
    assert body["note"] == "午饭"


def test_patch_can_clear_note(client, db):
    row = make_expense(db, note="要清掉", occurred_at=utc(2026, 11, 1))

    assert client.patch(f"{API}/expenses/{row.id}", json={"note": None}).json()["note"] is None


def test_patch_rejects_null_category(client, db):
    row = make_expense(db, occurred_at=utc(2026, 11, 1))

    res = client.patch(f"{API}/expenses/{row.id}", json={"category_id": None})

    assert res.status_code == 422
    assert "category_id 不能为 null" in res.json()["detail"]["errors"][0]["msg"]


def test_patch_rejects_empty_body(client, db):
    row = make_expense(db, occurred_at=utc(2026, 11, 1))

    res = client.patch(f"{API}/expenses/{row.id}", json={})

    assert res.status_code == 422


def test_patch_validates_merged_type_and_category(client, db):
    """只改 type 不改分类：旧分类（餐饮=支出域）与新 type（收入）拼起来非法，必须 422。"""
    row = make_expense(db, type_="expense", category_id=1, occurred_at=utc(2026, 11, 1))

    res = client.patch(f"{API}/expenses/{row.id}", json={"type": "income"})

    assert res.status_code == 422
    assert res.json()["message"] == "分类不存在"


def test_patch_type_and_category_together_succeeds(client, db):
    row = make_expense(db, type_="expense", category_id=1, occurred_at=utc(2026, 11, 1))

    res = client.patch(
        f"{API}/expenses/{row.id}", json={"type": "income", "category_id": 10}
    )

    body = res.json()
    assert (body["type"], body["category_id"], body["category_name"]) == (
        "income",
        10,
        "工资",
    )


# ---------- 删一笔 ----------


def test_delete_expense_is_soft(client, db):
    row = make_expense(db, occurred_at=utc(2026, 11, 1))

    res = client.delete(f"{API}/expenses/{row.id}")

    assert res.status_code == 204
    assert client.get(f"{API}/expenses/{row.id}").status_code == 404

    with new_session() as session:
        from modules.lifeweb.expenses.model import Expense

        kept = session.get(Expense, row.id)
        assert kept is not None and kept.deleted_at is not None


# ---------- 月度汇总 ----------


def test_summary_totals_and_category_share(client, db):
    make_expense(db, amount="100.00", category_id=1, occurred_at=utc(2026, 11, 1, 0, 30))
    make_expense(db, amount="200.00", category_id=2, occurred_at=utc(2026, 11, 3))
    make_expense(
        db, type_="income", amount="8000.00", category_id=10, occurred_at=utc(2026, 11, 2)
    )
    # 上个月的，不该被算进 11 月
    make_expense(db, amount="50.00", category_id=1, occurred_at=utc(2026, 10, 31, 23, 30))

    body = client.get(f"{API}/expenses/summary", params={"month": "2026-11"}).json()

    assert body["period"] == "2026-11"
    assert body["expense_total"] == "300.00"
    assert body["income_total"] == "8000.00"
    assert body["balance"] == "7700.00"

    shares = {item["category_name"]: item for item in body["by_category"]}
    assert set(shares) == {"餐饮", "交通"}, "收入分类不该出现在支出占比里"
    assert shares["交通"]["total"] == "200.00"
    assert shares["交通"]["ratio"] == pytest.approx(0.667, abs=5e-4)
    assert sum(item["ratio"] for item in body["by_category"]) == pytest.approx(1.0, abs=1e-3)


def test_summary_recent_7d_is_always_seven_points(client, db):
    body = client.get(f"{API}/expenses/summary").json()

    days = body["recent_7d"]
    assert len(days) == 7
    assert all(item["expense"] == "0.00" for item in days), "没有数据也要补 0"


def test_summary_defaults_to_current_month(client, db):
    from core.time import now_utc

    now_local = now_utc() + timedelta(hours=8)
    make_expense(db, amount="66.00", occurred_at=now_utc() - timedelta(minutes=5))

    body = client.get(f"{API}/expenses/summary").json()

    assert body["period"] == now_local.strftime("%Y-%m")
    assert body["expense_total"] == "66.00"


@pytest.mark.parametrize("month", ["2026-13", "abc", "2026-1", "202611"])
def test_summary_rejects_bad_month(client, month):
    res = client.get(f"{API}/expenses/summary", params={"month": month})

    assert res.status_code == 422
    assert res.json()["message"] == "month 格式应为 YYYY-MM"


def test_summary_excludes_soft_deleted(client, db):
    row = make_expense(db, amount="99.00", occurred_at=utc(2026, 11, 2))
    client.delete(f"{API}/expenses/{row.id}")

    body = client.get(f"{API}/expenses/summary", params={"month": "2026-11"}).json()

    assert body["expense_total"] == "0.00"
