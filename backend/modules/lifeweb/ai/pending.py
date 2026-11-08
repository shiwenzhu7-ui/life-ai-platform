"""V1.1 A5 第 3 步：把「已校验的草稿」写成一行待确认记录。

它连起前两步：

    tools.py（说明书） → 模型 → action_schema.py（验真伪） → ToolDraft
                                                                  │
                                                    本文件 create_draft（查分类、补可读文本、写库）
                                                                  ↓
                                                       ai_pending_actions.status = pending

**这一步仍然不写任何业务数据**：`tasks` 表一行都不碰。模型说「帮你记了一条待办」，
在用户点确认之前，待办列表里不该有任何东西 —— 这是 V1.1 的安全底线。

三条规则：

1. **只有本文件碰 `ai_pending_actions`**（和 `models/pending_action.py` 一一对应）。
   别的模块要建草稿、要改状态，都调这里的函数。
2. **写操作只在 pending → 别的状态 这一条路上生效**：`UPDATE ... WHERE status='pending'`
   的影响行数就是判据，两个人同时点确认只有一个能成功（乐观锁，不加行锁）。
3. **越权一律 404**：别人的动作查不到，和 tasks / sessions 的做法一致。

事务边界：`create_draft` 只 `flush` 不 `commit` —— 它被 `service.chat` 在同一事务里
调用，要和「assistant 消息 + 更新会话计数」一起提交或一起回滚。
状态流转（确认 / 取消 / 置过期）是单表一条 UPDATE，在本文件 commit。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from core.errors import AppError, not_found
from core.time import BEIJING, now_beijing, now_utc, to_utc_naive
from modules.lifeweb.ai.action_schema import ToolDraft
from modules.lifeweb.ai.models import ChatSession, PendingAction
from modules.shared.categories.model import Category

logger = logging.getLogger(__name__)

# 卡片有效期（分钟）。超过这个时间没点确认，卡片作废，用户得重新发起 ——
# 理由是草稿里的「明天下午三点」是相对发起那一刻说的，太久以后点确认会建成一个过期待办。
DEFAULT_TTL_MINUTES = 30

# 未分类的哨兵值，与 tasks / expenses 表的 category_id 默认值一致（0 = 未分类，不是 NULL）
UNCATEGORIZED_ID = 0

# 前端预览用的可读文本。数据库里存 UTC，给人看的必须是北京时间
_TIME_FMT = "%Y-%m-%d %H:%M"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 一周以内才显示「（周X）」 —— 三个月后的日期写个星期几，反而没人看得懂
_WEEKDAY_WINDOW_DAYS = 7

# priority 枚举值 → 中文。只做展示，取值校验已经在 CreateTaskPayload 做过
PRIORITY_TEXT = {"low": "低", "medium": "中", "high": "高"}


# ---------- 分类名 → id ----------


def resolve_category_id(
    db: Session, action_type: str, category_name: str | None
) -> int:
    """把模型给的中文分类名换成 categories.id；查不到返回 0（未分类）。

    **查不到不报错，这是有意的**：模型可能编一个「娱乐」出来，也可能用户刚把
    「购物」分类删了。这时标题、时间都是对的，只因为分类没匹配上就让整张卡片作废，
    用户体验很差。落 0 之后卡片上会显示「分类：无」，用户自己改一下就行。

    过滤条件与 `crud.system_categories`（提示词里给模型看的分类清单）完全一致，
    否则会出现「模型看得见、这里却查不到」的诡异情况：
    is_system=1（只认系统预置）、owner_user_id=0（非用户自建）、parent_id=0（只认一级）、未软删。

    scope 必须带：`categories` 里同名分类存在于不同域 ——「购物」既是 task 分类
    也是 expense 分类，只按 name 查会随机命中一行，把待办挂到账本分类上。
    """
    if not category_name:
        return UNCATEGORIZED_ID

    scope = _SCOPE_BY_ACTION.get(action_type)
    if scope is None:
        # 新增动作类型却忘了在这里登记：记日志，别静默落 0
        logger.warning("未知动作类型 %s，分类按未分类处理", action_type)
        return UNCATEGORIZED_ID

    category_id = db.scalar(
        select(Category.id)
        .where(
            Category.scope == scope,
            Category.name == category_name.strip(),
            Category.is_system == 1,
            Category.owner_user_id == 0,
            Category.parent_id == 0,
            Category.deleted_at.is_(None),
        )
        .order_by(Category.sort_order, Category.id)
        .limit(1)
    )
    if category_id is None:
        logger.info("分类名 %r 在 %s 域里没匹配到，按未分类处理", category_name, scope)
        return UNCATEGORIZED_ID
    return int(category_id)


# 动作类型 → categories.scope。现在只有 create_task；
# 加 create_expense 时这里加一行，resolve_category_id 不用改
_SCOPE_BY_ACTION: dict[str, str] = {"create_task": "task"}


# ---------- 可读文本 ----------


def format_due_text(iso: str | None) -> str | None:
    """ISO 时间字符串 → 「2026-11-09 15:00（周一）」。

    输入是 `CreateTaskPayload.model_dump(mode="json")` 里的字符串，**带 +08:00 偏移**。
    先转成北京时间再格式化，不用裸字符串切片：模型偶尔给 `+00:00` 或 `Z`，
    直接切前 16 个字符会把它当本地时间显示，差 8 小时。
    """
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        # 走到这里说明 action_schema 放过了不该放的东西，记日志但不让它炸掉整张卡
        logger.warning("时间字符串无法解析：%r", iso)
        return None

    local = parsed.astimezone(BEIJING)
    text = local.strftime(_TIME_FMT)
    if abs((local - now_beijing()).days) <= _WEEKDAY_WINDOW_DAYS:
        text += f"（{_WEEKDAYS[local.weekday()]}）"
    return text


def build_preview(action_type: str, payload: dict) -> dict:
    """补上前端卡片直接显示用的字段，**返回新字典，不改入参**。

    补三个：

        due_at_text    给人看的北京时间（含星期）
        priority_text  低 / 中 / 高
        category_id    分类 id（由 create_draft 先查好塞进来）

    为什么把「算好的文本」存进 payload，而不是让前端现算：

    1. 前端不用做时区换算 —— 浏览器时区和用户预期不一定一致；
    2. **三天后再看这张卡，显示的仍是当时那句话的意思**。前端现算的话，
       时间一过「明天」就变了，用户会以为自己记的是后天。
    """
    preview = dict(payload)
    preview["due_at_text"] = format_due_text(payload.get("due_at"))
    priority = payload.get("priority")
    preview["priority_text"] = PRIORITY_TEXT.get(priority, priority)
    if "category_id" not in preview:
        preview["category_id"] = UNCATEGORIZED_ID
    return preview


# ---------- 创建 ----------


def create_draft(
    db: Session,
    *,
    user_id: int,
    session: ChatSession,
    message_id: int,
    draft: ToolDraft,
) -> PendingAction:
    """把一份 ToolDraft 写成 `ai_pending_actions` 的一行 pending。**flush 不 commit**。

    `message_id` 是这条草稿出自哪句 assistant 回复：卡片上要能回答
    「你凭什么让我建这条待办」，所以这列非空。

    `idempotency_key` 由**服务端**生成，不取前端的值（models/pending_action.py:50 那句
    注释是文档旧稿）：创建草稿这个动作本身不需要幂等 —— 用户重复发一句话，
    本来就该得到两张卡；真正需要幂等的是「确认」那一下，键在前端请求头里（第 5 步）。
    """
    payload = dict(draft.payload)
    payload["category_id"] = resolve_category_id(db, draft.action_type, payload.get("category_name"))

    action = PendingAction(
        user_id=user_id,
        session_id=session.id,
        message_id=message_id,
        action_type=draft.action_type,
        payload=build_preview(draft.action_type, payload),
        status="pending",
        idempotency_key=str(uuid.uuid4()),
        expires_at=to_utc_naive(now_beijing() + timedelta(minutes=DEFAULT_TTL_MINUTES)),
    )
    db.add(action)
    db.flush()  # 拿到自增 id 就够；提交由 service 统一做
    return action


# ---------- 读取 ----------


def _get_owned(db: Session, user_id: int, action_id: int) -> PendingAction:
    """按 id 取本人名下的动作；别人的 / 不存在的统一 404。

    **不按 status 过滤**：已确认的动作也要能读到（前端刷新页面时卡片该变成
    「已创建」而不是消失）。状态判断交给调用方。
    """
    action = db.scalar(
        select(PendingAction).where(
            PendingAction.id == action_id,
            PendingAction.user_id == user_id,
        )
    )
    if action is None:
        raise not_found("待确认操作不存在")
    return action


def get_pending(db: Session, user_id: int, action_id: int) -> PendingAction:
    """取一条「还能被确认」的动作；过期了就顺手置 expired 并抛 410。

    **过期是读时判断，没有定时任务**。A5 不引入后台调度，代价是库里会攒着
    一些 status 还是 pending、其实早该 expired 的行 —— 不影响正确性（过期的永远
    确认不了），只是在 `idx_pending_user_status` 上多占点位置。真到了几十万行再说。

    为什么是 410 而不是 404：这条**确实存在过**，只是来晚了。前端据此提示
    「这个操作已过期，请重新发起」，比「不存在」有用。
    """
    action = _get_owned(db, user_id, action_id)

    if action.status == "pending" and action.expires_at <= now_utc():
        action.status = "expired"
        db.commit()
        raise AppError("ACTION_EXPIRED", "这个操作已过期，请重新发起", 410)

    return action


# ---------- 状态流转 ----------


def _transition(db: Session, action_id: int, from_status: str, **values) -> bool:
    """把 status 从 from_status 改成目标值，返回是否真的改了一行。

    **判据是影响行数，不是先读后改**：`WHERE status='pending'` 让数据库自己做
    比较和交换。两个请求同时确认时，第一个 UPDATE 影响 1 行、第二个影响 0 行，
    第二个立刻知道自己来晚了 —— 不需要 SELECT ... FOR UPDATE 加锁。

    不加锁的代价：可能有一个极短的窗口里两条事务都在跑，但 MySQL 的 UPDATE
    会给命中的行加排他锁，第二个必然阻塞到第一个提交，然后重读 status 发现不是
    pending，行数 0。所以不会出现「两条同样的待办」。

    过期为什么要在 WHERE 里再判一次 `expires_at > NOW()`：`get_pending` 的读时
    判断只是「顺手置位」，如果确认请求**绕过** `get_pending` 直接调本函数，
    一张 31 分钟前的卡照样能被确认 —— 状态机就漏了一个洞。所以时间条件写进
    UPDATE 本身：过期即改不动，与有没有人先读过无关。
    """
    extra = []
    if from_status == "pending":
        # 只对「还能被确认/取消」的转换加时间约束
        extra.append(PendingAction.expires_at > func.now())
    result = db.execute(
        update(PendingAction)
        .where(
            PendingAction.id == action_id,
            PendingAction.status == from_status,
            *extra,
        )
        .values(**values)
    )
    db.commit()  # 必须提交后 rowcount 才是真的；也让后面的调用方看到新状态
    return result.rowcount == 1


def mark_confirmed(db: Session, action_id: int, *, result: dict) -> bool:
    """pending → confirmed，记录执行结果（如 `{"task_id": 12}`）。

    返回 False 表示**这次不是它改的**：已被别人确认、或已取消、或已过期。
    调用方据此决定回 409 还是幂等返回上次的结果 —— 本函数不替它决定。
    """
    return _transition(
        db,
        action_id,
        "pending",
        status="confirmed",
        executed_at=now_utc(),
        result=result,
    )


def mark_cancelled(db: Session, action_id: int) -> bool:
    """pending → cancelled。已确认/已过期的不能被取消（返回 False）。"""
    return _transition(db, action_id, "pending", status="cancelled")


__all__ = [
    "DEFAULT_TTL_MINUTES",
    "PRIORITY_TEXT",
    "UNCATEGORIZED_ID",
    "build_preview",
    "create_draft",
    "format_due_text",
    "get_pending",
    "mark_cancelled",
    "mark_confirmed",
    "resolve_category_id",
]