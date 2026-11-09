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

A6 起多了一类**只读**调用（`query_tasks`）。它不走 `parse_tool_calls`：

| | 写入（create_task） | 只读（query_tasks） |
|---|---|---|
| 入口 | `parse_tool_calls` → `ToolDraft` | `parse_read_call` → 参数字典 |
| 参数坏掉 | 整条丢弃，让模型重来一次 | 降级成「无筛选」照常执行 |
| 结果 | 落一张待确认卡片 | 回灌给模型，不落库 |

只读之所以敢降级：查询没有必填项，查错了顶多是「多给几条」，不是「写错数据」。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.lifeweb.tasks.schema import Priority, _clean_title

logger = logging.getLogger(__name__)


def _one_line(exc: Exception) -> str:
    """pydantic 的 ValidationError 有几十行，回灌给模型只取第一行。"""
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__

# 工具名 → 数据库 ai_pending_actions.action_type 的 ENUM 值。
# 现在两个词恰好一样（create_task / update_task），但这层映射必须显式写出来：
# 工具名是给模型看的（贴近用户说法），ENUM 是对齐表结构的。
# 明天加 `record_expense` 工具时，ENUM 仍是 `create_expense`，靠这里翻译。
ACTION_TYPE_BY_TOOL: dict[str, str] = {
    "create_task": "create_task",
    "update_task": "update_task",
}

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


# 改待办时「允许模型改变」的字段。没有 status（勾完成是另一件事）、
# 没有 description（用户很少在对话里改描述，先不给口子）、
# 更没有 user_id / id / created_at —— 那些不是「内容」。
UPDATABLE_TASK_FIELDS: tuple[str, ...] = ("title", "due_at", "priority", "category_name")


class UpdateTaskPayload(BaseModel):
    """update_task 的草稿：一条已存在待办的「改成什么」。

    `task_id` 是模型从 `query_tasks` 结果里抄来的编号。**它只是线索，不是授权**：
    真正写表前 `execute._write_update` 会拿当前用户的 id 再查一遍，
    查不到就 404 —— 模型抄错行、或者编个 id 出来的，在这里拦不住，在那边拦得住。

    至少要有变化项：只给 task_id 等于「我想改但不知道改什么」，
    点确认什么都不会发生，不如当校验失败重试一次。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    due_at: datetime | None = None
    priority: Priority | None = None
    category_name: str | None = Field(default=None, max_length=32)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        return value if value is None else _clean_title(value)

    @field_validator("due_at")
    @classmethod
    def due_at_sane(cls, value: datetime | None) -> datetime | None:
        # 与 create_task 同一套规则（必须带时区、年份合理），不另写一份
        return CreateTaskPayload.due_at_sane(value)

    @model_validator(mode="after")
    def at_least_one_change(self) -> "UpdateTaskPayload":
        if not any(getattr(self, name) is not None for name in UPDATABLE_TASK_FIELDS):
            raise ValueError("至少要说清楚改什么（标题 / 时间 / 优先级 / 分类 里的一项）")
        return self

    def changes(self) -> dict[str, Any]:
        """只吐出真的给了值的字段 —— `None` 表示「没说要改」，不是「改成空」。

        与 tasks 的 PATCH 三态不同：这里不支持「清空时间」。
        把一条待办的时间改成空（＝不再有截止日）在对话里极难表达清楚，
        模型说「那个时间不要了」到底是清空还是改期，人和模型的理解常常不同 ——
        这种改动留给看板手点，对话里先只做「改成某个值」。
        """
        return {
            name: getattr(self, name)
            for name in UPDATABLE_TASK_FIELDS
            if getattr(self, name) is not None
        }


# 工具名 → 校验模型。加写入工具时在这里加一行，`parse_tool_calls` 不用改
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "create_task": CreateTaskPayload,
    "update_task": UpdateTaskPayload,
}

# 一次查询最多返回几条。与 tools.py 里 limit 的 maximum 是同一个数：
# 那边是说明书（写给模型看），这边是闸门（模型不听也拦得住）。
# 不改的话模型传 limit=1000 就能让一条回复的请求体涨到几万 token。
MAX_QUERY_LIMIT = 10
DEFAULT_QUERY_LIMIT = 5


class QueryTasksPayload(BaseModel):
    """query_tasks 的参数。五个都可选 —— 「我还有啥没做」一个词都没有也得能查。"""

    model_config = ConfigDict(extra="forbid")

    keyword: str | None = Field(default=None, max_length=50)
    status: str | None = None
    category_name: str | None = Field(default=None, max_length=32)
    # 传成 datetime 而不是 str：时区校验与 create_task 的 due_at 完全同一套规则，
    # 时区错、年份错都在这一步就挡掉，查询层只管比较（不重复实现一遍校验）
    due_before: datetime | None = None
    limit: int = DEFAULT_QUERY_LIMIT

    @field_validator("keyword")
    @classmethod
    def keyword_blank_means_none(cls, value: str | None) -> str | None:
        """模型常传 `""` 表示「不限」。空串要是当成关键词，
        会变成 LIKE '%%' —— 能查出来，但日志里全是没含义的条件。"""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("status")
    @classmethod
    def status_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in QUERYABLE_STATUS:
            raise ValueError(f"status 只能是 {'/'.join(QUERYABLE_STATUS)}")
        return value

    @field_validator("due_before")
    @classmethod
    def due_before_sane(cls, value: datetime | None) -> datetime | None:
        # 与 CreateTaskPayload.due_at 同一套规则：必须带时区、年份合理
        return CreateTaskPayload.due_at_sane(value)

    @field_validator("limit", mode="before")
    @classmethod
    def limit_clamp(cls, value: Any) -> int:
        """三个方向都要收口，缺一个就有一种崩法：

        - `"5"`：模型把数字写成字符串（真实发生过）→ 先转 int
        - `0` / `-3`：除了 500 什么都不返回 → 抬到 1
        - `1000`：请求体爆掉 → 压到 MAX_QUERY_LIMIT
        - `"abc"`：垃圾值 → 落到默认，而不是让整条消息失败
        """
        if value is None:
            return DEFAULT_QUERY_LIMIT
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning("query_tasks 的 limit 不是整数（%r），按默认 %d 处理", value, DEFAULT_QUERY_LIMIT)
            return DEFAULT_QUERY_LIMIT
        # max(1, ...) 先行：负数也要变成 1，不能变成「LIMIT -3」
        return min(max(1, number), MAX_QUERY_LIMIT)


# 用户能筛选的状态。doing=进行中的也算「没做完的一堆里」，
# 所以查询 todo 时自动带上它 —— 见 tool_flow._run_read_tool。
QUERYABLE_STATUS: tuple[str, ...] = ("todo", "done")

# 只读工具名 → 参数模型。与 PAYLOAD_MODELS 分开：这张表里的调用**不产生草稿**，
# 立刻执行、结果回灌，所以不会走 ACTION_TYPE_BY_TOOL 的翻译。
READ_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {"query_tasks": QueryTasksPayload}


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

    **只解析写入类工具**。query_tasks 不在这里出现：它不发卡片、不落库，
    走 `parse_read_call` 那条路。

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


def _read_arguments(call: dict[str, Any]) -> tuple[list[str], dict[str, Any]] | None:
    """把只读调用的 arguments 变成 {字段: 值}。

    区别在于**查不出参数时返回空字典继续执行**，而不是像写入那样整条丢弃：
    查询没有必填项，模型传了个坏 JSON 也完全能按「无筛选」查出前 5 条 ——
    让用户看到一份列表，比让他看到「我没听懂」有用。errors 只用来写日志。
    """
    raw = call.get("arguments")
    if raw is None or raw == "":
        return [], {}
    if not isinstance(raw, str):
        logger.warning("只读工具 arguments 不是字符串，按无筛选处理 | %r", raw)
        return ["arguments 不是字符串"], {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "只读工具 arguments 不是合法 JSON，按无筛选处理 | err=%s | args=%r",
            exc,
            raw[:_ARGS_LOG_LIMIT],
        )
        return [f"arguments 不是合法 JSON：{exc}"], {}
    if not isinstance(data, dict):
        logger.warning("只读工具 arguments 不是对象，按无筛选处理 | type=%s", type(data).__name__)
        return ["arguments 不是对象"], {}
    return [], data


def parse_read_call(call: Any) -> tuple[str, dict[str, Any], list[str]] | None:
    """解析单个只读调用，返回 `(tool_name, 参数, 出错原因列表)`。

    不是只读工具、或叫不出名字时返回 None（交给写入那条路处理）。
    参数校验失败**不返回 None**，而是带着出错原因和空参数返回：
    调用方据此执行一次「无筛选查询」，并把原因写进回灌给模型的内容里，
    模型下一轮就知道自己哪个参数写错了。
    """
    if not isinstance(call, dict):
        return None
    tool_name = call.get("name")
    if not isinstance(tool_name, str) or tool_name not in READ_PAYLOAD_MODELS:
        return None

    errors, data = _read_arguments(call)
    model = READ_PAYLOAD_MODELS[tool_name]
    try:
        validated = model(**data)
    except Exception as exc:  # ValidationError 之外也可能有别的
        logger.warning(
            "只读工具参数校验失败，降级为无筛选 | tool=%s | err=%s", tool_name, _one_line(exc)
        )
        return tool_name, model().model_dump(mode="json"), [*errors, _one_line(exc)]

    return tool_name, validated.model_dump(mode="json"), errors


__all__ = [
    "ACTION_TYPE_BY_TOOL",
    "DEFAULT_QUERY_LIMIT",
    "MAX_DRAFTS_PER_REPLY",
    "MAX_QUERY_LIMIT",
    "CreateTaskPayload",
    "QUERYABLE_STATUS",
    "QueryTasksPayload",
    "READ_PAYLOAD_MODELS",
    "ToolDraft",
    "UPDATABLE_TASK_FIELDS",
    "UpdateTaskPayload",
    "parse_read_call",
    "parse_tool_calls",
]