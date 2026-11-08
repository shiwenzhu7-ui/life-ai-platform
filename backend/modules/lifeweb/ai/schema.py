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


class MessageOut(BaseModel):
    """消息列表的一行 —— 一句话，或一次工具往返。

    tool_calls / tool_result 原样透出，不裁剪：
    前端要靠它们画「正在查待办…」这种过程卡片，裁掉就得再改回来。
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

    @classmethod
    def from_model(cls, message) -> "MessageOut":
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


class PendingActionOut(BaseModel):
    """待确认动作 —— 前端据此渲染确认卡片。

    存在即表示「模型想改数据，但还没改」。前端必须渲染卡片等用户点头，
    **此时任务/账本里没有任何新记录**（A5 的安全底线）。
    """

    id: int
    action_type: str = Field(description="create_task / create_expense / …")
    expires_at: str = Field(description="过期时间（北京时间 ISO）；过期后点确认返回 410")
    preview: dict[str, Any] = Field(
        default_factory=dict,
        description="给卡片直接显示的内容，已含分类名与可读时间",
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
    executed_at: str | None = Field(default=None, description="执行时间（北京时间 ISO）")


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
    usage: UsageOut
