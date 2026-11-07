"""AI 会话接口 —— 只做三件事，不含业务规则。

1. 绑定并校验 HTTP 参数（Query / Body / Path）
2. 注入 user_id（V1.0 固定演示用户；V1.2 换成 JWT 解析，本层不用改）
3. 调 crud，把 ORM 对象转成响应 Schema

五个接口分两类：
    会话：新建 / 列表 / 改标题 / 软删
    消息：只读（写消息是 A4 的事 —— 它要调模型、记 token，规则比 CRUD 重得多）
"""

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.deps import get_current_user_id
from core.errors import not_found
from modules.lifeweb.ai import crud
from modules.lifeweb.ai.schema import (
    MessageOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
)
from modules.shared.pagination import Page

router = APIRouter(prefix="/ai/sessions", tags=["AI 助手"])


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


@router.post("", response_model=SessionOut, status_code=201, summary="新建会话")
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


@router.get("", response_model=Page[SessionOut], summary="会话列表")
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


@router.get(
    "/{session_id}/messages", response_model=Page[MessageOut], summary="会话消息"
)
def list_messages(
    session_id: int,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=50, ge=1, le=100, description="每页条数"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[MessageOut]:
    """按发生顺序（id 升序）返回某会话的消息。

    先过 _get_or_404：会话不存在 / 已软删 / 不是我的，一律 404，
    不会返回一个「空列表」让人以为会话还在。
    """
    _get_or_404(db, user_id, session_id)
    rows, total = crud.list_messages(
        db, user_id, session_id, page=page, size=size
    )
    return Page.create([MessageOut.from_model(row) for row in rows], total, page, size)


@router.patch("/{session_id}", response_model=SessionOut, summary="改会话标题")
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


@router.delete("/{session_id}", status_code=204, summary="删除会话")
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
