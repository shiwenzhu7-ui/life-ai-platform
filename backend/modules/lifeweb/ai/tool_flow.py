"""A5 第 4 步 + A6：把模型的「工具申请」变成草稿，或者变成一次真实的只读查询。

这是整个 AI 模块里**唯一一层编排**，把各步的零件接起来：

    provider.chat(messages, tools=ALL_TOOLS)   ← tools.py 的说明书
        ↓  reply.tool_calls
    ├─ 只读（query_tasks）→ _run_read_tool → 结果拼成 role=tool → 回到 provider.chat
    └─ 写入（create_task）→ parse_tool_calls → create_draft → PendingAction（未提交）
        ↓  如果这一轮只有只读调用，模型拿到结果再说一遍话
    ToolFlow(reply=最后一轮的回复, draft=本轮草稿)

为什么单独一个文件、不直接塞进 service.chat：
service 的职责是「一次问答的事务边界」，这段是「一次问答里工具这一支的花活」，
混在一起之后 service.chat 会长到没法读。这里只依赖 provider/pending/action_schema，
**不知道 HTTP，也不知道 FastAPI**，所以能脱离接口单独测。

六条必须写下来的约束：

1. **正常路径只调一次模型。** `tools=` 一直带着（见 tools_available）；
   第一次没给 tool_calls 就直接结束 —— 99% 的对话是聊天，不该为它们多花一次钱。

2. **只有「交了工具调用、但参数不合格」才重试，最多一次。**
   网络超时重试由 provider 内部负责（429/5xx 重试 1 次），那是「这次没问到」；
   参数不合格是「问到了但答得不对」，后果是用户看到一句空话、卡片没了。
   两种错误重试的理由不同，不能混在一起处理。重试仍走 provider 的 `messages=`
   接口把错误原话喂回去，不新增 provider 方法。

3. **模型先要了工具、又交了不合格的参数，不当成「它改主意了」，当成失败。**
   第二轮模型完全可能只回一段文字、一个 tool_call 都不给（放弃重试）。
   如果这时把文字当成功回复，用户看到的是「好的，已经帮你记下了」+ 没有卡片，
   库里也什么都没有 —— 这是 A5 最危险的状态。所以重试轮只要没有可用草稿，
   就按 `MALFORMED_TOOL_ARGS` 失败处理（`ok=False`）。

4. **模型完全没想调工具（`has_calls=False`）是成功。**
   `ok=True`、`draft=None`，调用方把 content 当普通回复显示。这是最常见的路径，
   不能因为「没有草稿」就报错。

5. **参数错误的原文要留在 `ToolFlow.rejection` 里。**
   调用方（service）会把它写进助手消息的 `error_message` 列，
   这样以后查「为什么模型又没给时区」不用翻日志。

6. **A6 的只读分支：一次问答最多 3 轮模型调用，写入工具出现即收手。**
   用户在 `_build_llm_messages` 里说的那句历史为什么不回灌 tool_calls 行，
   这里给出完整答案：**模型看到的是一份「工具结果已经并进对话」的摘要**，
   不是原封不动的调用链。见 `_run_read_tool` 与 `_fold_messages`。

`MAX_DRAFTS_PER_REPLY == 1`：一次只接一张卡。模型嘴上说「我不调工具」
却仍然带了 tool_calls，那就按调用走（`has_calls` 优先）—— 以行为为准，不以措辞为准。

7. **「只说不做」的补救：用户明确让记，模型只在正文里许诺、没调工具时，补问一次。**
   真机会话 85/86/87：它写下「我整理了一条待办，请你确认」，底下却没有卡片，
   数据也没写入 —— 用户以为记好了。提示词规则 7 压不住这种漂移，所以放在服务端兜：
   只在**用户这句明确下令记东西**（`tools.WRITE_INTENT`）时判定，
   只补**一次**（`nudged`），补问后仍无卡就把正文换成「这次没记下来」的实话。
   代价：极少数轮多一次模型调用（失败的那一条消息里也有据可查）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from core.time import now_beijing
from modules.lifeweb.ai.action_schema import (
    PAYLOAD_MODELS,
    ToolDraft,
    parse_read_call,
    parse_tool_calls,
)
from modules.lifeweb.ai.models import ChatSession, PendingAction
from modules.lifeweb.ai import pending as pending_mod
from modules.lifeweb.ai.pending import create_draft, resolve_category_id
from modules.lifeweb.ai.provider import LLMError, LLMProvider, LLMReply, merge_replies
from modules.lifeweb.ai.tools import (
    ALL_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
    WRITE_INTENT,
    WRITE_TOOLS,
)
from modules.lifeweb.expenses import crud as expenses_crud
from modules.lifeweb.knowledge import service as knowledge_service
from modules.lifeweb.tasks import crud as tasks_crud
from modules.shared.money import money

# 重试时塞给模型的错误原文上限：完整校验信息可能几百字，太长反而盖住重点
_REJECTION_MAX = 300

# 参数错误原文里会带上一整段 JSON（pydantic 的 input_value），用户不需要看
_ARGS_DUMP = re.compile(r"\s*input_value=.*?\s*input_type=[^,)]*", re.S)

# 一次问答里最多调几次模型（含第一次）。写成 3 而不是「不限」：
# 每多一轮就是一次真实的 HTTP 请求与一份 token 账单，而用户等的耐心只有几秒。
# 正常的「查一下再说」只需要 2 轮；3 轮给了「先查、再按结果建卡、再说一句」的余量。
MAX_ROUNDS = 3

# 查询结果最多回灌多少字。一条待办约 40 字，5 条也就 200 字；
# 留 1200 是给「模型什么条件都没给、还查了 10 条」的极端情况兜底。
_RESULT_MAX_CHARS = 1200

# 知识库检索结果的上限单独放宽。查账的结论文本是模型写的、几十字就够；
# 检索的结论文本是**原文本身**，5 段 × 300 字 ≈ 1500 字，砍到 1200 会把最后一段拦腰截断
_SEARCH_RESULT_MAX_CHARS = 2400

# 感知到的状态：用户问「没做完的」时，进行中的也算
_OPEN_STATUSES = ("todo", "doing")


@dataclass
class ToolFlow:
    """一次问答里「工具这一支」的结果。

    - `ok=True, draft is None`      → 正常聊天，content 直接显示
    - `ok=True, draft is not None`  → 有卡片待确认（草稿已 flush，未提交）
    - `ok=False`                    → 模型想调工具但参数不行，调用方按失败处理
    """

    reply: LLMReply
    draft: ToolDraft | None = None
    pending: PendingAction | None = None
    has_calls: bool = False
    rejection: str | None = None
    ok: bool = True
    retried: bool = False
    last_error: LLMError | None = None
    """第一次调用的失败原因（重试被短路时带上，调用方据此抛 5xx）。"""

    rounds: int = 1
    """这一轮问答实际调了几次模型（A6：只读查询会多调几次）。"""

    reads: list[dict] = field(default_factory=list)
    """每次只读查询的记录：`{"tool": "query_tasks", "found": 3, "params": {...}}`。
    不进响应、不落库，只用来写日志和测试断言。"""

    suppressed: bool = False
    """这一轮模型想出卡，但因为会话里已经有一张待确认的卡，没有建新的。
    调用方据此改写正文 —— 否则用户看着「请你确认」却找不到卡。"""

    nudged: bool = False
    """这一轮触发过「只说不做」补问（模块注释第 7 条）。
    调用方据此把正文换成实话，并在补问仍无果时说明数据没写入。"""

    suppressed_titles: tuple[str, ...] = ()
    """被拦下的那一轮里，会话中已经挂着的卡都有哪些「标题」。
    用来判断用户这句是不是在说**另一件事**（见 `_origin_is_new_task`）。"""

    references: list[dict] = field(default_factory=list)
    """这一轮知识库检索命中的片段，直接回给前端渲染引用卡片。

    与 `reads` 的分工：`reads` 是给日志和测试看的「查了什么」，
    `references` 是给用户看的「答案出自哪份文件第几页」。只有检索命中了才非空，
    普通聊天永远是空列表 —— 前端据此决定要不要画引用区。"""

    @property
    def error_message(self) -> str | None:
        """给助手消息的 error_message 列用。"""
        return self.rejection[:_REJECTION_MAX] if self.rejection else None


def tools_available(provider: LLMProvider) -> bool:
    """这个 provider 的 chat 收不收 `tools` 参数。

    用**能力判断**而不是类型判断（isinstance），这样测试里换成别的假 provider 时，
    只要它的 `chat` 不收 `tools`，就自动退回纯聊天，不必改业务代码。
    DeepSeekProvider / FakeProvider 都声明了 `tools=None`，返回 True。
    """
    code = getattr(getattr(provider.chat, "__func__", None), "__code__", None)
    if code is None:
        # 内置函数 / mock 对象拿不到 __code__：按「支持」处理，
        # 真传进去不认会直接 TypeError，比悄悄降级更容易发现
        return True
    return "tools" in code.co_varnames


def run_tool_flow(
    db: Session,
    provider: LLMProvider,
    messages: list[dict],
    *,
    user_id: int,
    session: ChatSession,
    message_id: int,
    tools_enabled: bool = True,
) -> ToolFlow:
    """最多 3 轮：调模型 → 有只读调用就执行并回灌 → 有写入调用就落草稿。

    `message_id` 是**本次助手回复**的消息 id，草稿挂在它下面，用来回答
    「这张卡是哪句话产生的」。注意它必须是已经 flush 过的行（有 id）。

    `tools_enabled=False`（或 provider 不支持 tools）时是纯聊天：工具声明一栏都不发，
    也不会为「只说不做」多问一次 —— 系统提示词本来就没告诉模型有工具。

    轮次的结束条件（按优先级）：

    1. 模型没给 tool_calls → 这就是最终回复（绝大多数对话第 1 轮就结束）；
       例外：用户明确让记东西、模型却只说不做，补问一次（模块注释第 7 条）
    2. 有写入调用（create_task）→ 落草稿、立刻停。**不等模型再补一句**：
       卡片本身就是回答，再问一次只是多花钱
    3. 只有只读调用 → 执行、把结果拼进 messages，进入下一轮
    4. 轮数用尽（MAX_ROUNDS）→ 停，用最后一次的 content
    """
    tools = ALL_TOOLS if (tools_enabled and tools_available(provider)) else None

    convo: list[dict] = list(messages)
    reads: list[dict] = []
    rounds = 0
    rejection: str | None = None
    replies: list[LLMReply] = []
    last_reply: LLMReply | None = None
    nudged = False
    # 用户这一轮的原话：判断「是不是明确下令记东西」用（见 _claims_write）。
    # 在 convo 被补问改动之前取下来，之后不再变。
    origin = _last_user_text(messages)

    while rounds < MAX_ROUNDS:
        rounds += 1
        try:
            reply = provider.chat(convo, tools=tools)
        except LLMError as exc:
            return ToolFlow(
                reply=merge_replies([*replies, _empty_reply()]),
                last_error=exc,
                ok=False,
                rejection=exc.message,
                rounds=rounds,
                reads=reads,
                nudged=nudged,
            )
        replies.append(reply)
        last_reply = reply

        calls = _as_calls(reply)
        if not calls:
            # 没有工具调用就是最终回复。注意「上一轮查过、这一轮没补 tool_calls」
            # 也走这里 —— 正文可能为空，service 用 FALLBACK_REPLY 兜底。
            #
            # 唯一的例外：用户明确让记东西，它却没调工具、只在正文里许诺。
            # 用户会去找卡，而底下什么都没有。补问一次，只补一次。
            if (
                not reads
                and tools is not None
                and not nudged
                and rounds < MAX_ROUNDS
                and _claims_write(reply, origin)
            ):
                convo = _nudge_messages(convo)
                nudged = True
                continue
            # 第二个例外：用户明明在问「资料里写了什么」，模型却一个工具都没调。
            # 它接下来的回答只能来自常识 —— 而用户要的是文件里的内容。
            # 同样只补问一次，且只在还没查过的时候（见 _SEARCH_HINT）
            if (
                not reads
                and tools is not None
                and not nudged
                and rounds < MAX_ROUNDS
                and _wants_search(origin)
                and not _claims_write(reply, origin)
            ):
                convo = _search_nudge_messages(convo)
                nudged = True
                continue
            break

        read_calls = [c for c in calls if c.get("name") in READ_ONLY_TOOLS]
        write_calls = [c for c in calls if c.get("name") in WRITE_TOOLS]
        other_calls = [
            c for c in calls if c.get("name") not in (READ_ONLY_TOOLS | WRITE_TOOLS)
        ]

        if write_calls or other_calls:
            # 写入优先：这一轮只要申请了 create_task，就按原来的 A5 逻辑走，
            # 同一条回复里顺带的 query 调用直接丢掉（模型拿到卡片就够说话了）。
            # 认不出的工具名也交给写入分支：那里会判「没救了」，一次就返回。
            keep = write_calls or other_calls
            flow = _finish_write(
                db,
                provider,
                convo,
                tools,
                _with_calls(reply, keep),
                user_id=user_id,
                session=session,
                message_id=message_id,
                allow_suppress=not nudged,
            )
            # token 要把前面几轮的查询也算上，只记最后一轮会少记一大截
            flow.reply = merge_replies([*replies[:-1], flow.reply])
            flow.rounds = rounds
            flow.reads = reads
            flow.nudged = nudged
            if flow.suppressed and _origin_is_new_task(origin, flow.suppressed_titles):
                # 模型把**已经挂着的那件事**又发了一遍，而用户这句明显在说另一件事
                # （会话 118/120：用户要「去银行」，模型第二次还是发「买水果」）。
                # 直接把「上面那张卡还在等你确认」丢给用户，他要的事就消失了。
                # 再补问一次，并把「用户这次说的是什么」点明，让它重新提卡。
                if not nudged and rounds < MAX_ROUNDS:
                    convo = _nudge_messages(convo, hint=_redoS_hint(origin, flow.reply), origin=origin)
                    nudged = True
                    continue
                # 补问也救不回来：至少别让正文说谎
                flow.nudged = True
            return flow

        # 只有只读调用：执行、回灌、继续下一轮
        convo, results = _run_read_calls(db, user_id=user_id, calls=read_calls, convo=convo)
        reads.extend(results)

    # 落到这里说明：没有写入调用、轮次结束（正常聊天，或轮数用尽）
    final = last_reply or _empty_reply()
    has_calls = bool(_as_calls(final)) or bool(reads)
    if reads and not (final.content or "").strip():
        # 查了结果但模型一个字没说（轮数用尽 / 给了空正文）：
        # 直接把结果清单当正文，用户至少看到一份能用的答案
        final = _synthetic_summary(reads[-1])
    if has_calls and not (final.content or "").strip():
        rejection = "模型没有给出任何结论"
    return ToolFlow(
        reply=merge_replies([*replies[:-1], final]) if replies else final,
        has_calls=has_calls,
        rejection=rejection,
        ok=True,
        rounds=rounds,
        reads=reads,
        nudged=nudged,
        references=_collect_references(reads),
    )


def _collect_references(reads: list[dict]) -> list[dict]:
    """把检索记录里的片段收成前端要的引用列表。

    按 (document_id, chunk_id) 去重：模型有时会连调两次检索（换个说法再问一遍），
    同一段原文不该在卡片里出现两次。

    只取前端渲染需要的六个字段 —— 相似度 `score` 也留着：将来做「显示匹配度」
    或者调试「为什么检索到这段」时不用再改后端。
    """
    seen: set[tuple[int, int]] = set()
    refs: list[dict] = []
    for record in reads:
        if record.get("tool") != "search_personal_documents":
            continue
        for item in record.get("items") or []:
            # 检索结果里带的是 search_chunks 的原始键；这里宽进严出，
            # 少任何一个键就当这条不可用 —— 引用卡片宁可不画，也不能画一张错的
            try:
                key = (int(item["document_id"]), int(item["chunk_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "document_id": item["document_id"],
                    "chunk_id": item["chunk_id"],
                    "filename": item["filename"],
                    "page_no": item.get("page_no"),
                    "chunk_index": item.get("chunk_index"),
                    "snippet": _snippet(item.get("content") or ""),
                    "score": item.get("score"),
                }
            )
    return refs


# 引用卡片上显示的原文长度。完整一段有 512 字，卡片里只需要「一句能认出来」的量；
# 前端想展开看全文可以再调 /knowledge/documents/{id}
_SNIPPET_CHARS = 120


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _SNIPPET_CHARS:
        return flat
    return flat[:_SNIPPET_CHARS] + "…"


# ---------- 「只说不做」的补救（模块注释第 7 条） ----------

# 正文里出现这些词，说明它嘴上在许诺写入/删除，实际一个工具都没调。
# 只放**名词/动作**，不放「帮你」「好的」这类寒暄词 —— 闲聊里也会出现。
_WRITE_CLAIM_WORDS = (
    "待办",
    "一笔账",
    "这笔账",
    "记了账",
    "记一笔",
    "删除",
    "待确认",
    "记着",
    "说一声",
)

# 补问时塞进最后一条用户消息的说明：给它一个明确的「现在必须调用工具」，
# 并把四个写入工具各是什么写清楚，免得它翻上面的说明书。
_NUDGE_HINT = (
    "（系统提示：你刚才说要记下来，但这一轮没有真的调用工具，"
    "用户底下一张确认卡片都看不到。现在必须调用工具："
    "create_task=建待办，create_expense=记一笔账，update_task=改待办，delete_task=删待办。"
    "信息不够就先用文字反问，不要假装已经记下来。）"
)

# 用户明显在问自己的资料、模型却没检索时的补问。与写入补问分开写：
# 那一句在说「库里的东西你没存」，这一句在说「文件里的东西你没查」，
# 混用会让模型以为用户要它记账。
_SEARCH_NUDGE_HINT = (
    "（系统提示：用户这句是在问他自己上传的资料，但你这一轮没有调用 search_personal_documents，"
    "你现在的回答只能来自常识，可能完全不是文件里写的内容。"
    "现在调用 search_personal_documents，用一句完整的话作为 query 检索；"
    "查不到就如实说文档里没有相关内容。）"
)


def _last_user_text(messages: Sequence[dict]) -> str | None:
    """这一轮用户说的原话（messages 里最后一条 role=user）。

    取的是**service 拼好的原始 messages**，不是补问之后的 convo ——
    补问会把系统提示塞进最后一条 user 消息里，再取就不是用户说的话了。
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else None
    return None


def _claims_write(reply: LLMReply, origin: str | None = None) -> bool:
    """这次到底该不该补问一次。

    前提：**用户明确下令记东西**（`WRITE_INTENT` 命中用户那句）。用户只是闲聊
    「我最近在看书」，模型回一段话是对的 —— 这时候补问等于把聊天变成表单。

    在这个前提下，两种输出都算「该调工具却没调」：

    1. 正文空 —— 漏调工具时它经常只吐一个空正文；
    2. 正文带承诺/答应口吻（`_WRITE_CLAIM_WORDS`）：许诺「请你确认」、
       或者「记着」「说一声就行」这种话，实际什么都没做。
    """
    if not origin or not WRITE_INTENT.search(origin):
        return False
    text = (reply.content or "").strip()
    if not text:
        return True
    return any(word in text for word in _WRITE_CLAIM_WORDS)


def _nudge_messages(
    convo: list[dict], hint: str = _NUDGE_HINT, origin: str | None = None
) -> list[dict]:
    """把补问提示并进最后一条 user 消息（拷贝后返回，不改原列表）。

    `origin` 只在「重新提卡」那种补问里用：把用户这次的原话再贴一遍，
    因为模型被历史里同名的旧卡带偏时，光说「必须调用工具」它还会再发旧的那件事。
    """
    text = f"{origin}\n{hint}" if origin else hint
    nudged = list(convo)
    for index in range(len(nudged) - 1, -1, -1):
        if nudged[index].get("role") == "user":
            original = nudged[index].get("content")
            nudged[index] = {
                **nudged[index],
                "content": f"{original}\n{text}" if isinstance(original, str) else text,
            }
            return nudged
    nudged.append({"role": "user", "content": text})
    return nudged


def _search_nudge_messages(convo: list[dict]) -> list[dict]:
    """检索补问：复用 _nudge_messages，只是换一句话。"""
    return _nudge_messages(convo, hint=_SEARCH_NUDGE_HINT)


def _titles_of(cards: Sequence[Any]) -> tuple[str, ...]:
    """会话里那些还挂着的卡，各自在说哪件事。

    取 `payload` 里的标题（记账卡没有标题，退到分类名/金额），
    空标题直接跳过 —— 空字符串会命中任何一句原话，等于全放行。
    """
    titles: list[str] = []
    for card in cards:
        payload = getattr(card, "payload", None)
        if not isinstance(payload, dict):
            continue
        title = payload.get("title") or (payload.get("gone") or {}).get("title")
        if not title:
            title = payload.get("category_name") or payload.get("amount_text")
        title = str(title).strip() if title else ""
        if title:
            titles.append(title)
    return tuple(titles)


def _origin_is_new_task(origin: str | None, titles: Sequence[str]) -> bool:
    """用户这句是不是在说**旧卡以外**的另一件事。

    判据只有两条，宁可漏判（少补问一次）也不误判（把用户的话吞掉）：

    1. 这句话本身带「记东西」的意图（`WRITE_INTENT`）；
    2. 它提到了旧卡标题以外的内容 —— 旧卡标题的词不算「新」，
       所以「提醒我买水果」在旧卡是「买水果」时不算新事，仍按重复拦下。

    反例（会话 120）：旧卡「买水果」，用户说「帮我记个待办：周五下午三点去银行」
    —— 命中意图，且「去银行」不是旧卡内容 → 判为新事，值得再问模型一次。
    """
    if not origin or not origin.strip():
        return False
    if not WRITE_INTENT.search(origin):
        return False
    text = origin
    for title in titles:
        if title:
            text = text.replace(title, "")
    # 去掉旧卡的词之后，还得剩下点像话的内容（去银行 / 交房租 / 体检）
    return len(text.strip()) >= 4


def _redoS_hint(origin: str | None, reply: LLMReply) -> str:
    """「上一轮你又发了旧的那件事」时用的补问文本。

    和 `_NUDGE_HINT` 的区别：那个是「你没调工具」，这个是「你调了但要发的是旧的」。
    两个都必须说清楚，否则模型只会把旧卡再发一遍。
    """
    said = (reply.content or "").strip()
    lines = [
        "（系统提示：你刚才调用工具时，发的是会话里**已经挂着的那件事**，",
        "不是用户这一句说的事，所以系统把它拦下了。",
    ]
    if said:
        lines.append(f"你当时的回复是：「{said[:80]}」。")
    if origin:
        lines.append(f"用户这一次说的是：「{origin.strip()[:120]}」，请按这句话提取新的一条。")
    lines.append("现在按用户这句话调用一次工具，不要再发旧的那件事。）")
    return "".join(lines)


def _is_hopeless(reply: LLMReply) -> bool:
    """这次失败是不是「重试也没用」的那类：工具名不认识 / arguments 不是 JSON 字符串。

    参数校验失败（缺 title、时间没时区）才值得重试 —— 那是模型「知道要调什么、
    只是格式不对」；工具名对不上说明它根本没理解这份说明书。
    """
    for call in getattr(reply, "tool_calls", None) or []:
        if not isinstance(call, dict):
            return True
        if call.get("name") not in TOOL_NAMES:
            return True
        if not isinstance(call.get("arguments"), str):
            return True
    return False


def _as_calls(reply: LLMReply) -> list[dict]:
    """只保留 dict 形态的 tool_call。

    模型偶尔把 tool_calls 写成 `[null]` 或 `["create_task"]`，
    后面所有 `.get()` 都会炸 —— 在这里一次洗掉，下游不用再防。
    """
    return [call for call in (getattr(reply, "tool_calls", None) or []) if isinstance(call, dict)]


def _with_calls(reply: LLMReply, calls: list[dict]) -> LLMReply:
    """复制一份只带指定 tool_calls 的回复。

    同一轮里既申请了 create_task 又顺带申请了 query_tasks 时，
    把 query 剔掉再交给写入分支 —— 否则 parse_tool_calls 会先看到 query 报
    「未知工具」，白白把一条好参数判成失败。
    """
    return LLMReply(
        content=reply.content,
        tool_calls=list(calls),
        token_in=reply.token_in,
        token_out=reply.token_out,
        latency_ms=reply.latency_ms,
        model=reply.model,
        raw=reply.raw,
    )


# ---------- 写入分支（A5 原逻辑，加上「多轮后仍可用」的处理） ----------


def _finish_write(
    db: Session,
    provider: LLMProvider,
    convo: list[dict],
    tools: list[dict] | None,
    reply: LLMReply,
    *,
    user_id: int,
    session: ChatSession,
    message_id: int,
    allow_suppress: bool = True,
) -> ToolFlow:
    """把这一轮的写入调用变成草稿；参数不合格时按 A5 原规则重试一次。

    `allow_suppress=False` 表示这次调用是**服务端补问之后**才出现的（见模块注释第 7 条）。
    这种情况下不走「同一会话只留一张卡」的拦截 —— 模型的第一次输出里根本没有工具调用，
    「已有卡」并没有让它少发什么；而用户这句是明确的「帮我记一个」，
    拦下来只会得到一句「上面那张卡还在等你确认」，他要的那件事就没了。
    """
    drafts = parse_tool_calls(reply)
    if not drafts:
        # 交了工具调用但参数不合格 → 把错误原文喂回去，让它原地改一次。
        # 前提是「错误出在参数上」：工具名不存在 / arguments 根本不是字符串，
        # 说明模型这一支已经跑偏，同样的说明书再发一次也改不好，直接失败更省一次钱。
        rejection = _rejection_of(reply) or "工具参数没有通过校验"
        if _is_hopeless(reply):
            return ToolFlow(
                reply=reply,
                has_calls=True,
                rejection=rejection,
                ok=False,
            )

        # 重试时用**本轮真正发出去的** convo（可能已经带着查询结果），
        # 而不是 service 最初那份 messages —— 否则前几轮查到的数据白查了
        try:
            reply = provider.chat(_retry_messages(convo, rejection), tools=tools)
        except LLMError as exc:
            return ToolFlow(
                reply=_empty_reply(),
                has_calls=True,
                rejection=f"{rejection}；重试时模型调用失败：{exc.message}",
                ok=False,
                retried=True,
                last_error=exc,
            )

        drafts = parse_tool_calls(reply)
        if not drafts:
            return ToolFlow(
                reply=reply,
                has_calls=True,
                rejection=rejection,
                ok=False,
                retried=True,
            )
        retried = True
    else:
        retried = False

    # 同一会话里还挂着的旧卡（status=pending 且没过期），以及「这张还要不要发」的判据。
    # 448075a 的起因：用户随口聊天，模型把已经发过的卡原样又发一遍，
    # 屏幕上多出一张一模一样的卡（会话 82）。
    #
    # 但「有卡就一律不建」把真机另一件事吞掉了：会话 116/118 里用户明确说
    # 「帮我记个待办：周五下午三点去银行」，模型规规矩矩调了工具，
    # 却被这条规则拦下，回了一句「上面那张卡还在等你确认」。
    #
    # 所以按内容区分：
    #   - 同一类型、同一标题（记账看金额+分类）→ 认为是重复发同一件事，跳过；
    #   - 明显是另一件事 → 建新卡，并把旧卡标 expired（留痕、可逆、屏幕只留一张）。
    # 判据做成 `skip` 回调传给 parse_tool_calls：模型常常一口气交两个调用
    # （先重申旧的那张、再说新的一张，见会话 118），必须在**解析阶段**跳过旧的，
    # 否则「一次只接一张卡」的截断会先把新卡片踢掉。
    # 用 pending 模块的函数而不是在这里拼 SQL：过期语义（expires_at > now）
    # 只在 pending.py 里定义一次，两边各写一份迟早漂移。
    existing = pending_mod.open_drafts(
        db, user_id, session.id, exclude_message_id=message_id
    )

    def _is_duplicate(action_type: str, payload: dict) -> bool:
        return any(
            pending_mod.is_same_item(action_type, payload, card) for card in existing
        )

    if existing:
        drafts = parse_tool_calls(reply, skip=_is_duplicate)
        if not drafts and allow_suppress:
            # 要发的全是已经挂着的那些 —— 不建第二张，正文换成实话
            return ToolFlow(
                reply=reply,
                has_calls=True,
                retried=retried,
                suppressed=True,
                suppressed_titles=_titles_of(existing),
                rejection="这一轮没有再出卡：会话里已有一张同样的待确认卡",
            )
        if not drafts and not allow_suppress:
            # 补问之后模型**还是**把旧的那件事再发一遍：用户要的新事没提出来。
            # 不能放行（否则屏幕上多出一张和旧卡一模一样的卡），
            # 也不再补问一次（已经补过了）。交给调用方把正文换成「没记下来」。
            return ToolFlow(
                reply=reply,
                has_calls=True,
                retried=retried,
                suppressed=True,
                suppressed_titles=_titles_of(existing),
                rejection="这一轮没有再出卡：模型补问后仍然发的是旧的那件事",
            )
        # 真的建出一张新卡时，旧卡一律让位（标 expired）：
        #   - 走到「另一件事」分支：新的那件跟旧的不同，旧卡留着会同时挂两张；
        #   - 补问之后才建出来的卡：用户当时明确要记另一件事，旧卡如果不让位，
        #     屏幕上就会出现两张都能点的卡，点哪张都得先想一下
        #     —— 而这正是 448075a 要治的场景。
        # 只有「一个草稿都没留下」（上面的两个 return）才不动旧卡。
        pending_mod.expire_open_drafts(
            db, user_id, session.id, exclude_message_id=message_id
        )

    created = create_draft(
        db,
        user_id=user_id,
        session=session,
        message_id=message_id,
        draft=drafts[0],
    )
    return ToolFlow(
        reply=reply,
        draft=drafts[0],
        pending=created,
        has_calls=True,
        retried=retried,
    )


# ---------- 只读分支（A6 新增） ----------


def _run_read_calls(
    db: Session,
    *,
    user_id: int,
    calls: list[dict],
    convo: list[dict],
) -> tuple[list[dict], list[dict]]:
    """执行这一轮的只读调用，把结果并进对话。

    返回 `(新的 convo, 记录列表)`。新 convo 是**拷贝后追加**，不改原列表 ——
    上层可能还要用它去重试（见 _retry_messages）。

    为什么**不**按 DeepSeek 的要求拼成 `assistant(tool_calls) + tool(tool_call_id)`：

      - 那要求把 assistant 那一行原样回传，于是每一轮都要保存在记忆里的调用链，
        service 拼历史时还得把 tool 行也存进库 —— 存库就要考虑「历史里的工具结果算不算对话」。
      - 我们每轮重试时**重新拼** messages（见 service._build_llm_messages），
        tool 行一旦进库就会和历史里的 tool_calls 行对不上，直接 400。

    折中做法：只把「工具 + 参数 + 结果」写成一段 user 角色的补充说明。
    模型照样能读懂（它就是读文本的），而这段文字**不进库**，所以不存在对不上的问题。
    代价是 token 会被算进下一轮输入，可接受。
    """
    new_convo = list(convo)
    records: list[dict] = []

    for call in calls:
        parsed = parse_read_call(call)
        if parsed is None:
            # 只读名单里的工具，parse_read_call 只有一种情况返回 None：
            # call 不是 dict（已被 _as_calls 过滤掉）—— 真到了这里就跳过
            continue
        tool_name, params, errors = parsed
        payload = _run_read_tool(db, tool_name=tool_name, params=params, user_id=user_id)
        record = {
            "tool": tool_name,
            "found": payload["count"],
            "params": params,
            "errors": errors,
            # 留着条目本身：模型轮数用尽却一个字没说时，
            # _synthetic_summary 要拿它拼出一段给用户看的正文
            "items": list(payload["items"]),
            # 查账的合计：兜底正文要用，否则只会甩一串明细
            "totals": (
                {
                    "matched": payload.get("matched", payload["count"]),
                    "expense_total": payload.get("expense_total", "0.00"),
                    "income_total": payload.get("income_total", "0.00"),
                }
                if payload.get("totals")
                else None
            ),
        }
        records.append(record)
        new_convo.append(
            {
                "role": "user",
                "content": _fold_messages(tool_name, params, payload, errors),
            }
        )

    return new_convo, records


def _run_read_tool(
    db: Session, *, tool_name: str, params: dict[str, Any], user_id: int
) -> dict[str, Any]:
    """执行一次只读查询，返回 `{"count": n, "items": [...], "truncated": bool, ...}`。

    按工具名分流到三个执行器。多一种工具就多一个 `if`，而不是搞一张 dispatch 表 ——
    几个函数各读各的参数，比一张间接表更容易一眼看出「查询到底做了什么」。
    """
    if tool_name == "query_tasks":
        return _run_query_tasks(db, params, user_id)
    if tool_name == "query_expenses":
        return _run_query_expenses(db, params, user_id)
    if tool_name == "search_personal_documents":
        return _run_search_documents(db, params, user_id)
    raise ValueError(f"未知的只读工具：{tool_name}")


def _run_search_documents(db: Session, params: dict[str, Any], user_id: int) -> dict[str, Any]:
    """检索知识库。查不到（或知识库整个挂了）就返回空，**不抛异常**。

    调用方拿到空的 count 会照常回灌一句「没检索到」，模型据此说「你的文档里没提到」。
    让它比「工具报错」好：报错会让模型倾向于跳过检索直接回答，空的则是明确的证据。
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {"count": 0, "items": [], "truncated": False, "query": ""}

    hits = knowledge_service.search_chunks(db, user_id, query)
    return {
        "count": len(hits),
        "items": hits,
        "truncated": False,
        "query": query,
    }


def _run_query_tasks(db: Session, params: dict[str, Any], user_id: int) -> dict[str, Any]:

    category_name = params.get("category_name")
    category_id = (
        resolve_category_id(db, "create_task", category_name) if category_name else None
    )
    if category_name and category_id == 0:
        # 用户说的分类根本不存在：不能当成「未分类」去查（那会返回一堆无关的），
        # 而是直接给空结果，让模型知道自己编了个分类
        return {
            "count": 0,
            "items": [],
            "truncated": False,
            "unknown_category": category_name,
        }

    status = params.get("status")
    if status == "todo":
        # 「没做完的」包含进行中：只查 todo 会把用户正在做的事漏掉，
        # 那正是他最想听见的那几条
        statuses = list(_OPEN_STATUSES)
    elif status == "done":
        statuses = ["done"]
    else:
        # 没指定状态 = 「我现在还有哪些事」，所以默认只看没做完的。
        # 否则用户问「我还有哪些事」，模型不带 status 就会把做完的也念一遍。
        # 要看已完成的，模型必须显式传 status="done" —— 提示词里写了这一点。
        # 软删的行由 tasks_crud.search 统一排除。
        statuses = list(_OPEN_STATUSES)

    due_before = _parse_datetime(params.get("due_before"))
    limit = int(params.get("limit") or 5)

    rows = tasks_crud.search(
        db,
        user_id,
        keyword=params.get("keyword"),
        statuses=statuses,
        category_id=category_id,
        due_before=due_before,
        limit=limit,
    )

    return {
        "count": len(rows),
        "items": [_task_line(row) for row in rows],
        # 这里**不**多查一条来判断「还有更多」：tasks_crud.search 自己已经把
        # limit 夹在 1..MAX_SEARCH_LIMIT，多传 limit+1 会被压回同一个上限，
        # 于是截断判断永远是 False —— 一个永远为假的标志比没有更糟，
        # 因为它让"已经到上限了"和"正好就这些"看起来一样。
        # 需要精确的"还有更多"时，改 search 返回 total，而不是在这里猜。
        "truncated": False,
    }


def _parse_datetime(value: Any) -> datetime | None:
    """只读参数里的时间是 ISO 字符串（model_dump(mode="json") 之后）。

    解析不出来就当没给 —— 宁可查全部，也不要因为一个时间格式让用户什么都看不到。
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    """`2026-11-01` / `2026-11-01T00:00:00+08:00` 都取日期部分；解析不出来当没给。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _run_query_expenses(db: Session, params: dict[str, Any], user_id: int) -> dict[str, Any]:
    """查账：明细 + 合计（合计按全部命中记录算，见 expenses_crud.search）。"""
    category_name = params.get("category_name")
    category_id = None
    if category_name:
        # scope 必须传 "expense"：task 域也有「购物」（id 18），传错域会静默查错分类
        category_id = resolve_category_id(db, "create_expense", category_name)
        if category_id == 0:
            return {
                "count": 0,
                "items": [],
                "truncated": False,
                "unknown_category": category_name,
            }

    found = expenses_crud.search(
        db,
        user_id,
        date_from=_parse_date(params.get("date_from")),
        date_to=_parse_date(params.get("date_to")),
        type=params.get("type") or None,
        category_id=category_id,
        account=(params.get("account") or None),
        limit=int(params.get("limit") or expenses_crud.DEFAULT_SEARCH_LIMIT),
    )

    rows = found["items"]
    return {
        # count 是「返回的明细条数」，matched 才是「命中的总条数」——
        # 合计用 matched 算，模型就不会把「这 5 条」说成「一共」
        "count": len(rows),
        "items": [_expense_line(row) for row in rows],
        "truncated": found["has_more"],
        "matched": found["matched"],
        "expense_total": found["expense_total"],
        "income_total": found["income_total"],
        # 给 _fold_messages 用：合计那几行要单独渲染成一段，而不是混在明细里
        "totals": True,
    }


def _task_line(task: Any) -> str:
    """一条待办的紧凑文字形式。

    为什么不直接 model_dump 一段 JSON 回灌：JSON 里一半字段（user_id、created_at、
    version…）模型完全用不上，白占 token；而且它看到 `"status": "todo"`
    会开始用字段名回答用户。
    """
    parts = [f"title={task.title}"]
    parts.append(f"status={_STATUS_TEXT.get(task.status, task.status)}")
    if task.due_at is not None:
        # 库里是 naive UTC，回灌给模型必须换成北京时间，否则它会照着 UTC 说时间。
        # fromutc 而不是 replace：replace 只是贴个标签，值还是 UTC 那个数
        parts.append(f"due={_fmt_due(task.due_at)}")
    if task.priority != "medium":
        parts.append(f"priority={_PRIORITY_TEXT.get(task.priority, task.priority)}")
    parts.append(f"id={task.id}")
    return "，".join(parts)


_STATUS_TEXT = {"todo": "未完成", "doing": "进行中", "done": "已完成", "cancelled": "已取消"}
_PRIORITY_TEXT = {"low": "低", "medium": "中", "high": "高"}
_EXPENSE_TYPE_TEXT = {"expense": "支出", "income": "收入"}


def _expense_line(expense: Any) -> str:
    """一笔账的紧凑文字形式，与 _task_line 同理：只留模型用得上的字段。

    金额从 `money()` 出（两位小数字符串），日历上写北京时间 ——
    库里 07:00 UTC 是用户眼里的 15:00，回灌错时区模型就会照着 UTC 说时间。
    """
    parts = [
        f"金额=¥{money(expense.amount)}",
        f"类型={_EXPENSE_TYPE_TEXT.get(expense.type, expense.type)}",
        f"时间={_fmt_due(expense.occurred_at)}",
        f"支付方式={expense.account}",
    ]
    # category_id=0 是「未分类」，值得说出来；名字由模型对照提示词里的分类表翻译
    parts.append(f"分类id={expense.category_id}")
    if expense.note:
        parts.append(f"备注={expense.note}")
    parts.append(f"id={expense.id}")
    return "，".join(parts)


def _fmt_due(naive_utc: datetime) -> str:
    """naive UTC → `2026-11-09 15:00（周一）`，与前端卡片上的写法保持一致。

    `replace(tzinfo=BEIJING)` 只会给 UTC 数字贴个「北京」标签，07:00 还是显示 07:00，
    差 8 小时。必须 replace 成 UTC 再用 astimezone 换算过去。
    """
    from core.time import BEIJING

    beijing = naive_utc.replace(tzinfo=timezone.utc).astimezone(BEIJING)
    return f"{beijing:%Y-%m-%d %H:%M}（{_WEEKDAYS[beijing.weekday()]}）"


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _fold_messages(
    tool_name: str, params: dict[str, Any], payload: dict[str, Any], errors: list[str]
) -> str:
    """把一次查询的结果写成一段给模型看的话（不进库，见 _run_read_calls）。"""
    pretty_params = {k: v for k, v in params.items() if v not in (None, "", [])}
    lines = [
        f"【系统：已执行 {tool_name}，参数 "
        f"{json.dumps(pretty_params, ensure_ascii=False)}】"
    ]

    unknown = payload.get("unknown_category")
    if unknown:
        lines.append(f"没有名为「{unknown}」的分类，这个条件查不到任何数据。")

    if tool_name == "search_personal_documents":
        return _fold_search_result(params, payload)

    if payload.get("totals"):
        # 合计先于明细说：模型最容易犯的错就是把「返回的这 5 条」当成「全部」
        matched = payload.get("matched") or 0
        lines.append(f"命中记录：共 {matched} 条（合计按全部 {matched} 条算，不受条数上限影响）")
        lines.append(
            f"支出合计：¥{payload.get('expense_total', '0.00')}；"
            f"收入合计：¥{payload.get('income_total', '0.00')}"
        )

    if payload["count"] == 0:
        lines.append("结果：没有符合条件的记录。")
    else:
        tail = "（明细只列了最近的一部分）" if payload.get("truncated") else ""
        lines.append(f"明细 {payload['count']} 条{tail}：")
        lines.extend(f"- {item}" for item in payload["items"])

    if errors:
        lines.append(
            f"（注意：这次的查询参数没通过校验（{errors[0]}），"
            "上面是按无筛选查的，条件可能不是你想要的。）"
        )

    lines.append("请根据以上真实数据回答用户；数据里没有的不要编造。")
    text = "\n".join(lines)
    return text[:_RESULT_MAX_CHARS]


# 每段引文回灌给模型的字数。_SNIPPET_CHARS 是给前端卡片看的（短、能认出来）；
# 模型要的是能据以回答的原文，给整段 512 字 × 5 段 = 2500 字又太贵，
# 300 字足够回答绝大多数「文件里怎么写的」。
_FOLD_CONTENT_CHARS = 300


def _fold_search_result(params: dict[str, Any], payload: dict[str, Any]) -> str:
    """检索结果的回灌文案。单独一个函数是因为**空结果时的话术很关键**。

    查不到时不能只说「结果：0 条」—— 那样模型很可能接着用常识回答，
    用户看到的是一个像模像样、其实没有出处的答案（RAG 最典型的幻觉）。
    这里把话说死：没检索到就是没有，要如实告诉用户。
    """
    query = payload.get("query") or params.get("query") or ""
    lines = [f"【系统：已在用户的知识库中检索「{query}」】"]

    if payload["count"] == 0:
        lines.append(
            "没有检索到任何相关内容。这说明用户的文档里没有提到这件事，"
            "或者还没有上传相关的文档。请如实告诉用户「你的文档里没找到相关内容」，"
            "不要用常识补一个答案，也不要说「根据你的文档」——文档里根本没人写过。"
        )
        return "\n".join(lines)

    lines.append(f"检索到 {payload['count']} 段最相关的原文：")
    for index, item in enumerate(payload["items"], start=1):
        page = f"第 {item['page_no']} 页 · " if item.get("page_no") else ""
        content = " ".join((item.get("content") or "").split())[:_FOLD_CONTENT_CHARS]
        lines.append(f"[{index}] {item.get('filename', '未知文件')}（{page}第 {item['chunk_index']} 段）：{content}")

    lines.append(
        "请只依据以上原文回答。引用时说明出自哪份文件（有页码就带上页码）；"
        "原文里没有的内容一律不要补，拿不准就说不确定。"
    )
    text = "\n".join(lines)
    # 检索结果的预算比查账大：原文本身就是要给模型看的，砍到 1200 字就白检索了
    return text[:_SEARCH_RESULT_MAX_CHARS]


def _synthetic_summary(record: dict) -> LLMReply:
    """模型查完却不说话时的兜底正文：直接把清单说给用户看。

    查账的合计优先给：用户问「这个月花了多少」时，一行合计比十条明细有用得多。
    """
    found = record.get("found") or 0
    items = record.get("items") or []
    totals = record.get("totals") or {}
    if totals and found:
        content = (
            f"这个范围一共 {totals.get('matched', found)} 笔，"
            f"支出合计 ¥{totals.get('expense_total', '0.00')}、"
            f"收入合计 ¥{totals.get('income_total', '0.00')}。\n"
            "最近的几笔：\n" + "\n".join(f"- {item}" for item in items)
        )
        return LLMReply(content=content)
    if not found:
        content = "查了一下，没有符合条件的事项。"
    else:
        content = "查到这些：\n" + "\n".join(f"- {item}" for item in items)
    return LLMReply(content=content)


def _empty_reply() -> LLMReply:
    """模型根本没回话时的占位（调用方拿它的 content 当正文，所以给句人话）。"""
    return LLMReply(content="模型没有返回内容，请稍后重试。")


def _rejection_of(reply: LLMReply) -> str | None:
    """从被丢弃的 tool_call 里挖出「为什么不行」。

    解析器本身的约定是「只丢弃、不抛异常」，所以原因只可能在两处：
    arguments 不是合法 JSON（json 库里带 message），或者参数校验失败
    （pydantic 的 ValidationError 带 first line + 字段定位）。
    挖不出来就返回一句通用原因 —— 重试仍然值得做。
    """
    for call in getattr(reply, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if name not in TOOL_NAMES:
            # 幻觉工具名不是「参数」问题，重试也没用
            return f"不支持的工具：{name}"
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            return "工具参数不是字符串"
        data = _loads(arguments)
        if data is _INVALID_JSON:
            return "工具参数不是合法 JSON"
        if not isinstance(data, dict):
            return "工具参数不是对象"
        detail = _validate_message(name, data)
        if detail:
            return detail
    return None


_INVALID_JSON = object()


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _INVALID_JSON


def _validate_message(tool_name: str, data: dict) -> str | None:
    """跑一次校验，把 pydantic 的报错压成一行人话。"""
    model = PAYLOAD_MODELS.get(tool_name)
    if model is None:
        return f"不支持的工具：{tool_name}"
    try:
        model(**data)
    except Exception as exc:  # noqa: BLE001 - 任何校验异常都只要第一行
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        cleaned = _ARGS_DUMP.sub("", first).strip()
        return cleaned or "参数校验失败"
    return None


def _retry_messages(messages: Sequence[dict], rejection: str | None) -> list[dict]:
    """重试用的 messages：系统提示 + 原问题 + 一条错误说明。

    不带历史：历史里那个坏参数已经被解析器丢掉，重发一遍反而让模型以为自己是对的。
    """
    reason = (rejection or "参数格式不对")[:_REJECTION_MAX]
    head = list(messages[:1])
    last = messages[-1]["content"] if messages else ""
    return [
        *head,
        {
            "role": "user",
            "content": f"{last}\n\n（系统提示：上一次的工具参数没有通过校验：{reason}。"
            "请按工具要求的格式重新给出一次；如果确实无法确定，就直接用文字反问用户。）",
        },
    ]


def _wants_search(text: str) -> bool:
    """这句话像不像在问「我上传的文件里写了什么」。

    只看**是否提到知识库**，不看具体问什么 —— 判断「该不该检索」是模型的活，
    这里只回答「用户是在跟我聊，还是在问文件」。判早了会让闲聊强行检索，判晚了
    用户白等一句「我不知道」。
    """
    return bool(_SEARCH_HINT.search(text or ""))


# 用户在提「资料」：文件 / 文档 / 知识库 / 附件，或者直接点名 PDF、笔记、资料。
# 刻意不收「写」「记」这种动词 —— 它们满嘴都是（「帮我记个待办」），收进来会误伤
_SEARCH_HINT = re.compile(
    r"(文件|文档|知识库|附件|资料|PDF|pdf|笔记|截图里|材料|那份|这份)"
)


__all__ = ["MAX_ROUNDS", "ToolFlow", "run_tool_flow", "tools_available"]
