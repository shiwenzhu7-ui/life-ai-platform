"""AI 会话模块的请求 / 响应字段。

本文件只定义「合同」，不碰数据库：进来什么、出去什么，全在这里。
出参的时间转换也在这里一次做完（同 `tasks/schema.py`），
router 和 crud 不各写一遍。

**请求体里故意没有的字段**：

    user_id        服务端从依赖注入拿；允许前端传，就等于允许往别人账户塞会话
    model          由服务端按 .env 决定，前端指定会让「当时用的哪个模型」失去意义
    message_count  服务端维护的冗余计数，前端写了也不算数
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.time import to_beijing_iso

# 新建会话的默认标题。数据库列上也有 server_default="新对话"（models/chat_session.py:33），
# 两处并存是故意的：这里管「走接口」，库默认管「绕过接口直接建行」（比如测试夹具）。
DEFAULT_TITLE = "新对话"

# 必须与 models/chat_message.py 的 CHAT_ROLES 一字不差 —— 模型里那份是数据库 ENUM 的镜像
MessageRole = Literal["user", "assistant", "system", "tool"]


def _clean_title(value: str) -> str:
    """先 strip 再判空 —— min_length 拦不住 '   '（3 个空格长度也是 3）。"""
    value = value.strip()
    if not value:
        raise ValueError("标题不能为空")
    return value


class SessionCreate(BaseModel):
    """新建会话的请求体 —— 只允许传标题。

    不传就用「新对话」；用户的第一句话落库后，标题由后续步骤自动改写。
    """

    title: str = Field(default=DEFAULT_TITLE, min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        return _clean_title(value)


class SessionUpdate(BaseModel):
    """改标题的请求体。

    只有一个字段，所以不做「三态」那套（没传 / 传 null / 传值）：
    title 必填，传 null 直接 422。「清空标题」在业务上没有意义。
    """

    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        return _clean_title(value)


class SessionOut(BaseModel):
    """会话列表的一行 —— 对应左侧边栏的一条。"""

    id: int
    title: str
    model: str | None = Field(default=None, description="本次会话用的模型名，可为空")
    message_count: int = Field(description="消息条数（冗余计数，列表页不必再 COUNT）")
    last_message_at: str | None = Field(
        default=None, description="最后一条消息时间；新建还没说话时为 null"
    )
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_model(cls, session) -> "SessionOut":
        """ORM 对象 → 响应体：naive UTC 一律转成带 +08:00 的字符串。"""
        return cls(
            id=session.id,
            title=session.title,
            model=session.model,
            message_count=session.message_count,
            last_message_at=to_beijing_iso(session.last_message_at),
            created_at=to_beijing_iso(session.created_at),
            updated_at=to_beijing_iso(session.updated_at),
        )


class PendingActionOut(BaseModel):
    """待确认动作 —— 前端据此渲染确认卡片。

    存在即表示「模型想改数据，但还没改」。前端必须渲染卡片等用户点头，
    **此时任务/账本里没有任何新记录**（A5 的安全底线）。

    `status` 在这里出现（而不是只在 `ActionOut` 里）是为了让**刷新页面后的卡片**
    也能显示成「已创建 / 已取消 / 已过期」而不是一堆可点的按钮 ——
    消息列表接口会把它一起带回来（见 `MessageOut.pending_action`）。
    """

    id: int
    action_type: str = Field(description="create_task / create_expense / …")
    status: str = Field(
        default="pending", description="pending / confirmed / cancelled / expired"
    )
    expires_at: str | None = Field(
        default=None, description="过期时间（北京时间 ISO）；过期后点确认返回 410"
    )
    preview: dict[str, Any] = Field(
        default_factory=dict,
        description="给卡片直接显示的内容，已含分类名与可读时间",
    )
    created_id: int | None = Field(
        default=None,
        description="确认后真正建出来的记录 id（从执行结果里取）；"
        "消息列表也带它，所以刷新后「查看待办」链接不会消失",
    )

    @classmethod
    def from_model(cls, action) -> "PendingActionOut":
        return cls(
            id=action.id,
            action_type=action.action_type,
            status=action.status,
            expires_at=to_beijing_iso(action.expires_at),
            preview=action.payload or {},
            created_id=_created_id(action.result),
        )


def _created_id(result: dict[str, Any] | None) -> int | None:
    """从 ai_pending_actions.result 里取 `{"type": "task", "id": 151}` 的 id。

    只认 int：确认那一刻写进去的是我们自己的 execute 模块，
    但如果哪天有人手工改过库，宁可让卡片没有链接，也不要抛异常把整个列表弄挂。
    """
    if not isinstance(result, dict):
        return None
    created = result.get("id")
    return created if isinstance(created, int) else None


class MessageOut(BaseModel):
    """消息列表的一行 —— 一句话，或一次工具往返。

    tool_calls / tool_result 原样透出，不裁剪：
    前端要靠它们画「正在查待办…」这种过程卡片，裁掉就得再改回来。

    `pending_action` 是**这条助手消息产生的那张卡**（A5 第 6 步）。
    没有它的话，刷新页面后前端根本不知道哪条消息曾经有过卡片 ——
    草稿还躺在 `ai_pending_actions` 里，界面上却凭空消失了。
    """

    id: int
    role: MessageRole
    content: str | None = Field(default=None, description="正文；只调工具时可为 null")
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="模型发起的工具调用原始数组"
    )
    tool_result: dict[str, Any] | None = Field(
        default=None, description="role=tool 时工具返回的内容"
    )
    token_in: int = Field(default=0, description="输入 token 数")
    token_out: int = Field(default=0, description="输出 token 数")
    latency_ms: int = Field(default=0, description="本次调用耗时（毫秒）")
    status: str = Field(default="ok", description="ok / failed")
    error_message: str | None = Field(default=None, description="失败原因，最多 500 字")
    created_at: str | None
    pending_action: PendingActionOut | None = Field(
        default=None,
        description="这条助手消息产生的待确认卡片；没有则为 null（A5 第 6 步）",
    )
    references: list["ReferenceOut"] = Field(
        default_factory=list,
        description="这条助手消息产生时用到的知识库引用；从 tool_result 还原（V1.3）",
    )

    @classmethod
    def from_model(cls, message, pending=None) -> "MessageOut":
        """`pending` 由调用方批量查好传进来 —— 让 ORM 对象自己反向查表
        会在列表循环里触发 N+1 查询，而且把查询职责塞进了 schema 层。

        references 是**从落库的 tool_result 里还原**的，不是重新检索一次：
        重新检索会拿到「现在」最相似的段落，可能和当时模型看到的不是同一批，
        引用就对不上了；而且读消息列表不该产生嵌入计算。
        """
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_result=message.tool_result,
            token_in=message.token_in,
            token_out=message.token_out,
            latency_ms=message.latency_ms,
            status=message.status,
            error_message=message.error_message,
            created_at=to_beijing_iso(message.created_at),
            pending_action=(
                PendingActionOut.from_model(pending) if pending is not None else None
            ),
            references=references_from_tool_result(message.tool_result),
        )


class ChatRequest(BaseModel):
    """发一句话的请求体。

    `session_id` 不传 = 新建一个会话（返回里带新 id，前端记住它继续聊）。
    传了就是往那个会话里追加；不属于自己 / 已删除 → 404。
    """

    session_id: int | None = Field(
        default=None, ge=1, description="会话 ID；不传则自动新建一个会话"
    )
    message: str = Field(min_length=1, max_length=2000, description="用户说的话")

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        # 纯空格长度也是 1，min_length 拦不住；strip 之后再判一次
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value


class UsageOut(BaseModel):
    """这次调用的用量 —— 前端可以在气泡下面显示「320 tokens · 0.9s」。"""

    token_in: int = Field(default=0, description="输入 token 数")
    token_out: int = Field(default=0, description="输出 token 数")
    latency_ms: int = Field(default=0, description="调用耗时（毫秒）")
    model: str | None = Field(
        default=None,
        description="实际回答的模型名（以服务端响应为准，可能与请求的 model 不同）",
    )
    cost_estimate: float = Field(
        default=0.0, description="费用估算（元）；未配置单价时恒为 0"
    )


class ActionConfirmRequest(BaseModel):
    """确认卡片时的请求体（A5 第 5 步）。

    `payload_override` 是用户在前端改过的草稿：改了标题、时间、优先级之后点确认，
    改过的内容要随请求带上来。`null` 表示「按卡片上原样执行」。

    **注意它只是「用户说改成什么」，不是「已校验」**：用户改了也可能填个空标题，
    所以 `pending.prepare_payload` 会把它重新过一遍 `CreateTaskPayload`，
    而且只认那几个白名单字段（`user_id`、`status` 之类塞进来会被忽略）。
    """

    payload_override: dict[str, Any] | None = Field(
        default=None, description="用户修改后的草稿；null 表示按原样执行"
    )


class CreatedOut(BaseModel):
    """确认成功后，业务表里新建了什么 —— 前端据此跳转（「查看这条待办」）。"""

    type: str = Field(description="task / expense / shopping_item …")
    id: int


class ActionOut(BaseModel):
    """确认 / 取消之后的卡片状态。

    `status` 只可能是 confirmed（确认成功）或 cancelled（取消成功）。
    **摘要即结果**：`preview` 是用户最终确认的那一版（含他改过的标题），
    前端拿它直接更新卡片，不用再拉一次消息列表。
    """

    action_id: int
    action_type: str
    status: str = Field(description="confirmed / cancelled")
    preview: dict[str, Any] = Field(default_factory=dict, description="最终生效的内容")
    created: CreatedOut | None = Field(
        default=None, description="新建的业务记录；取消时恒为 null"
    )
    created_id: int | None = Field(
        default=None,
        description="与 `created.id` 同值，拍平一层方便前端直接塞回卡片",
    )
    executed_at: str | None = Field(default=None, description="执行时间（北京时间 ISO）")


class ReferenceOut(BaseModel):
    """一条知识库引用：答案里的这一句出自哪份文件的哪一段。

    只在模型这一轮**真的调了检索**且命中时才出现在 `references` 里；
    普通聊天和查待办/查账都不会有它。前端据此决定要不要画引用区。
    """

    document_id: int = Field(description="文档 id，可用来调 /knowledge/documents/{id}")
    chunk_id: int = Field(description="切块 id；同一份文件的多段用它区分")
    filename: str = Field(description="文件名，原是用户上传时的名字")
    page_no: int | None = Field(default=None, description="页码；DOCX/TXT 没有页概念，为 null")
    chunk_index: int | None = Field(default=None, description="这份文件里的第几段，从 0 开始")
    snippet: str = Field(description="命中原文的前 120 字，用于卡片预览")
    score: float | None = Field(default=None, description="余弦相似度，越大越相关（调试用）")


def references_from_tool_result(tool_result: Any) -> list[ReferenceOut]:
    """从落库的 `tool_result` 里还原引用列表。

    键名 `references` 与 `service.REFERENCES_KEY` 必须一致；这里不 import
    service，是因为 service 反过来 import schema，会成环。

    对脏数据一律宽容：不是 dict、键不存在、某一条字段缺失 → 跳过那一条。
    读历史消息是只读路径，宁可少画一张卡，也不能因为一条坏数据让整个
    会话打不开。
    """
    if not isinstance(tool_result, dict):
        return []
    raw = tool_result.get("references")
    if not isinstance(raw, list):
        return []
    items: list[ReferenceOut] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            items.append(ReferenceOut(**entry))
        except (TypeError, ValueError):
            continue
    return items


class ChatResponse(BaseModel):
    """一问一答的响应。

    `message_id` 是**助手消息**的 id：前端要重试/引用时用它，
    不用再拉一次消息列表。
    """

    session_id: int
    message_id: int
    reply: str = Field(description="模型回复正文；只调工具时可能为空串（A5 起）")
    pending_action: PendingActionOut | None = Field(
        default=None,
        description="不为空表示有动作待用户确认；此时业务数据尚未写入",
    )
    references: list[ReferenceOut] = Field(
        default_factory=list,
        description="知识库引用来源；只有检索命中时非空，普通聊天为空数组（V1.3）",
    )
    usage: UsageOut
