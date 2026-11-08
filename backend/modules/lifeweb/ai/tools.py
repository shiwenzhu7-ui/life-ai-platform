"""V1.1 A5 第 1 步：可用工具的声明（只描述，不执行）。

这份声明会被原样塞进请求体的 `tools` 字段发给模型。模型靠它知道三件事：
自己能申请什么、参数长什么样、什么时候该申请。

**这里绝不能出现任何数据库操作**。工具声明是一份「说明书」，谁都能读；
真正的执行在 `execute.py`（第 5 步），而执行的前提是用户点了确认。

字段名与 `tasks/model.py`、`tasks/schema.py`、`docs/数据库设计.md` 保持同名
（title / due_at / priority），全链路一个词，读代码时不用在脑子里做映射。
"""

from __future__ import annotations

CREATE_TASK: dict = {
    "type": "function",
    "function": {
        "name": "create_task",
        # description 是模型唯一的说明书，四层信息缺一不可：
        #   1. 干什么  2. 什么时候用  3. 什么时候别用  4. 拿不准怎么办
        # 只写第 1、2 层，用户说一句「今天好累」它也会记一条待办
        "description": (
            "创建一条新的待办事项。"
            "只在用户明确要求记录一件要做的事时调用，"
            "例如「记一下明天下午三点去超市」「帮我加一条待办：周五交报告」。"
            "用户只是聊天、抱怨、描述过去发生的事、或者询问信息时，一律不要调用。"
            "用户没有说清要做什么时，不要调用，直接反问。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "要做的事，15 字以内。"
                        "不要包含时间和优先级（它们有单独字段）"
                    ),
                },
                # 关键一行：给一个能直接照抄的完整样例。
                # 只说「ISO 8601 格式」时，模型常写成 Z 结尾（差 8 小时）或少了时区
                "due_at": {
                    "type": "string",
                    "description": (
                        "截止时间，ISO 8601 带时区偏移，"
                        "例如 2026-11-09T15:00:00+08:00。"
                        "用户没说时间就不要传这个字段，不要编造时间"
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "重要程度。用户没说就传 medium",
                },
                # 用名字不用 id：模型不可能知道你这边分类的 id 是几，
                # 由后端按名字回查 categories 表，查不到落 0（未分类）
                "category_name": {
                    "type": "string",
                    "description": (
                        "分类名，只能从系统提示词里列出的分类中选。不确定就不传"
                    ),
                },
            },
            "required": ["title"],
            # 不允许模型自己加字段：加了也存不进 payload（schema 里没有）
            "additionalProperties": False,
        },
    },
}

# 顺序固定：以后加工具就往后追加，测试与请求体都依赖这个顺序稳定
ALL_TOOLS: list[dict] = [CREATE_TASK]

# 模型可能申请一个不存在的工具名（幻觉），用集合一秒判断认不认。
# frozenset 不可变，也不怕谁 import 之后 append
TOOL_NAMES: frozenset[str] = frozenset(tool["function"]["name"] for tool in ALL_TOOLS)