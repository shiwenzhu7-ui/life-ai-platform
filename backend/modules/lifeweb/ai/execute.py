"""V1.1 A5 第 5 步：用户点「确认」之后，真正把草稿写进业务表。

前四步做的事是「模型提议 → 落成一张待确认卡片」；这一步是**唯一一处**
模型产出的内容变成真实数据的地方。四条规则也是为此而写：

1. **写业务表一律复用业务模块自己的 crud**，不在这里手拼 INSERT。
   AI 模块不知道 `tasks` 表的列、不维护 `completed_at` 那套规则、
   也不知道分类怎么校验 —— 这些只写在 `tasks/` 里。AI 只负责「翻译 + 编排」：
   把草稿翻译成 `TaskCreate`，然后调用 `tasks.crud.create(..., commit=False)`。
   好处是：以后待办加了「提醒时间」「子任务」，AI 建的待办自动跟着有，这里不用改。

2. **写和置位同事务（见 `pending.confirm_action`）**：
   「插入待办」和「卡片置 confirmed」一次提交，崩在中间不会留下
   「待办建了、卡片还能再点」的重复写入窗口。

3. **分类在写入的最后一刻再查一次**（`K2`，见 `_confirmed_category_id`）。
   卡片是 30 分钟前生成的，中间分类可能被删了。分类失效不该让整张卡作废，
   落成「未分类」就行 —— 标题和时间还是对的。

4. **用户改过的内容也算不可信输入**：`pending.prepare_payload` 会重新校验，
   这里再补一道「分类必须真的存在」的检查。

接口怎么用这套东西，见 `router.confirm_action` / `router.cancel_action`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from core.errors import AppError, not_found
from core.time import now_utc, to_beijing_iso
from modules.lifeweb.ai import pending
from modules.lifeweb.ai.action_schema import UPDATABLE_EXPENSE_FIELDS
from modules.lifeweb.ai.models import PendingAction
from modules.lifeweb.ai.pending import CODE_BAD_PAYLOAD, CODE_EXPIRED, CODE_REJECTED
from modules.lifeweb.expenses import crud as expenses_crud
from modules.lifeweb.expenses.schema import ExpenseCreate, ExpenseUpdate
from modules.lifeweb.shopping import crud as shopping_crud
from modules.lifeweb.shopping.schema import DEFAULT_LIST_NAME, DEFAULT_UNIT, ItemCreate
from modules.lifeweb.tasks import crud as tasks_crud
from modules.lifeweb.tasks.schema import TaskCreate, TaskUpdate


@dataclass
class Executed:
    """确认之后返回给前端的东西 —— 比 pending.result 多一层「给人看」的包装。

    `created_type` / `created_id` 让前端能直接跳转（「已创建 → 查看这条待办」），
    不用自己解析 result 里的键名。
    """

    action: PendingAction
    created_type: str | None
    created_id: int | None
    result: dict[str, Any]


def confirm(
    db: Session,
    user_id: int,
    action_id: int,
    *,
    payload_override: dict[str, Any] | None = None,
) -> Executed:
    """确认一张卡片：校验 → 调用业务层写入 → 卡片置 confirmed。

    错误语义（前端据此分流，不要一律弹「系统错误」）：

        404  不存在 / 不是我的
        410  已过期（置 expired）
        409  已确认过 / 已取消
        422  用户改过的内容不合法（`ACTION_PAYLOAD_INVALID`）
        400  动作类型不支持（`ACTION_REJECTED`）

    并发：`claim_action` 会对这一行 `SELECT ... FOR UPDATE`，
    同一张卡的两个并发请求，第二个会等到第一个提交后拿到 409。
    **不会建出两条待办。**
    """
    action = pending.claim_action(db, user_id, action_id)

    try:
        payload = pending.prepare_payload(db, action, payload_override)
        spec = pending.ACTION_SPECS.get(action.action_type)
        if spec is None:  # prepare_payload 已经拦过一次，这里只是让类型检查器放心
            raise AppError(CODE_REJECTED, "这个操作类型暂不支持", 400)
        writer = _WRITERS.get(action.action_type)
        if writer is None:
            raise AppError(CODE_REJECTED, "这个操作类型暂不支持", 400)

        confirmed = pending.confirm_action(
            db, action, payload, write=lambda data: writer(db, user_id, data)
        )
    except AppError:
        # 握着的行锁要放掉，卡片保持原状（业务表也还没写）
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    result = confirmed.result or {}
    return Executed(
        action=confirmed,
        created_type=result.get("type"),
        created_id=result.get("id"),
        result=result,
    )


def cancel(db: Session, user_id: int, action_id: int) -> PendingAction:
    """取消一张卡 —— 不碰任何业务表，只是把 status 从 pending 挪到 cancelled。

    同样用行锁 + 状态判断，所以和「确认」互斥：
    确认正在写业务表的那个瞬间，取消会等待；确认完成后取消拿到 409。

    为什么取消要加锁而不是简单 UPDATE：要知道「到底改成没改成」以及
    「没改成是因为过期还是已经确认了」，前端对这两种给出的提示完全不同。
    """
    action = pending._locked(db, user_id, action_id)  # noqa: SLF001 —— 同一模块内的原语

    if action.status != "pending":
        status = action.status
        db.rollback()
        if status == "expired":
            raise AppError(CODE_EXPIRED, "这个操作已过期，请重新发起", 410)
        raise AppError(
            pending.CODE_ALREADY_DONE,
            "这个操作已经处理过了" if status == "confirmed" else "这个操作已取消",
            409,
        )

    if action.expires_at <= now_utc():
        action.status = "expired"
        db.commit()
        raise AppError(CODE_EXPIRED, "这个操作已过期，请重新发起", 410)

    action.status = "cancelled"
    db.commit()
    db.refresh(action)
    return action


# ---------- 各动作类型怎么写进业务表 ----------


def _write_task(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """create_task → 往 `tasks` 插一行。返回记进 `action.result` 的摘要。

    `commit=False` 是这一步唯一改动的业务模块代码：待办的插入不能自己提交，
    要和「卡片置 confirmed」一起提交（理由见模块 docstring 第 2 条）。

    **不要在这里再跑一遍 `CreateTaskPayload(**payload)`**：`prepare_payload` 已经
    校验过了，而且往 payload 里补了一个模型不认识的 `category_id` ——
    那个模型是 `extra="forbid"` 的，再校验一次必然抛 `extra_forbidden`。
    这里只按字段取值。
    """
    task = tasks_crud.create(
        db,
        user_id,
        TaskCreate(
            title=payload["title"],
            category_id=_confirmed_category_id(db, "task", payload.get("category_id")),
            priority=payload.get("priority") or "medium",
            due_at=_as_datetime(payload.get("due_at")),
        ),
        commit=False,
    )
    # 下面这行能读到自增 id，说明 flush 生效了；真正的落盘在 confirm_action 里
    return {"type": "task", "id": task.id}


def _write_update(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """update_task → 改一条已存在的待办。返回摘要。

    **归属校验在这里，不在模型给的 id 上**。`payload["task_id"]` 是模型从
    查询结果里抄来的数字 —— 抄错行、或者干脆编一个，都要在这里拦下来：
    用 `get_for_user(db, user_id, task_id)` 查，查不到就是 404。
    模型看不见别的用户的数据，但「相信模型给的 id」这个习惯本身就是漏洞。

    分类同样二次校验：`_confirmed_category_id` 会确认它在 task 域里还活着。

    改动项来自 `prepare_payload` 已经拼好的完整字段集（原值 + 改动），
    这里只负责翻译成 `TaskUpdate`。`status` 不在里面 ——
    「把这条标成完成」不在本轮的改动范围，它要联动 completed_at，走看板。
    """
    task = tasks_crud.get_for_user(db, user_id, int(payload["task_id"]))
    if task is None:
        raise not_found("要修改的待办不存在")

    updated = tasks_crud.update(
        db,
        task,
        TaskUpdate(
            title=payload.get("title"),
            category_id=_confirmed_category_id(db, "task", payload.get("category_id")),
            priority=payload.get("priority") or "medium",
            due_at=_as_datetime(payload.get("due_at")),
        ),
        # 与「卡片置 confirmed」同事务，理由见模块 docstring 第 2 条
        commit=False,
    )
    return {"type": "task", "id": updated.id}


def _write_expense(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """create_expense → 往 `expenses` 插一行（A8）。

    三个与待办不同的地方：

    1. **`source="ai"`**。这是「这笔账是谁记的」的唯一证据：以后要查
       「AI 记的账有没有出错」，一条 `WHERE source='ai'` 就够了。
       来源由这条代码路径决定，前端在 payload_override 里写 source 也没用
       （白名单里没有这个字段）。
    2. **分类按 expense 域二次校验**，不按 task 域。查不到落 0（未分类）——
       和用户的约定一致：钱数和时间是硬信息，分类可以在账本里补，
       不该因为分类名没对上就把整笔账丢掉。
    3. **`ExpenseCreate` 的 category_id 是 `ge=1`**，所以 0（未分类）
       不能走 `ExpenseCreate` 校验。这里的做法是：先给一个占位合法值再直接
       改对象 `category_id`，见下方注释 —— 与其为了 0 去放宽业务层的 schema，
       不如让「未分类」只在这个模块里成立。
    """
    category_id = _confirmed_category_id(db, "expense", payload.get("category_id"))
    # ExpenseCreate.category_id 要求 ge=1；未分类（0）在建对象前替换成一个
    # 保险值没有意义，直接在模型上用 model_construct 跳过校验更诚实 —— 但那样
    # 会连金额校验一起跳过。所以走「先按合法值构造，再改字段」：
    # 校验用的是 1，落库用的是真实的 category_id（含 0）。
    probe_id = category_id or 1
    create = ExpenseCreate(
        type=payload.get("type") or "expense",
        amount=payload["amount"],
        category_id=probe_id,
        account=payload.get("account") or "现金",
        occurred_at=_as_datetime(payload.get("occurred_at")) or now_utc(),
        note=payload.get("note"),
    )
    create.category_id = category_id

    expense = expenses_crud.create(db, user_id, create, source="ai", commit=False)
    return {"type": "expense", "id": expense.id}


def _write_delete(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """delete_task → 硬删一条待办（真执行 DELETE）。返回摘要。

    走的是和页面删除同一个函数（`tasks.crud.delete`）：
    对话里说「删掉」，等于页面上点删除，没有第二套语义。

    删除之前**先查一次**：卡片可能在屏幕上躺了二十分钟，这期间用户可能
    已经在页面上把它删了 —— 那时 `get_for_user` 查出来是 None，这里回 404
    「要删除的待办不存在，可能已经删过了」，而不是默默成功一次空操作。

    不写 `result` 之外的任何东西：删了就是删了，没有「撤销」按钮。
    卡片本身会保留 `gone` 快照，所以事后还能看到「当时删的是哪一条」。
    """
    task_id = int(payload["task_id"])
    task = tasks_crud.get_for_user(db, user_id, task_id)
    if task is None:
        raise not_found("要删除的待办不存在，可能已经删过了")

    # 与「卡片置 confirmed」同事务：commit=False，由 confirm_action 最后统一提交。
    # 中途任何一步抛错，事务回滚，卡片仍是 pending、待办仍在，用户可以再点一次。
    tasks_crud.delete(db, task, commit=False)
    return {"type": "task", "id": task.id, "deleted": True}


def _write_update_expense(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """update_expense → 改一条已存在的账（A12）。返回摘要。

    三个要点：

    1. **归属校验在这里**：`get_for_user` 用当前用户的 id 查，查不到 404。
       卡片可能躺了二十分钟，这期间用户可能已经在账本里把这一笔删了 ——
       那时这里是 None，回 404 而不是默默成功一次空操作。
    2. **只写真正变了的字段**：`expense_changed_fields` 比过 before 与最终值，
       只把变了的塞进 `ExpenseUpdate`。不这么做的后果是「没改的字段也被写一遍」，
       `updated_at` 会凭空刷新，而对账的人把 updated_at 当「这笔记动过」的证据。
    3. **note 的清空要显式给 None**：`ExpenseUpdate` 靠 `model_fields_set` 区分
       「不动」和「清空」，所以清空时必须把键放进去、值为 None。
    """
    expense_id = int(payload["expense_id"])
    expense = expenses_crud.get_for_user(db, user_id, expense_id)
    if expense is None:
        raise not_found("要修改的账目不存在")

    before = expense_before_dict(expense)
    changed = pending.expense_changed_fields(before, payload)

    updates: dict[str, Any] = {}
    for name in changed:
        if name not in UPDATABLE_EXPENSE_FIELDS and name != "category_id":
            continue
        if name == "category_id":
            updates["category_id"] = _confirmed_category_id(
                db, "expense", payload.get("category_id")
            )
        elif name == "occurred_at":
            updates["occurred_at"] = _as_datetime(payload.get("occurred_at"))
        elif name == "note":
            updates["note"] = payload.get("note") or None
        else:
            updates[name] = payload.get(name)

    if not updates:
        # 什么都没变：仍然算成功（用户点的就是这张卡），但不写库
        return {"type": "expense", "id": expense.id, "changed": []}

    update = ExpenseUpdate(**updates)
    updated = expenses_crud.update(db, expense, update, commit=False)
    return {"type": "expense", "id": updated.id, "changed": changed}


def _write_delete_expense(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """delete_expense → 硬删一笔账（A12）。返回摘要。

    与 `_write_delete`（删待办）完全同一套规矩：先校验归属再删、
    硬删不保留行、`commit=False` 与卡片置位同事务、删了就删了没有撤销按钮。
    """
    expense_id = int(payload["expense_id"])
    expense = expenses_crud.get_for_user(db, user_id, expense_id)
    if expense is None:
        raise not_found("要删除的账目不存在，可能已经删过了")

    expenses_crud.delete(db, expense, commit=False)
    return {"type": "expense", "id": expense.id, "deleted": True}


def expense_before_dict(expense: Any) -> dict[str, Any]:
    """ORM 的 Expense → 与 `pending.expense_before` 同形的快照（用于比对）。

    不直接调 `pending.expense_before`：那个函数要再查一次库，
    而这里手里已经有对象了，多一次 SELECT 只为拼同样的字典不值当。
    """
    from modules.lifeweb.ai.pending import _amount_text  # noqa: SLF001

    return {
        "expense_id": expense.id,
        "amount": _amount_text(expense.amount),
        "type": expense.type,
        "category_id": expense.category_id,
        "account": expense.account,
        "occurred_at": to_beijing_iso(expense.occurred_at),
        "note": expense.note,
    }


def _as_datetime(value: Any):
    """payload 里的 due_at 是 ISO 字符串（model_dump(mode="json") 出来的）。"""
    if not value:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _write_shopping_item(db: Session, user_id: int, payload: dict) -> dict[str, Any]:
    """add_shopping_item → 往某张清单里插 N 条明细（V1.4）。

    三个决定：

    1. **清单不存在就顺手建**，但只在出卡时就被判定为「不存在」的情况下建。
       `list_exists` 是出卡那一刻的快照：卡上写了「将新建清单『周末采购』」，
       用户点确认就该建；卡上写「加到『周末采购』」，那时它一定存在。
       真到了执行时发现被删了（用户在另一个标签页删的），按最新的库状态走 ——
       也就是建一张新的，而不是报错让用户白点。增删都不可逆的动作才需要严格，这里是加数据。

    2. **`source="ai"`**，与 `_write_expense` 同理：这是「这条明细是助手加的」的证据，
       白名单里没有 source，前端 override 改不了它。

    3. **`commit=False`**：业务写入与卡片置位在同一个事务里，`confirm_action` 统一提交。
       中途任何一条明细炸了，整张卡一起回滚，不会出现「加了一半」。
    """
    items = payload.get("items") or []
    if not items:
        # 出卡时 schema 已经要求 ≥1 条，这里是第二道门：一张什么都没加的卡
        # 如果被确认成功，用户会以为加上了
        raise AppError(CODE_BAD_PAYLOAD, "这张卡里没有要加的东西", 422)

    shopping_list = shopping_crud.find_or_create_default_list(
        db, user_id, payload.get("list_name") or DEFAULT_LIST_NAME, commit=False
    )

    item_ids: list[int] = []
    for raw in items:
        create = ItemCreate(
            name=raw["name"],
            quantity=raw.get("quantity") or 1,
            unit=raw.get("unit") or DEFAULT_UNIT,
        )
        item = shopping_crud.create_item(
            db, user_id, shopping_list.id, create, source="ai", commit=False
        )
        item_ids.append(item.id)

    return {
        "type": "shopping_list",
        "id": shopping_list.id,
        "name": shopping_list.name,
        "item_ids": item_ids,
    }


# 动作类型 → 写入函数。每个动作类型都在这里登记一行，`confirm` 不用改
_WRITERS: dict[str, Callable[[Session, int, dict], dict[str, Any]]] = {
    "create_task": _write_task,
    "update_task": _write_update,
    "create_expense": _write_expense,
    "delete_task": _write_delete,
    "update_expense": _write_update_expense,
    "delete_expense": _write_delete_expense,
    "add_shopping_item": _write_shopping_item,
}


def _confirmed_category_id(db_session: Session, scope: str, raw: Any) -> int:
    """执行时的二次解析（A5 计划里叫 K2）：分类失效就落 0，不阻断。

    为什么不直接用草稿里的 category_id：
    草稿是 30 分钟前生成的，用户可能中途把「购物」分类删了，
    或者前端 override 里塞了一个别的域的分类 id（账本分类）。
    这里按 **scope** 再查一次——只认这个域里还活着的分类，其余一律 0。
    """
    return pending.entity_category_id(db_session, scope, raw)


__all__ = ["Executed", "cancel", "confirm", "expense_before_dict"]