"""V1.1 A5 第 1 步：可用工具的声明（只描述，不执行）。

这份声明会被原样塞进请求体的 `tools` 字段发给模型。模型靠它知道三件事：
自己能申请什么、参数长什么样、什么时候该申请。

**这里绝不能出现任何数据库操作**。工具声明是一份「说明书」，谁都能读；
真正的执行在 `execute.py`（第 5 步，写入）与 `tool_flow._run_read_tool`
（A6，只读查询），而写入的前提是用户点了确认。

工具分两类，这条界线比什么都重要：

- **写入类**（`create_task` / `update_task` / `create_expense`）：产出一张待确认卡片，
  用户点确认才落库。
- **只读类**（`query_tasks`）：立刻执行、结果回灌给模型继续说话，不落库、无卡片。

`update_task` 的存在顺带解释了为什么要先有 `query_tasks`：
模型必须能**先查到那条待办的真实 id**，才谈得上改它。它的参数里
`task_id` 是必填的 —— 模型不许靠猜 id 改数据。

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

QUERY_TASKS: dict = {
    "type": "function",
    "function": {
        "name": "query_tasks",
        # 和 create_task 一样四层：干什么 / 何时用 / 何时别用 / 拿不准怎么办。
        # 这里额外写清「只读」：模型常常查完就顺手说「我已经帮你改好了」，
        # 而它根本没有改写工具 —— 提前把这条说死。
        "description": (
            "查询这个人自己名下的待办事项，只读，不会修改任何数据。"
            "用户问「我还有哪些没做」「这周有什么安排」「买牛奶那条是什么时候」时调用；"
            "用户说要改或删某条待办、但没说是哪一条时，也先调用这个对上号。"
            "用户明确要求新建待办时不要用这个（用 create_task）。"
            "查询结果只是给你看的，回复里不要说「已经帮你改好/删掉」这类话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "标题里包含的字，例如「牛奶」。用户没提具体事项就不传",
                },
                "status": {
                    "type": "string",
                    "enum": ["todo", "done"],
                    "description": "todo=还没做的，done=已完成的。用户问「做完了吗」传 done",
                },
                "category_name": {
                    "type": "string",
                    "description": "分类名，只能从系统提示词里列出的分类中选",
                },
                "due_before": {
                    "type": "string",
                    "description": (
                        "只查截止时间早于此刻的，ISO 8601 带时区偏移，"
                        "例如 2026-11-15T00:00:00+08:00。用户说「这周」「今天」时据此换算"
                    ),
                },
                # 上限 10 不是随便定的：结果会原样回灌进下一轮请求体，
                # 放开到 100 条就是几千 token 的账单，而且模型反而更容易挑错行
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "返回条数，默认 5。用户问「全部」也不要超过 10",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

UPDATE_TASK: dict = {
    "type": "function",
    "function": {
        "name": "update_task",
        # 四层之外多一条硬规则：**先查后改**。模型最容易犯的错是
        # 拿着上下文里记得的编号直接改 —— 那个编号要么过期，要么根本不存在。
        "description": (
            "修改一条已存在的待办。"
            "用户说「把买牛奶那条改到明天」「交房租的优先级调高」「那条待办改名」时调用。"
            "task_id 必须来自 query_tasks 的查询结果，不要凭记忆或猜测填写；"
            "不知道是哪一条时，先调用 query_tasks 找到它。"
            "只传要改的字段，没提到的字段保持原样。"
            "用户明确要求新建一条时用 create_task，不要用这个。"
            "调用后只能说「我整理了一条修改，请你确认」，绝不能说你已经改好了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "要改的待办 id，必须来自 query_tasks 的结果里的 id=…",
                },
                "title": {
                    "type": "string",
                    "description": "新的标题，15 字以内。用户没提要改名就不要传",
                },
                "due_at": {
                    "type": "string",
                    "description": (
                        "新的截止时间，ISO 8601 带时区偏移，"
                        "例如 2026-11-09T15:00:00+08:00。用户没提时间就不要传"
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "新的优先级。用户没提就不要传",
                },
                "category_name": {
                    "type": "string",
                    "description": (
                        "新的分类名，只能从系统提示词里列出的分类中选。用户没提就不要传"
                    ),
                },
            },
            "required": ["task_id"],
            # 只改不建：这里没有 status 字段，勾选完成走看板，不走对话
            "additionalProperties": False,
        },
    },
}

CREATE_EXPENSE: dict = {
    "type": "function",
    "function": {
        "name": "create_expense",
        "description": (
            "记录一笔支出或收入（记账）。"
            "只在用户明确要求记一笔钱时调用，"
            "例如「昨天中午吃饭花了 38」「刚发了工资 12000」，"
            "或者用户对刚才的确认卡片说「对，记上吧」而上下文里已有金额时。"
            "用户只是抱怨贵、询问花了多少、讨论预算时，一律不要调用。"
            "金额用户没说出来时，不要调用，直接反问多少。"
            "分类拿不准就用「其他」或省略；账户没提就用「现金」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "金额，正数。收入也填正数，方向由 type 表达。最多两位小数",
                },
                "type": {
                    "type": "string",
                    "enum": ["expense", "income"],
                    "description": "expense=支出（默认），income=收入。用户说工资、收到、入账时用 income",
                },
                "category_name": {
                    "type": "string",
                    "description": (
                        "分类名，只能从系统提示词里列出的支出分类中选，例如「餐饮」「交通」。"
                        "说的是收入就从收入分类里选（如「工资」）。拿不准可以省略"
                    ),
                },
                "account": {
                    "type": "string",
                    "description": "支付账户：现金 / 微信 / 支付宝 / 银行卡。用户没提就省略",
                },
                "occurred_at": {
                    "type": "string",
                    "description": (
                        "发生时间，ISO 8601 带时区偏移，例如 2026-11-08T12:30:00+08:00。"
                        "用户说「昨天中午」时要换算成具体时间；没说时间就省略"
                    ),
                },
                "note": {
                    "type": "string",
                    "description": "备注，用户在句子里的额外信息（吃了什么、跟谁），≤ 50 字。不要编",
                },
            },
            "required": ["amount"],
            "additionalProperties": False,
        },
    },
}

# 顺序固定：以后加工具就往后追加，测试与请求体都依赖这个顺序稳定
ALL_TOOLS: list[dict] = [CREATE_TASK, QUERY_TASKS, UPDATE_TASK, CREATE_EXPENSE]

# 只读工具名：tool_flow 据此分流 —— 这批立刻执行并回灌，不产生待确认卡片
READ_ONLY_TOOLS: frozenset[str] = frozenset({"query_tasks"})

# 模型可能申请一个不存在的工具名（幻觉），用集合一秒判断认不认。
# frozenset 不可变，也不怕谁 import 之后 append
TOOL_NAMES: frozenset[str] = frozenset(tool["function"]["name"] for tool in ALL_TOOLS)