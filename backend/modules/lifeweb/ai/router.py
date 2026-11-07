"""AI 接口 —— 只做三件事，不含业务规则。

1. 绑定并校验 HTTP 参数（Query / Body / Path）
2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
3. 调 crud / service，把 ORM 对象转成响应 Schema

接口分两类：
    会话：新建 / 列表 / 改标题 / 软删（A3）
    对话：发一句话拿回复（A4）；消息列表是只读（A3）

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
from modules.lifeweb.ai import crud, service
from modules.lifeweb.ai.provider import LLMError, LLMProvider, build_provider
from modules.lifeweb.ai.schema import (
    ChatRequest,
    ChatResponse,
    MessageOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    UsageOut,
)
from modules.shared.pagination import Page

session_router = APIRouter(prefix="/ai/sessions", tags=["AI 助手"])
chat_router = APIRouter(prefix="/ai", tags=["AI 助手"])

# lifeweb/__init__.py 遍历它挂载；加新 router 只改这一行
routers = (session_router, chat_router)


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
    """
    _get_or_404(db, user_id, session_id)
    rows, total = crud.list_messages(
        db, user_id, session_id, page=page, size=size, order=order
    )
    return Page.create([MessageOut.from_model(row) for row in rows], total, page, size)


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
    return ChatResponse(
        session_id=turn.session.id,
        message_id=turn.assistant_message.id,
        reply=turn.assistant_message.content or "",
        usage=UsageOut(
            token_in=turn.token_in,
            token_out=turn.token_out,
            latency_ms=turn.latency_ms,
            model=turn.model,
            cost_estimate=turn.cost_estimate,
        ),
    )
