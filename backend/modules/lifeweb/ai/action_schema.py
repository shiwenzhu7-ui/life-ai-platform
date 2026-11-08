"""V1.1 A5 第 2 步：把模型交上来的工具调用变成「可信任的草稿」。

模型返回的 `arguments` 是**一段字符串**，而且它可能：

    {"title":"去超市",}                          ← 多一个逗号，json.loads 直接抛
    ```json\n{"title":"x"}\n```                 ← 裹了代码块
    []                                           ← 合法 JSON，但不是对象
    {"title":"","due_at":"2026-11-09"}           ← 空标题、时间没时区
    {"title":"开会","due_at":"0206-11-09T15:00"} ← 年份写错
    {"title":"开会","priority":"urgent"}         ← 枚举外的值
    {"name":"开会"}                              ← 字段名都不对

本文件把这些全部挡在门外：**要么得到一份字段已验证的 ToolDraft，要么得到 None**。

三条硬规则：

1. **纯函数**：不接收 db、不写库、不发请求。查分类 id 是第 3 步的事，
   这里只做「结构 + 取值范围」的校验，所以测试跑起来不用起数据库。
2. **出错不抛异常**：模型的错不该让整条消息失败。坏掉的调用丢掉并记 warning，
   正常的 `content` 照样显示给用户。
3. **字段名与 `tools.py` 的声明一一对应**：多一个字段直接拒绝（extra="forbid"），
   否则会存进一个谁也不认识的字段。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modules.lifeweb.tasks.schema import Priority, _clean_title

logger = logging.getLogger(__name__)

# 工具名 → 数据库 ai_pending_actions.action_type 的 ENUM 值。
# 现在两个词恰好一样（create_task），但这层映射必须显式写出来：
# 工具名是给模型看的（贴近用户说法），ENUM 是对齐表结构的。
# 明天加 `record_expense` 工具时，ENUM 仍是 `create_expense`，靠这里翻译。
ACTION_TYPE_BY_TOOL: dict[str, str] = {"create_task": "create_task"}

# 一次回复里最多采纳几份草稿。
# A5 取 1：一句话说两件事时，模型可能一次交两个 call；多卡确认要改响应结构、
# 前端循环渲染、幂等粒度，留到以后。这里只采纳第一个，其余丢弃并记日志。
MAX_DRAFTS_PER_REPLY = 1

# 日志里打印原始 arguments 时截断，模型可能吐几十 KB
_ARGS_LOG_LIMIT = 200

# 年份的合理区间：挡住 0206、9999 这类明显写错的
_MIN_YEAR = 2020
_MAX_YEAR = 2100


class CreateTaskPayload(BaseModel):
    """create_task 的草稿。字段与 `tools.py` 的 parameters 一一对应。"""

    # 模型自己加的字段（tags、remind_before…）一律拒绝：payload 里存不下，
    # 静默丢掉会让用户以为设置生效了
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    due_at: datetime | None = None
    priority: Priority = "medium"
    category_name: str | None = Field(default=None, max_length=32)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        # 与 tasks 的 TaskCreate 共用同一套规则（strip 后非空），不另写一份
        return _clean_title(value)

    @field_validator("due_at")
    @classmethod
    def due_at_sane(cls, value: datetime | None) -> datetime | None:
        """两个必须拦的点。

        1. **必须带时区**：`2026-11-09T15:00:00` 这种 naive 时间，落库前
           `to_utc_naive()` 只能猜它按哪个时区算；猜错就是差 8 小时的待办。
        2. **年份合理**：模型把 `2026` 写成 `0206` 是真实会发生的，
           那个待办从此永远不会逾期提醒。
        """
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间必须带时区，例如 2026-11-09T15:00:00+08:00")
        if not _MIN_YEAR <= value.year <= _MAX_YEAR:
            raise ValueError(f"年份不合理：{value.year}")
        return value


# 工具名 → 校验模型。加工具时在这里加一行，`parse_tool_calls` 不用改
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {"create_task": CreateTaskPayload}


@dataclass(frozen=True)
class ToolDraft:
    """一份通过校验的草稿（还没入库，也还没查分类 id）。"""

    tool_call_id: str
    """模型给这次调用的 id，将来回填成 role=tool 的消息时用得上。"""

    tool_name: str
    """模型申请的工具名，如 create_task。"""

    action_type: str
    """落库用的 ENUM 值，经 ACTION_TYPE_BY_TOOL 翻译。"""

    payload: dict[str, Any]
    """`CreateTaskPayload.model_dump(mode="json")`：
    datetime 已变成 ISO 字符串，能直接存进 JSON 列。"""


def _parse_one(call: dict[str, Any]) -> ToolDraft | None:
    """解析单个 tool_call。任何一步不对都返回 None，不抛异常。"""
    tool_name = call.get("name")
    arguments = call.get("arguments")

    if not tool_name:
        logger.warning("丢弃工具调用：缺少 name | call=%r", call)
        return None

    model = PAYLOAD_MODELS.get(tool_name)
    if model is None:
        # 模型幻觉出一个不存在的工具名（delete_all_tasks 之类）
        logger.warning("丢弃工具调用：未知工具 %s", tool_name)
        return None

    if not isinstance(arguments, str):
        logger.warning("丢弃工具调用：arguments 不是字符串 | tool=%s", tool_name)
        return None

    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "丢弃工具调用：arguments 不是合法 JSON | tool=%s | err=%s | args=%r",
            tool_name,
            exc,
            arguments[:_ARGS_LOG_LIMIT],
        )
        return None

    if not isinstance(data, dict):
        # "[]" / "\"abc\"" / "123" 都是合法 JSON，但都不是我们要的对象
        logger.warning("丢弃工具调用：arguments 不是对象 | tool=%s | type=%s", tool_name, type(data).__name__)
        return None

    try:
        validated = model(**data)
    except Exception as exc:  # ValidationError 之外也可能有别的，一律当坏草稿
        logger.warning(
            "丢弃工具调用：参数校验失败 | tool=%s | err=%s | args=%r",
            tool_name,
            exc,
            arguments[:_ARGS_LOG_LIMIT],
        )
        return None

    return ToolDraft(
        tool_call_id=str(call.get("id") or ""),
        tool_name=tool_name,
        action_type=ACTION_TYPE_BY_TOOL[tool_name],
        payload=validated.model_dump(mode="json"),
    )


def parse_tool_calls(reply: Any) -> list[ToolDraft]:
    """从 LLMReply 里解析草稿，最多返回 `MAX_DRAFTS_PER_REPLY` 个。

    参数写成 `Any` 而不是 `LLMReply`：测试里可以传一个只有 `tool_calls`
    属性的假对象，不必为了造一个 LLMReply 去拼完整字段。真正用到的只有 `.tool_calls`。

    返回空列表表示「这次没有可用草稿」—— 调用方应当把模型的 content 当普通回复显示。
    """
    calls = getattr(reply, "tool_calls", None) or []
    if not calls:
        return []

    drafts: list[ToolDraft] = []
    for call in calls:
        if not isinstance(call, dict):
            logger.warning("丢弃工具调用：call 不是对象 | %r", call)
            continue
        draft = _parse_one(call)
        if draft is not None:
            drafts.append(draft)

    if len(drafts) > MAX_DRAFTS_PER_REPLY:
        logger.info(
            "本次回复有 %d 份草稿，只采纳前 %d 份", len(drafts), MAX_DRAFTS_PER_REPLY
        )
        drafts = drafts[:MAX_DRAFTS_PER_REPLY]

    return drafts


__all__ = [
    "ACTION_TYPE_BY_TOOL",
    "MAX_DRAFTS_PER_REPLY",
    "CreateTaskPayload",
    "ToolDraft",
    "parse_tool_calls",
]