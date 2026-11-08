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

## 覆盖范围（303 条）

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_ai_tools.py` | 9 | 工具声明可 json 序列化且中文不转义、结构合 OpenAI 规范（type/description/required 与 properties 对得上/additionalProperties=False）、名字唯一、TOOL_NAMES 一致、create_task 只有 title 必填、参数名与 tasks 模块同名词、priority 枚举与库一致、模块常量不被调用方改动 |
| `test_ai_parse.py` | 34 | 解析成功（全字段/只给标题补默认/strip/JSON 可序列化/frozen）、坏数据一律丢弃不抛（坏 JSON、代码块、数组、字符串、None、空标题、超长、缺 title、时间不带时区、年份 0206/2260、枚举外优先级、自加字段、未知工具名、非 dict call）、坏的不挡好的、多条只取第一、action_type 映射与 payload 字段与 tools 声明三方一致 |
| `test_ai_pending.py` | 32 | 分类名换 id（系统分类/按 task 域隔离/未知或空回落 0/软删分类/未知动作类型）、可读时间（北京时间、Z 与 +00:00 归一、一周内补星期、无效字符串不抛）、preview 补字段且不改入参、create_draft 写 pending 行且 expires_at 落在 30 分钟内、只 flush 不 commit、重复建两张卡、确认前 tasks 表零写入、get_pending 越权或不存在 404、过期置 expired 并 410、已确认仍可读、状态机矩阵（重复确认不覆盖、已取消或已过期改不动、过期未置位也确认不了）、expires_at 为 naive UTC |
| `test_ai_execute.py` | 27 | 确认真建待办（标题/分类/优先级/时区换算）、同事务（待办已提交且卡片 confirmed）、重复确认 409 且只建一条、已取消/已过期再确认 409/410、越权 404、`payload_override` 白名单（改标题/分类/时间生效，`user_id`/`status`/`completed_at` 被丢弃，非法值 422 且卡片保持 pending）、写业务表抛错整体回滚（卡片仍 pending、tasks 零行）、取消只置状态不建数据、确认与取消互斥 |
| `test_ai_provider.py` | 16 | 假 provider 排队与入参记录、请求形状与 Bearer 头、工具透传、响应解析（含只调工具）、401 不泄露 key、429 重试一次、5xx 不无限重试、超时/连不上/坏 JSON、工厂无 key 报错 |
| `test_ai_sessions.py` | 24 | 新建默认标题/自定义/空白拒绝/忽略请求体 user_id、列表排序（空会话置后）与分页、软删过滤、跨用户 404、同 session_id 下消息按 user_id 隔离、消息顺序与分页、改标题三态、软删后行仍在、连删 404、消息带出确认卡片（preview 与 +08:00 时间、无卡为 null、已确认/过期卡照实返回且读列表不改状态、别人的卡不外泄） |
| `test_ai_tool_flow.py` | 12 | tools 真的传给 provider、纯聊天不重试也不落卡、合格调用落 pending 行且 tasks 零写入、只调工具时正文有兜底、未知分类落 0、坏参数重试一次后成功（重试请求形状与错误原文）、两次都坏→5xx 且不落卡、重试轮模型放弃调工具仍算失败、幻觉工具名不重试、草稿按会话隔离、失败轮不污染下一轮上下文、计数与消息行数一致 |
| `test_ai_chat.py` | 16 | 不带 session_id 自动建会话并取首句标题、追加到已有会话、system 提示词含时间与分类、历史按序回灌且过滤失败行、用量记实际 model、按配置算费用、空/超长消息 422、别人的/软删的/不存在的会话 404 且不落消息、超时 504 落失败行与失败用量、429/401/503 映射 |
| `test_health.py` | 4 | healthz / readyz / 未注册路径与错方法的统一错误体 |
| `test_tasks.py` | 36 | 列表分页与全部筛选、三种排序、新建、详情、PATCH 三态、状态幂等、软删除、用户隔离 |
| `test_expenses.py` | 30 | 记一笔、金额精度与边界、收支分类域、标签清洗、改一笔、软删除、月度汇总 |
| `test_categories.py` | 7 | scope 过滤、字段白名单、他人私有分类不可见、软删除不可见 |
| `test_dashboard.py` | 9 | 四处口径、配额制 focus、三个时间窗口、7 天补零、隔离与软删 |

用户隔离（别人的数据返回 404 而不是 403）每个模块都有用例。
