# 后端测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest          # 全部
.\.venv\Scripts\python.exe -m pytest tests/test_tasks.py -v
.\.venv\Scripts\python.exe -m pytest -k "summary"
```

## 三条要点

1. **跑在独立的 `lifeai_test` 库上**：`conftest.py` 在 import 业务模块之前把 `DATABASE_URL`
   改成 `lifeai_test`，真实库 `lifeai` 全程不被打开。库不存在会自动创建。
2. **每个用例前后清空业务表并重置自增**，再重新插入 41 条系统分类。
   所以分类 id 是稳定的（15 = 工作、1 = 餐饮、10 = 工资），测试里可以直接写死。
3. **表结构以模型为准**：session 开始时 `drop_all` + `create_all`，不跑 Alembic。
   改了模型不用写迁移就能跑测试；但迁移本身仍要单独验证（`alembic check`）。

## 覆盖范围（416 条）

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_ai_clarify.py` | 12 | 信息不全时「一次问全」这条产品要求钉成断言：提示词里有这条规则、真正发出去的 system 文本里也有、问过一轮就出卡（默认值兜底）、create_expense 只把 amount 设为必填而 account 不许写成「没提就问」、工具声明仍是合法 JSON；外加优先级三档规则（3 天内 high / 更远 medium / 没时间 low、用户明说为准、不用问用户、枚举与 `TASK_PRIORITIES` 一致、update_task 的 priority 不被污染） |
| `test_ai_tools.py` | 18 | 工具声明可 json 序列化且中文不转义、结构合 OpenAI 规范（type/description/required 与 properties 对得上/additionalProperties=False）、名字唯一、TOOL_NAMES 一致、create_task 只有 title 必填、参数名与 tasks 模块同名词、priority 枚举与库一致、模块常量不被调用方改动 |
| `test_ai_parse.py` | 34 | 解析成功（全字段/只给标题补默认/strip/JSON 可序列化/frozen）、坏数据一律丢弃不抛（坏 JSON、代码块、数组、字符串、None、空标题、超长、缺 title、时间不带时区、年份 0206/2260、枚举外优先级、自加字段、未知工具名、非 dict call）、坏的不挡好的、多条只取第一、action_type 映射与 payload 字段与 tools 声明三方一致 |
| `test_ai_pending.py` | 32 | 分类名换 id（系统分类/按 task 域隔离/未知或空回落 0/软删分类/未知动作类型）、可读时间（北京时间、Z 与 +00:00 归一、一周内补星期、无效字符串不抛）、preview 补字段且不改入参、create_draft 写 pending 行且 expires_at 落在 30 分钟内、只 flush 不 commit、重复建两张卡、确认前 tasks 表零写入、get_pending 越权或不存在 404、过期置 expired 并 410、已确认仍可读、状态机矩阵（重复确认不覆盖、已取消或已过期改不动、过期未置位也确认不了）、expires_at 为 naive UTC |
| `test_ai_execute.py` | 27 | 确认真建待办（标题/分类/优先级/时区换算）、同事务（待办已提交且卡片 confirmed）、重复确认 409 且只建一条、已取消/已过期再确认 409/410、越权 404、`payload_override` 白名单（改标题/分类/时间生效，`user_id`/`status`/`completed_at` 被丢弃，非法值 422 且卡片保持 pending）、写业务表抛错整体回滚（卡片仍 pending、tasks 零行）、取消只置状态不建数据、确认与取消互斥 |
| `test_ai_provider.py` | 16 | 假 provider 排队与入参记录、请求形状与 Bearer 头、工具透传、响应解析（含只调工具）、401 不泄露 key、429 重试一次、5xx 不无限重试、超时/连不上/坏 JSON、工厂无 key 报错 |
| `test_ai_sessions.py` | 26 | 新建默认标题/自定义/空白拒绝/忽略请求体 user_id、列表排序（空会话置后）与分页、软删过滤、跨用户 404、同 session_id 下消息按 user_id 隔离、消息顺序与分页、改标题三态、软删后行仍在、连删 404、消息带出确认卡片（preview 与 +08:00 时间、无卡为 null、已确认/过期卡照实返回且读列表不改状态、别人的卡不外泄、confirmed 卡带出 created_id 且坏 result 降级为 null、确认响应同时有 created.id 与 created_id） |
| `test_ai_tool_flow.py` | 12 | tools 真的传给 provider、纯聊天不重试也不落卡、合格调用落 pending 行且 tasks 零写入、只调工具时正文有兜底、未知分类落 0、坏参数重试一次后成功（重试请求形状与错误原文）、两次都坏→5xx 且不落卡、重试轮模型放弃调工具仍算失败、幻觉工具名不重试、草稿按会话隔离、失败轮不污染下一轮上下文、计数与消息行数一致 |
| `test_ai_query.py` | 23 | 只读查询回灌真实清单（标题/状态/北京时间/优先级/id）、默认只查未完成且 doing 也算（done 需显式传 status）、关键词与分类过滤、未知分类返回空、due_before 按北京时间、limit 夹紧到 10、空结果明说、坏 JSON 与非法状态降级为无筛选、跨用户看不到别人的待办、软删不出现、只读链路 tasks 零写入且不落卡、最多 MAX_ROUNDS 轮、轮数用尽无正文时回退清单、多轮 token 累加、query→create_task 链（结果进下一轮上下文）、同轮 query+write 以写入为准 |
| `test_ai_query_expenses.py` | 18 | 查账口径：合计按**全部命中记录**算而不受明细 limit 影响、收入支出分开合计、时间按北京时间整天切（边界那一笔不多不少）、起止反了自动换、datetime 字符串按日期处理、分类按 expense 域解析（「购物」=3 不是 18）、未知分类返回空、账户过滤、别人的与软删的不进合计、空结果明说、limit 夹到 10、坏参数降级为无筛选、查账零写入零卡片、模型不说话时兜底正文带合计、工具声明与只读名单一致、`_parse_date` 各种形状 |
| `test_ai_update.py` | 17 | 出卡带 before（原值/北京时间还原）且出卡时业务表零改动、编造或别人的/软删的 task_id 一律 404 且不落卡、确认只改一行不多出一条、没提的字段（时间/优先级/分类）原样保留、改时间按 +08:00→UTC 落库、没说分类沿用 before、说了新分类名按名重查、override 白名单外的 user_id/status/id 被丢弃、改出空标题 422 且卡片仍 pending、重复确认 409 不再改、过期 410、取消后确认 409、写业务表抛错整体回滚、正文为空时兜底说「我整理了一条修改」 |
| `test_ai_delete.py` | 23 | 出卡带 `gone` 快照（标题/优先级/北京时间还原/优先级中文）且出卡时待办零改动；编造、别人的、已软删的 task_id 一律 404 且不落卡；缺 task_id 走重试仍失败则 502；确认后**只软删那一行**（deleted_at 有值、行仍在、别的待办不动、列表里消失）；无请求体也能确认；override 一个字段都不许传（换个 id 删、改标题、改分类、改状态、改归属，全部 422 且卡片仍 pending）；重复确认 409、过期 410、取消后 409；卡片躺着时待办被页面删掉→确认 404 且卡片仍 pending；写业务表抛错整体回滚；正文为空兜底说「我整理了一条删除」、模型自己说了话就不覆盖 |
| `test_ai_expense.py` | 24 | 说出「38 块 支付宝」出卡：金额/时间/账户/分类都在 preview 且库里零写入；分类按 expense 域解析（「购物」落 3 而不是 task 域的 18）；分类名不认识落 0（未分类）照常出卡；收入类型与默认发生时间（出卡时刻）；金额 0 / 负数 / 三位小数 / 无时区时间一律不落卡；确认真写一笔且 source=ai、未分类 0 能落库、时间按 +08:00→UTC；重复确认 409、过期 410、取消后 409、别人的卡 404；override 可改金额与分类但改不了 source/user_id/id，非法值 422 且卡片仍 pending；写业务表抛错整卡回滚；模型不说话时兜底正文是「我整理了一笔账」（不是「待办」，响应与库里都验）|
| `test_ai_chat.py` | 16 | 不带 session_id 自动建会话并取首句标题、追加到已有会话、system 提示词含时间与分类、历史按序回灌且过滤失败行、用量记实际 model、按配置算费用、空/超长消息 422、别人的/软删的/不存在的会话 404 且不落消息、超时 504 落失败行与失败用量、429/401/503 映射 |
| `test_health.py` | 4 | healthz / readyz / 未注册路径与错方法的统一错误体 |
| `test_tasks.py` | 36 | 列表分页与全部筛选、三种排序、新建、详情、PATCH 三态、状态幂等、软删除、用户隔离 |
| `test_expenses.py` | 30 | 记一笔、金额精度与边界、收支分类域、标签清洗、改一笔、软删除、月度汇总 |
| `test_categories.py` | 7 | scope 过滤、字段白名单、他人私有分类不可见、软删除不可见 |
| `test_dashboard.py` | 9 | 四处口径、配额制 focus、三个时间窗口、7 天补零、隔离与软删 |

用户隔离（别人的数据返回 404 而不是 403）每个模块都有用例。
