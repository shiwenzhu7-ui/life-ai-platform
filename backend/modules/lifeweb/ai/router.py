"""AI 接口 —— 只做三件事，不含业务规则。

1. 绑定并校验 HTTP 参数（Query / Body / Path / Header）
2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
3. 调 crud / service / execute，把 ORM 对象转成响应 Schema

接口分三类：
    会话：新建 / 列表 / 改标题 / 软删（A3）
    对话：发一句话拿回复（A4/A5）；消息列表是只读（A3）
    待确认卡片：确认写入 / 取消（A5 第 5 步）

两个 router 分开是因为路径前缀不同：`/ai/sessions` 与 `/ai`。
为什么不是一个前缀 `/ai` 下全挂 —— 上一轮踩过：`/ai` + `/{session_id}`
会把 `/ai/sessions` 抢走（405）。路径前缀越具体，越不容易互相吃掉。
"""

from typing import Literal

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.deps import get_current_user_id
from core.errors import AppError, not_found
from core.time import to_beijing_iso
from modules.lifeweb.ai import crud, execute, service
from modules.lifeweb.ai.provider import LLMError, LLMProvider, build_provider
from modules.lifeweb.ai.schema import (
    ActionConfirmRequest,
    ActionOut,
    ChatRequest,
    ChatResponse,
    CreatedOut,
    MessageOut,
    PendingActionOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    UsageOut,
)
from modules.shared.pagination import Page

session_router = APIRouter(prefix="/ai/sessions", tags=["AI 助手"])
chat_router = APIRouter(prefix="/ai", tags=["AI 助手"])
action_router = APIRouter(prefix="/ai/actions", tags=["AI 助手"])

# lifeweb/__init__.py 遍历它挂载；加新 router 只改这一行
routers = (session_router, chat_router, action_router)


def _get_or_404(db: Session, user_id: int, session_id: int):
    """取一个会话，取不到就 404。

    五个接口里有四个要这个判断（新建除外），写成一处：
    以后改「查不到怎么报错」只需要改这里。

    别人的会话也走这条路径 —— 统一 404，不泄露「这个 ID 存在」。
    """
    session = crud.get_session(db, user_id, session_id)
    if session is None:
        raise not_found("会话不存在")
    return session


@session_router.post("", response_model=SessionOut, status_code=201, summary="新建会话")
def create_session(
    payload: SessionCreate | None = Body(default=None),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> SessionOut:
    """新建一个空会话，默认标题「新对话」。

    model 由服务端写入（读 .env），不让前端指定：
    否则「当时用的哪个模型」就变成前端说了算，事后排查失去依据。

    请求体整个不传、传 {}、或只传 title 都可以：
    payload 为 None 时在这里补一个带默认标题的对象。
    """
    data = payload or SessionCreate()
    session = crud.create_session(
        db, user_id, title=data.title, model=settings.llm_model
    )
    return SessionOut.from_model(session)


@session_router.get("", response_model=Page[SessionOut], summary="会话列表")
def list_sessions(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[SessionOut]:
    """我的会话，按最后消息时间倒序；空会话排在最后。

    软删的会话不出现（crud 里已过滤）。
    """
    rows, total = crud.list_sessions(db, user_id, page=page, size=size)
    return Page.create([SessionOut.from_model(row) for row in rows], total, page, size)


@session_router.get(
    "/{session_id}/messages", response_model=Page[MessageOut], summary="会话消息"
)
def list_messages(
    session_id: int,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=50, ge=1, le=100, description="每页条数"),
    order: Literal["asc", "desc"] = Query(
        default="asc",
        description="asc=最早在前（默认）；desc=最新在前，聊天窗口要这个",
    ),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[MessageOut]:
    """返回某会话的消息。

    默认 asc（id 升序，最早在前）—— 兼容 A3 已交付的行为。
    `order=desc` 时返回「最新在前」，page=1 就是最近 size 条：
    聊天窗口不能一打开先给人看三个月前的开场白。

    先过 _get_or_404：会话不存在 / 已软删 / 不是我的，一律 404，
    不会返回一个「空列表」让人以为会话还在。

    卡片也一起带回来（A5 第 6 步）：每条带 `pending_action` 的消息，
    前端就能在刷新后原样画出确认卡；**一次批量查**，不给列表位加 N+1。
    这里**不动过期状态** —— 列表是读路径，不能因为「翻开历史」顺手写库；
    过期的卡照实返回 `status=pending`，用户真去点时由
    `pending.get_pending` 置 expired 并回 410。
    """
    _get_or_404(db, user_id, session_id)
    rows, total = crud.list_messages(
        db, user_id, session_id, page=page, size=size, order=order
    )
    cards = crud.pending_by_message(db, user_id, [row.id for row in rows])
    items = [MessageOut.from_model(row, cards.get(row.id)) for row in rows]
    return Page.create(items, total, page, size)


@session_router.patch("/{session_id}", response_model=SessionOut, summary="改会话标题")
def update_session_title(
    session_id: int,
    payload: SessionUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> SessionOut:
    """只改标题。

    title 必填（空串 / 纯空格 / 超过 120 字都是 422），
    没有「传 null 清空」这条路径 —— 清空标题在业务上没有意义。
    """
    session = _get_or_404(db, user_id, session_id)
    session = crud.update_title(db, session, payload.title)
    return SessionOut.from_model(session)


@session_router.delete("/{session_id}", status_code=204, summary="删除会话")
def delete_session(
    session_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> None:
    """软删除（只写 deleted_at，不真删）。

    三条行为都要保证：
      1. 删完再查列表 → 不出现
      2. 连删两次 → 第二次 404，不是 500
      3. 删别人的 → 404，不泄露「这条存在」

    消息不跟着删：留给 A4/A6 做用量与历史复盘。
    """
    crud.soft_delete_session(db, _get_or_404(db, user_id, session_id))


# ---------- 对话（A4） ----------


def get_provider() -> LLMProvider:
    """模型提供方依赖。

    写成依赖而不是在函数里直接 `build_provider()`，是为了测试能整体替换：
    `app.dependency_overrides[get_provider] = lambda: FakeProvider([...])`，
    业务代码一行不改，测试也不联网。

    没配 key 时 build_provider 抛 LLMError —— 在这里转成 AppError，
    否则会走 500 兜底，前端只看到「服务器内部错误」，不知道是配置问题。
    """
    try:
        return build_provider()
    except LLMError as exc:
        raise AppError(exc.code, exc.message, exc.status_code) from exc


@chat_router.post("/chat", response_model=ChatResponse, summary="发一句话，拿回复")
def chat(
    payload: ChatRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_provider),
) -> ChatResponse:
    """把一句话发给模型，落库并返回回复。

    `session_id` 省略时自动新建会话（标题取这句话的前 30 字）。
    失败不返回 200 + 假回复：模型超时回 504、限流 429、没配 key 503 ——
    前端据此提示「稍后重试」，而不是把错误当成模型的正式回答展示。
    """
    turn = service.chat(
        db,
        user_id,
        session_id=payload.session_id,
        message=payload.message,
        provider=provider,
    )
    assistant = turn.assistant_message
    pending = None
    if turn.pending is not None:
        # 刚建出来的卡一定是 pending，状态由 from_model 带出，两处响应字段一致
        pending = PendingActionOut.from_model(turn.pending)
    if assistant.status == "failed":
        # 这一轮模型想调工具、参数始终不合格：业务上没办成，不能用 200 糊过去。
        # 消息已经落库（刷新能看到 + 失败原因），前端应提示「稍后重试」。
        raise AppError("MALFORMED_TOOL_ARGS", service.MALFORMED_REASON, 502)
    return ChatResponse(
        session_id=turn.session.id,
        message_id=assistant.id,
        reply=assistant.content or "",
        pending_action=pending,
        usage=UsageOut(
            token_in=turn.token_in,
            token_out=turn.token_out,
            latency_ms=turn.latency_ms,
            model=turn.model,
            cost_estimate=turn.cost_estimate,
        ),
    )


# ---------- 待确认卡片（A5 第 5 步） ----------


def _action_out(action, *, created: execute.Executed | None = None) -> ActionOut:
    """PendingAction → 响应体。确认和取消两条路共用，字段口径必须一致。"""
    return ActionOut(
        action_id=action.id,
        action_type=action.action_type,
        status=action.status,
        preview=action.payload or {},
        created=(
            CreatedOut(type=created.created_type, id=created.created_id)
            if created is not None and created.created_type and created.created_id
            else None
        ),
        executed_at=to_beijing_iso(action.executed_at),
    )


@action_router.post(
    "/{action_id}/confirm", response_model=ActionOut, summary="确认卡片，真正写入"
)
def confirm_action(
    action_id: int,
    payload: ActionConfirmRequest | None = Body(default=None),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ActionOut:
    """用户点了「确认」：把卡片上的内容写进待办表，卡片置 confirmed。

    这是整个 AI 模块里**唯一**会让模型产出的内容变成真实数据的地方。

    可以整个不传请求体（等于「按卡片原样执行」），也可以传
    `{"payload_override": {...}}` 带上用户改过的标题/时间/优先级。

    错误码（前端按这个分流，别一律弹「系统错误」）：

        404  这张卡不存在 / 不是我的
        410  `ACTION_EXPIRED`      超过 30 分钟，卡片作废，请重新发起
        409  `ACTION_NOT_PENDING`  已经确认过 / 已取消
        422  `ACTION_PAYLOAD_INVALID`  改过的内容不合格（空标题、时间不带时区）
        400  `ACTION_REJECTED`     动作类型暂不支持

    并发安全：同一张卡被连点两下，第二个请求会等第一个提交完再拿到 409，
    **不会建出两条待办**（行锁 + 状态判断，见 `pending.claim_action`）。
    """
    data = payload or ActionConfirmRequest()
    executed = execute.confirm(
        db,
        user_id,
        action_id,
        payload_override=data.payload_override,
    )
    return _action_out(executed.action, created=executed)


@action_router.post(
    "/{action_id}/cancel", response_model=ActionOut, summary="取消卡片"
)
def cancel_action(
    action_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ActionOut:
    """用户点了「不用了」：卡片置 cancelled，**不改任何业务数据**。

    再把 cancel 两次：第一次 200，第二次 409 `ACTION_NOT_PENDING`。
    """
    action = execute.cancel(db, user_id, action_id)
    return _action_out(action)
