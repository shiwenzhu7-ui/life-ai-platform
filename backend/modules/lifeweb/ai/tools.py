"""V1.1 A5 第 1 步：可用工具的声明（只描述，不执行）。

这份声明会被原样塞进请求体的 `tools` 字段发给模型。模型靠它知道三件事：
自己能申请什么、参数长什么样、什么时候该申请。

**这里绝不能出现任何数据库操作**。工具声明是一份「说明书」，谁都能读；
真正的执行在 `execute.py`（第 5 步，写入）与 `tool_flow._run_read_tool`
（A6，只读查询），而写入的前提是用户点了确认。

工具分两类，这条界线比什么都重要：

- **写入类**（`create_task` / `update_task` / `create_expense` / `delete_task`）：
  产出一张待确认卡片，用户点确认才落库。
- **只读类**（`query_tasks` / `query_expenses`）：立刻执行、结果回灌给模型继续说话，
  不落库、无卡片。

`update_task` 的存在顺带解释了为什么要先有 `query_tasks`：
模型必须能**先查到那条待办的真实 id**，才谈得上改它。它的参数里
`task_id` 是必填的 —— 模型不许靠猜 id 改数据。

`query_expenses` 同时回答两种问法：「这个月花了多少」（要合计）和
「上周都买了什么」（要明细）。合计永远按**全部命中记录**算，不受 `limit` 影响，
否则「一共 3200」会被悄悄说成「这 5 条一共」。语句风格上与 `query_tasks` 对齐：
日期参数用户说「这个月」就要模型自己换算成 `2026-11-01`，换算错就查错时间段。

字段名与 `tasks/model.py`、`tasks/schema.py`、`docs/数据库设计.md` 保持同名
（title / due_at / priority），全链路一个词，读代码时不用在脑子里做映射。
"""

from __future__ import annotations

import re

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
            "缺信息时**一次把缺的全问完**：要做什么没说就问做什么，"
            "时间没说就问什么时候，分类没说就问是哪一类，"
            "几项都没说就在一条回复里逐条列出来一起问，不要只问一个、也不要此时出卡。"
            "问完等用户回答。**用户补了一种就先按默认值出卡、别再追问其他项**："
            "分类拿不准就不传（落「其他」），时间没说就不传（记现在）——"
            "这些用户都能在确认卡上改，追问不记得的细节只会让用户多打几轮字。"
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
                        "用户没说时间就不传这个字段，后端会记成现在"
                    ),
                },
                # 优先级的规则由「工具描述」而不是系统提示词来管：
                # 它要和 due_at 一起看（有时间的按远近分档），放在同一个
                # 参数说明里，模型不会漏读一半。
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "重要程度，按这套可解释的规则给，不要凭感觉："
                        "有明确时间且在 3 天内 → high；有明确时间但更远 → medium；"
                        "没有任何时间 → low。"
                        "用户明确说「很急」「重要」「不急」「随便」时，以用户的话为准。"
                        "这一项不用问用户"
                    ),
                },
                # 用名字不用 id：模型不可能知道你这边分类的 id 是几，
                # 由后端按名字回查 categories 表，查不到落 0（未分类）
                "category_name": {
                    "type": "string",
                    "description": (
                        "分类名，只能从系统提示词里列出的分类中选。"
                        "用户没说分类就不传（落「其他」），不要编一个，也**不要为了问分类而推迟出卡**"
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
            "缺信息时**一次把缺的全问完**：金额没说就问金额，"
            "用途没说就问花在什么上，支付方式没说就问微信/支付宝/现金/银行卡，"
            "几种都没说就在一条回复里逐条列出来一起问，不要只问一个、也不要此时出卡。"
            "问完等用户回答。**用户补了一种就先按默认值出卡、别再追问其他项**："
            "用途拿不准落「其他」，支付方式没说记「现金」，"
            "发生时间没说记现在——这些用户都能在确认卡上改，"
            "追问不记得的细节只会让用户多打几轮字。"
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
                    "description": "支付账户：现金 / 微信 / 支付宝 / 银行卡。用户没提就省略，后端会记「现金」",
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

QUERY_EXPENSES: dict = {
    "type": "function",
    "function": {
        "name": "query_expenses",
        "description": (
            "查询这个人自己名下的收支记录与合计，只读，不会修改任何数据。"
            "用户问「这个月花了多少」「吃饭花了多少钱」「上周都买了什么」时调用；"
            "问总数、问某类、问某段时间，都先用它拿真实数字再回答。"
            "用户只是要记一笔账时不要用这个（用 create_expense）。"
            "查询结果只是给你看的，回复里不要说「已经帮你改好/删掉」这类话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": (
                        "起始日期（含），形如 2026-11-01。"
                        "用户说「这个月」就传当月 1 号，「上周」传上周一，「今天」传今天"
                    ),
                },
                "date_to": {
                    "type": "string",
                    "description": "结束日期（含），形如 2026-11-30。与 date_from 配套使用",
                },
                "type": {
                    "type": "string",
                    "enum": ["expense", "income"],
                    "description": (
                        "expense=只看支出，income=只看收入。"
                        "问「赚了多少」「工资呢」传 income；问「花了多少」传 expense 或不传"
                    ),
                },
                "category_name": {
                    "type": "string",
                    "description": "分类名，只能从系统提示词里列出的分类中选，例如「餐饮」。不传则不限",
                },
                "account": {
                    "type": "string",
                    "description": "支付方式：现金 / 微信 / 支付宝 / 银行卡。用户没提就不传",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "返回的明细条数，默认 5。合计不受这个限制，永远是全部记录的总和",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

DELETE_TASK: dict = {
    "type": "function",
    "function": {
        "name": "delete_task",
        # 与 update_task 一样是「先查后删」，另外多一条：**删只删一条**。
        # 用户说「把这几条都删了」时，模型应当一次只提一条，让用户逐张确认 ——
        # 一次删五条只要有一张卡理解错了，用户就要花五分钟去查哪条没了。
        "description": (
            "删除一条已存在的待办。"
            "只在用户明确要求删掉某条待办时调用，例如「把买菜那条删了」「这条不用了，删掉」。"
            "task_id 必须来自 query_tasks 的查询结果，不要凭记忆或猜测填写；"
            "不知道是哪一条时，先调用 query_tasks 找到它；找不到就如实说没找到，不要自己编一个。"
            "用户说「把这几条都删了」时，一次只提一条，等用户确认后再提下一条。"
            "用户只是说「做完了」「不用了」但没说要删时，用 update_task 改状态，不要删。"
            "调用后只能说「我整理了一条删除，请你确认」，绝不能说你已经删掉了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "要删的待办 id，必须来自 query_tasks 的结果里的 id=…",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "为什么要删，一句话，比如「买完了」「记错了」。"
                        "会显示在确认卡上，帮用户核对。用户没说就不用传"
                    ),
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
}

UPDATE_EXPENSE: dict = {
    "type": "function",
    "function": {
        "name": "update_expense",
        # 与 update_task 同构，但账目**没有标题**，模型无法靠「名字」认出是哪一笔，
        # 只能靠 id。所以 description 的第一件事就是把「先查再改」这条流程说死。
        "description": (
            "修改一笔已存在的账。"
            "只在用户明确要求改某一笔时调用，例如「把昨晚那笔打车改成 30 块」"
            "「那杯咖啡记错分类了，应该是餐饮」。"
            "expense_id 必须来自 query_expenses 的查询结果，不要凭记忆或猜测填写；"
            "不知道是哪一笔时，先调用 query_expenses 找到它；查出来有多笔都像时，"
            "把这几笔列出来问用户是哪一笔，不要自己挑一个；找不到就如实说没找到。"
            "**只传用户要求改的那几个字段**，其余不传（不传 = 保持原值）；"
            "不要顺手把分类、账户、备注重新猜一遍。"
            "用户说的是「记错了一笔，删掉重记」这类要作废的，用 delete_expense，不要用本工具。"
            "调用后只能说「我整理了一条修改，请你确认」，绝不能说你已经改好了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expense_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "要改的那笔账的 id，必须来自 query_expenses 结果里的 id=…",
                },
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "改成的金额，正数，两位小数。用户没说就不传",
                },
                "type": {
                    "type": "string",
                    "enum": ["expense", "income"],
                    "description": "支出还是收入。通常不用传；用户说「这笔其实是收入」才传",
                },
                "category_name": {
                    "type": "string",
                    "description": "改成的分类名，例如「餐饮」。用户没说就不传",
                },
                "account": {
                    "type": "string",
                    "description": "改成的支付方式：现金 / 微信 / 支付宝 / 银行卡。用户没说就不传",
                },
                "occurred_at": {
                    "type": "string",
                    "description": (
                        "改成什么时间花的，带时区的 ISO8601，例如 2026-11-08T21:00:00+08:00。"
                        "用户没说就不传"
                    ),
                },
                "note": {
                    "type": "string",
                    "description": "改成的备注。用户没说就不传",
                },
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
    },
}

DELETE_EXPENSE: dict = {
    "type": "function",
    "function": {
        "name": "delete_expense",
        "description": (
            "删除一笔已存在的账。"
            "只在用户明确要求删掉某一笔时调用，例如「删掉昨天那杯咖啡」「这笔记错了，删了吧」。"
            "expense_id 必须来自 query_expenses 的查询结果，不要凭记忆或猜测填写；"
            "查出来有多笔都像时，列出来问用户是哪一笔；找不到就如实说没找到。"
            "用户说「把这几笔都删了」时，一次只提一笔，等用户确认后再提下一笔。"
            "只是想改数字或分类时用 update_expense，不要删了重记。"
            "调用后只能说「我整理了一条删除，请你确认」，绝不能说你已经删掉了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expense_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "要删的那笔账的 id，必须来自 query_expenses 结果里的 id=…",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "为什么要删，一句话，比如「记错了」「重复记了」。"
                        "会显示在确认卡上，帮用户核对。用户没说就不用传"
                    ),
                },
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
    },
}

SEARCH_DOCUMENTS: dict = {
    "type": "function",
    "function": {
        "name": "search_personal_documents",
        # 四层照旧：干什么 / 何时用 / 何时别用 / 拿不准怎么办。
        # 这里最要紧的是第一句「只读」和第二句「答案只能来自查到的原文」——
        # 模型拿到检索结果后最常见的毛病是顺手把常识补进去，
        # 用户看到的是「文件里写的」，实际上是模型自己编的。
        "description": (
            "在用户自己上传的知识库文档里做语义检索，只读，不会修改任何数据。"
            "用户问到过去上传的文件里才有的内容时调用："
            "「我那个预算表里房贷多少」「上次那份合同怎么写的」「我笔记里怎么记的」。"
            "用户只是闲聊、问常识、问待办和账目时不要用这个。"
            "回答**只能来自检索到的原文**：查不到就如实说「你的文档里没提到这件事」，"
            "绝不要用常识补一个像样的答案，也不要说「根据文件」而其实没查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "要检索的内容，写成一句完整的话，例如「每个月房贷多少钱」。"
                        "不要只丢一个关键词，语义检索按整句的意思找"
                    ),
                },
            },
            "required": ["query"],
            # 不给 top_k：返回几条是后端的事，放开给模型只会让它一次要 20 条撑爆上下文
            "additionalProperties": False,
        },
    },
}

# 顺序固定：以后加工具就往后追加，测试与请求体都依赖这个顺序稳定
ALL_TOOLS: list[dict] = [
    CREATE_TASK,
    QUERY_TASKS,
    UPDATE_TASK,
    CREATE_EXPENSE,
    QUERY_EXPENSES,
    DELETE_TASK,
    UPDATE_EXPENSE,
    DELETE_EXPENSE,
    SEARCH_DOCUMENTS,
]

# 只读工具名：tool_flow 据此分流 —— 这批立刻执行并回灌，不产生待确认卡片。
# 注意 search_personal_documents 属于**查询**，不是写入：
# 它的结果直接喂回模型让模型据此说话，中间没有任何东西要用户确认。
# 归错这一格，用户问「我文件里写了什么」会被弹出一张确认卡。
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {"query_tasks", "query_expenses", "search_personal_documents"}
)

# 写入工具名：这批产出一张待确认卡片。与 READ_ONLY_TOOLS 互补而不是互相推导 ——
# 以后加第三个「既不是查询也不出卡」的工具（比如 set_reminder）时不会被误判成写入。
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_task",
        "update_task",
        "create_expense",
        "delete_task",
        "update_expense",
        "delete_expense",
    }
)

# 用户在**明确下令**记东西（「帮我记一下」「记个待办」「提醒我」）。
# tool_flow 用它决定「模型没调工具时值不值得再问一次」：
# 明确下令却漏调，是模型的问题；用户只是闲聊提了一句「我最近在看书」，
# 补问反而是在把他没想记的东西往库里塞。
WRITE_INTENT = re.compile(r"(帮我记|记个|记一下|记一笔|记下来|记下|提醒我|帮我加|待办)")

# 模型可能申请一个不存在的工具名（幻觉），用集合一秒判断认不认。
# frozenset 不可变，也不怕谁 import 之后 append
TOOL_NAMES: frozenset[str] = frozenset(tool["function"]["name"] for tool in ALL_TOOLS)