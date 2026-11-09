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
真正的状态流转只在 `confirm_action`（第 5 步）里发生，而且**由它一个人提交**：
「写业务表」和「置 confirmed」必须同生共死。

`mark_confirmed` / `mark_cancelled` 是单条 UPDATE 的简化版，留给「不需要连业务写入」
的场景（取消、手工修数据、测试）；确认走业务表的那条路用 `confirm_action`。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from core.errors import AppError, not_found
from core.time import BEIJING, now_beijing, now_utc, to_beijing_iso, to_utc_naive
from modules.lifeweb.ai.action_schema import (
    UPDATABLE_TASK_FIELDS,
    CreateTaskPayload,
    ToolDraft,
    UpdateTaskPayload,
)
from modules.lifeweb.ai.models import ChatSession, PendingAction
from modules.lifeweb.tasks import crud as tasks_crud
from modules.shared.categories.model import Category

logger = logging.getLogger(__name__)

# ---------- 错误码（第 5 步的三个接口都会抛，集中在这里） ----------

# 卡已被确认过 / 已取消 / 已过期：这张卡不能再动了。
# 前端拿到它应当刷新消息列表，卡片会显示成「已创建」而不是报错弹窗。
CODE_ALREADY_DONE = "ACTION_NOT_PENDING"

# 用户改过的草稿没通过校验（空标题、时间不带时区、优先级乱填……）
CODE_BAD_PAYLOAD = "ACTION_PAYLOAD_INVALID"

# 模型当初给的值落到业务表里仍然不合法（比如分类被删了）
CODE_REJECTED = "ACTION_REJECTED"

# 过期沿用第 3 步定的 410
CODE_EXPIRED = "ACTION_EXPIRED"

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

    return _lookup_category_id(db, scope, category_name)


def entity_category_id(db: Session, entity: str, category_id: Any) -> int:
    """业务写入前再确认一次分类可用；不可用就落 0（未分类），流程不中断。

    用途见 `execute._confirmed_category_id`。参数是**用户/模型改完之后的原始值**
    （可能是 int、字符串、None），所以这里自己兜容错，不假定类型。

    为什么不在 K1 抛错：「分类被管理员改名/删掉」不是用户的错，也不影响待办的
    标题和时间。落 0 之后卡片照常建成未分类待办，前端想提示就提示。
    """
    if category_id is None or category_id == "" or category_id == UNCATEGORIZED_ID:
        return UNCATEGORIZED_ID
    try:
        wanted = int(category_id)
    except (TypeError, ValueError):
        logger.info("分类 id %r 不是整数，按未分类处理", category_id)
        return UNCATEGORIZED_ID
    if wanted <= 0:
        return UNCATEGORIZED_ID

    exists = db.scalar(
        select(Category.id)
        .where(
            Category.id == wanted,
            Category.scope == entity,
            Category.deleted_at.is_(None),
        )
        .limit(1)
    )
    if exists is None:
        logger.info("分类 id=%s 在 %s 域里已不存在，按未分类处理", wanted, entity)
        return UNCATEGORIZED_ID
    return wanted


def _lookup_category_id(db: Session, scope: str, category_name: str) -> int:
    """按名字在某个域里查系统分类 id，查不到返回 0。"""
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


# 动作类型 → categories.scope。现在两个动作都落在 task 域；
# 加 create_expense 时这里加一行（`"create_expense": "expense"`），其余函数都不用改
_SCOPE_BY_ACTION: dict[str, str] = {"create_task": "task", "update_task": "task"}

# 动作类型 → 能写进业务表的东西。现在只有一个字段：
#   entity        写到哪个表的什么域（决定分类要按哪个 scope 再校验一次）
#   parse_payload 把（可能被用户改过的）dict 重新验成一棵可信对象
ACTION_SPECS: dict[str, dict[str, Any]] = {
    "create_task": {"entity": "task", "parse_payload": CreateTaskPayload},
    "update_task": {"entity": "task", "parse_payload": UpdateTaskPayload},
}


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

    改待办多一件事：`update_task` 的 payload 里有一份 `before`（改之前的样子），
    那是给用户判断用的 ——「把买牛奶改到明天」这张卡，光看「明天」没法确认
    改的是不是对的那条，必须能看见「现在：10月31日 15:00」。
    所以 before 里也要补同样的可读文本，否则卡片两栏一边中文一边 ISO 字符串。
    """
    preview = dict(payload)
    preview["due_at_text"] = format_due_text(payload.get("due_at"))
    priority = payload.get("priority")
    preview["priority_text"] = PRIORITY_TEXT.get(priority, priority)
    if "category_id" not in preview:
        preview["category_id"] = UNCATEGORIZED_ID

    before = payload.get("before")
    if isinstance(before, dict):
        preview["before"] = {
            **before,
            "due_at_text": format_due_text(before.get("due_at")),
            "priority_text": PRIORITY_TEXT.get(before.get("priority"), before.get("priority")),
        }
    return preview


def task_before(db: Session, user_id: int, task_id: Any) -> dict:
    """取一条待办「改之前的样子」，写进草稿的 before 字段。

    三个用途，缺一个都不行：

    1. **卡片上能证明改的是哪一条**：用户看到「买牛奶 / 现在 10月31日」才敢点确认。
       模型给错 id 时，这一栏会显示成另一条待办的名字 —— 用户一眼就能看出来。
    2. **执行时能算出真正的 diff**：用户可能在卡片上只改了时间，
       标题和分类沿用 before 里的原值，不会被误写成 None。
    3. **找不到就是 404**：`task_id` 越权或不存在，草稿根本不该建出来。

    时间用 `to_beijing_iso` 转成 +08:00 的字符串：卡片的 payload 是一个 JSON 字段，
    存不进 datetime 对象，也不该存 naive UTC（前端拿到会显示成 UTC）。
    """
    task = tasks_crud.get_for_user(db, user_id, task_id)
    if task is None:
        raise not_found("要修改的待办不存在")
    return {
        "task_id": task.id,
        "title": task.title,
        "due_at": to_beijing_iso(task.due_at),
        "priority": task.priority,
        "category_id": task.category_id,
    }


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

    if draft.action_type == "update_task":
        # 改待办要有「改之前」：卡片上能证明改的是哪条，执行时能算 diff。
        # 找不到这条待办（id 不存在 / 是别人的）在这里就 404 ——
        # 不能建一张注定点不动或改错行的卡。
        before = task_before(db, user_id, payload.get("task_id"))
        payload = {
            **before,
            **{key: value for key, value in payload.items() if key in UPDATABLE_TASK_FIELDS},
            "before": before,
        }

    if "category_name" in payload:
        payload["category_id"] = resolve_category_id(
            db, draft.action_type, payload.get("category_name")
        )
    elif draft.action_type == "update_task":
        # 模型没说分类：沿用这条待办现在的分类，不要落成 0（「未分类」）。
        # 上面的 `{**before, ...}` 里已经有 before["category_id"] 了，
        # 但紧随其后的 `if "category_name" in payload` 分支在模型给了分类名时
        # 会覆盖它 —— 那时以新名字查出来的 id 才是对的。走到 else 说明没给名字，
        # 这时把 before 的 category_id 明确写回，别让它依赖字典合并的副作用。
        before_again = payload.get("before") or {}
        payload["category_id"] = before_again.get("category_id") or UNCATEGORIZED_ID

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


def get_pending(
    db: Session, user_id: int, action_id: int, *, locked: bool = False
) -> PendingAction:
    """取一条「还能被确认」的动作；过期了就顺手置 expired 并抛 410。

    **过期是读时判断，没有定时任务**。A5 不引入后台调度，代价是库里会攒着
    一些 status 还是 pending、其实早该 expired 的行 —— 不影响正确性（过期的永远
    确认不了），只是在 `idx_pending_user_status` 上多占点位置。真到了几十万行再说。

    为什么是 410 而不是 404：这条**确实存在过**，只是来晚了。前端据此提示
    「这个操作已过期，请重新发起」，比「不存在」有用。

    若调用方已经握着行锁（`claim_action` / `execute.cancel` 的路径），
    `locked=True` 跳过 `action.status = "expired"` 这一步 —— 那会把锁住的行改成脏状态，
    随后调用方的 commit 会把它写进去，而调用方其实打算 rollback。
    """
    action = _get_owned(db, user_id, action_id)

    if action.status == "pending" and action.expires_at <= now_utc():
        if not locked:
            action.status = "expired"
            db.commit()
        raise AppError(CODE_EXPIRED, "这个操作已过期，请重新发起", 410)

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


# ---------- 第 5 步：认领 + 确认（事务边界最重要的一段） ----------


def _locked(db: Session, user_id: int, action_id: int) -> PendingAction:
    """取本人名下的动作并加行锁（SELECT ... FOR UPDATE），**不提交、不加状态判断**。

    为什么这里要锁而 `_transition` 不用：确认要「先看再写业务表」——
    读草稿、往 tasks 插一行、回来置 status —— 中间隔着一整条 INSERT。
    不用 `WHERE status='pending'` 那种乐观锁的话，两个并发确认会各自看到
    pending、各自插一条待办，最后只有一个 UPDATE 成功，但两条待办已经躺在那儿了。

    换成行锁之后，第二个请求会阻塞在这里，等第一个提交完再拿到行，
    这时 status 已经是 confirmed，直接走 409。**整段窗口靠锁关闭，不靠重试。**

    调用方必须保证锁一直持有到自己的 commit / rollback —— 所以本函数自己绝不提交，
    也不做 `db.rollback()`（一 rollback 锁就没了，前面读的全白读）。
    """
    action = db.scalar(
        select(PendingAction)
        .where(PendingAction.id == action_id, PendingAction.user_id == user_id)
        .with_for_update()
    )
    if action is None:
        raise not_found("待确认操作不存在")
    return action


def _expire_and_raise(db: Session, action: PendingAction) -> None:
    """"卡过期了，置 expired 并抛 410。"""
    action.status = "expired"
    db.commit()
    raise AppError(CODE_EXPIRED, "这个操作已过期，请重新发起", 410)


def prepare_payload(
    db: Session, action: PendingAction, override: dict[str, Any] | None
) -> dict:
    """合成「最终要写进业务表的 payload」并重新校验一遍。

    两个来源：

        原草稿   payload 去掉前端预览用的两个派生字段
                 （due_at_text / priority_text 是 create_draft 补的，
                  不在 CreateTaskPayload 的字段里，带着进去会被 extra="forbid" 拒绝）
        override 用户在前端改过的内容，**只认下面这些字段**：

                 title / due_at / priority / category_name / category_id

                 「认得的字段」这个边界很关键。客户端的 payload_override 里塞一个
                 `user_id: 1` 或 `status: "done"`，如果原样透传下去，
                 就等于让前端决定「写到谁名下」「直接建成已完成」。
                 这里只挑白名单，等于把它们挡在门外 —— 前端传来的东西同样是不可信的输入。

    **category_id 单独处理**：它不在 CreateTaskPayload 里（模型只给分类名，
    不直接说 id），但它是「最终要落库的东西」之一，所以：

        1. 从草稿继承了分类名的，现查一次 id —— 用户可能把「购物」改成了「工作」
        2. override 里显式给了 category_id 的，用它（执行时还会按 scope 再验一次）
        3. 前两步都没有，才沿用草稿里 create_draft 查好的那个 id

    然后不管来源是哪边，**统一过一遍 payload 模型**：用户也可能填个空标题、
    给个不带时区的时间。不能因为「这是用户亲自改的」就免检。
    """
    spec = ACTION_SPECS.get(action.action_type)
    if spec is None:
        # 数据库 ENUM 里有、代码里没登记：属于配置错误，不该发生
        raise AppError(CODE_REJECTED, "这个操作类型暂不支持", 400)

    model = spec["parse_payload"]
    stored = action.payload or {}

    if action.action_type == "update_task":
        data = _prepare_update(model, stored, override)
        data["category_id"] = _category_id_from(
            db,
            action,
            data.get("category_name"),
            override.get("category_id") if override else None,
        )
        return data

    raw = {key: value for key, value in stored.items() if key in model.model_fields}
    if override:
        for key in model.model_fields:
            if key in override:
                raw[key] = override[key]

    try:
        validated = model(**raw)
    except Exception as exc:  # pydantic 的 ValidationError，但别让它走 500
        raise AppError(CODE_BAD_PAYLOAD, f"待办内容不合法：{_short(exc)}", 422) from exc

    data = validated.model_dump(mode="json")
    data["category_id"] = _category_id_from(
        db,
        action,
        data.get("category_name"),
        override.get("category_id") if override else None,
    )
    return data


def _prepare_update(
    model: type, stored: dict, override: dict[str, Any] | None
) -> dict:
    """改待办的 payload 合成：把「改之前」和「改成什么」拼成一份完整字段集。

    与新增的区别在于**空值语义**：新增时 `due_at: None` 就是「没有截止时间」；
    改的时候 `due_at: None` 只表示「这次不改时间」，必须回填成原值 ——
    否则用户说「把标题改一下」，模型没提时间，待办的截止日就被清空了。

    所以这里的规则是：

        before[字段]      打底（原值）
        override 显式给的  覆盖（用户在前端改的）
        都没有            保持原值

    最后整体过一遍 `UpdateTaskPayload`：用户改出来的空标题、坏时间同样不放过。
    """
    before = dict(stored.get("before") or {})
    merged = {name: before.get(name) for name in UPDATABLE_TASK_FIELDS if before.get(name) is not None}

    if override:
        for name in UPDATABLE_TASK_FIELDS:
            if name in override and override[name] is not None:
                merged[name] = override[name]
    else:
        # 没有 override：用草稿里模型真正给出的那些改动项
        for name in UPDATABLE_TASK_FIELDS:
            if stored.get(name) is not None:
                merged[name] = stored[name]

    # 分类名要和「改之前的名字」比一比才知道有没有变。payload 顶层的
    # category_name 可能是从 before 带过来的（模型没说分类时等于原样），
    # 那种情况下不该重查，也就不会把 category_id 带歪。
    if merged.get("category_name") == before.get("category_name"):
        merged.pop("category_name", None)

    merged["task_id"] = stored.get("task_id") or before.get("task_id")
    try:
        validated = model(**merged)
    except Exception as exc:  # pydantic 的 ValidationError，但别让它走 500
        raise AppError(CODE_BAD_PAYLOAD, f"修改内容不合法：{_short(exc)}", 422) from exc

    return validated.model_dump(mode="json")


def _category_id_from(
    db: Session,
    action: PendingAction,
    category_name: str | None,
    override_id: Any,
) -> int:
    """决定最终用哪个分类 id（三种来源，优先级见 prepare_payload 的说明）。

    override 里给了就照单全收 —— 不在这里判断它合不合法，
    因为「合不合法」要按业务域（scope）判断，那是 `entity_category_id` 的活，
    而且要到真正写表那一刻才查得准。
    """
    if override_id is not None:
        try:
            return int(override_id)
        except (TypeError, ValueError):
            return UNCATEGORIZED_ID

    stored = action.payload or {}

    if action.action_type == "update_task":
        return _category_id_for_update(db, action, stored, category_name)

    if category_name is not None and category_name != stored.get("category_name"):
        # 用户把分类名改了：以新名字为准，重新查一次
        scope = _SCOPE_BY_ACTION.get(action.action_type)
        if scope is not None:
            return _lookup_category_id(db, scope, category_name)

    return stored.get("category_id") or UNCATEGORIZED_ID


def _category_id_for_update(
    db: Session, action: PendingAction, stored: dict, category_name: str | None
) -> int:
    """改待办该用哪个分类 id。

    比较基准必须是 **before（这条待办此刻的分类）**，不能是 payload 顶层的
    `category_name` —— 后者在 `create_draft` 里已经被写成「模型这次要改成的名字」，
    拿它来比永远是「没变化」，改了分类也没人重查（这是本函数存在的全部原因）。

    三档：

        名字跟原值不同 → 按新名字重查（用户/模型确实换了分类）
        名字没给       → 沿用 before 的 id
        名字等于原值   → 也沿用 before 的 id（没有理由再查一次）
    """
    before = stored.get("before") if isinstance(stored.get("before"), dict) else {}
    original_name = before.get("category_name")
    scope = _SCOPE_BY_ACTION.get(action.action_type)

    if category_name is not None and category_name != original_name and scope is not None:
        return _lookup_category_id(db, scope, category_name)

    return before.get("category_id") or UNCATEGORIZED_ID


def _short(exc: Exception, limit: int = 200) -> str:
    """把 pydantic 的错误压成一行给人看。"""
    text = " ".join(str(exc).split())
    return text[:limit]


def claim_action(db: Session, user_id: int, action_id: int) -> PendingAction:
    """确认前的第一步：锁住这张卡，确认它现在**还**能被执行。

    顺序很重要 —— 先加锁再判断状态，判断完锁还握在手里：
    `get_pending` 那套「读后判断」放在这里不够，因为读完到写业务表之间
    别人可能已经确认了。锁 + 事务保证这段窗口里没人能插队。

    抛出之前先 `db.rollback()`：锁只在事务里有效，函数要退出了就把它放掉
    （此时业务表一行都还没写，回滚是安全的）。
    """
    action = _locked(db, user_id, action_id)
    if action.status != "pending":
        status = action.status
        db.rollback()
        if status == "expired":
            raise AppError(CODE_EXPIRED, "这个操作已过期，请重新发起", 410)
        raise AppError(
            CODE_ALREADY_DONE,
            "这个操作已经处理过了" if status == "confirmed" else "这个操作已取消",
            409,
        )
    if action.expires_at <= now_utc():
        _expire_and_raise(db, action)
    return action


def confirm_action(
    db: Session,
    action: PendingAction,
    payload: dict,
    *,
    write: Callable[[dict], dict[str, Any]],
) -> PendingAction:
    """执行写入 + 置 confirmed，**同一个事务，一次提交**。

    `write` 是 `execute.py` 传进来的函数：它接收合成好的 payload，往业务表写一行，
    返回要记进 `result` 的摘要（如 `{"task_id": 12}`）。

    为什么一定要写和置位同事务：

        commit 之后才置 confirmed  → 中间崩掉，卡片还是 pending，
                                     用户再点一次 → 第二条待办（重复写入）
        commit 之前置 confirmed    → 业务写失败回滚了，卡片却是 confirmed，
                                     点什么都办不成（丢写入）

    所以：`write()` 不提交 → 这里改 status/executed_at/result → 一次 commit。
    `write()` 抛异常时由调用方 rollback（锁随之释放），卡片回到 pending，什么都没发生。

    顺带把 `payload` 写回 `action.payload` 的**原始字段**（保留预览派生字段），
    这样用户确认时改过的标题、时间，在卡片上刷新后也是改过的那一版 ——
    「卡片上写什么，库里就有什么」。
    """
    summary = write(payload)

    merged = {key: value for key, value in (action.payload or {}).items()}
    merged.update(payload)
    action.payload = build_preview(action.action_type, merged)
    action.status = "confirmed"
    action.executed_at = now_utc()
    action.result = summary
    db.commit()
    return action


__all__ = [
    "ACTION_SPECS",
    "CODE_ALREADY_DONE",
    "CODE_BAD_PAYLOAD",
    "CODE_EXPIRED",
    "CODE_REJECTED",
    "DEFAULT_TTL_MINUTES",
    "PRIORITY_TEXT",
    "UNCATEGORIZED_ID",
    "build_preview",
    "claim_action",
    "confirm_action",
    "create_draft",
    "entity_category_id",
    "format_due_text",
    "get_pending",
    "mark_cancelled",
    "mark_confirmed",
    "prepare_payload",
    "resolve_category_id",
]