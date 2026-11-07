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

## 覆盖范围（137 条）

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_ai_provider.py` | 16 | 假 provider 排队与入参记录、请求形状与 Bearer 头、工具透传、响应解析（含只调工具）、401 不泄露 key、429 重试一次、5xx 不无限重试、超时/连不上/坏 JSON、工厂无 key 报错 |
| `test_ai_sessions.py` | 19 | 新建默认标题/自定义/空白拒绝/忽略请求体 user_id、列表排序（空会话置后）与分页、软删过滤、跨用户 404、同 session_id 下消息按 user_id 隔离、消息顺序与分页、改标题三态、软删后行仍在、连删 404 |
| `test_ai_chat.py` | 16 | 不带 session_id 自动建会话并取首句标题、追加到已有会话、system 提示词含时间与分类、历史按序回灌且过滤失败行、用量记实际 model、按配置算费用、空/超长消息 422、别人的/软删的/不存在的会话 404 且不落消息、超时 504 落失败行与失败用量、429/401/503 映射 |
| `test_health.py` | 6 | healthz / readyz / 未注册路径与错方法的统一错误体 |
| `test_tasks.py` | 24 | 列表分页与全部筛选、三种排序、新建、详情、PATCH 三态、状态幂等、软删除、用户隔离 |
| `test_expenses.py` | 30 | 记一笔、金额精度与边界、收支分类域、标签清洗、改一笔、软删除、月度汇总 |
| `test_categories.py` | 7 | scope 过滤、字段白名单、他人私有分类不可见、软删除不可见 |
| `test_dashboard.py` | 10 | 四处口径、配额制 focus、三个时间窗口、7 天补零、隔离与软删 |

用户隔离（别人的数据返回 404 而不是 403）每个模块都有用例。
