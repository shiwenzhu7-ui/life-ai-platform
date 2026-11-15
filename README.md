# LifeAI 智能生活管理平台

基于 **Vue 2 + FastAPI + MySQL + Milvus** 的前后端分离生活管理平台：用自然语言记账、建待办，用个人知识库做 RAG 问答，用 Keycloak 做多应用单点登录。

> **项目状态：已归档，不再新增功能。** 后端 **820 个测试全部通过**。

## 界面

| 首页 | 待办 |
|---|---|
| ![首页](docs/demo/dashboard.png) | ![待办](docs/demo/tasks.png) |

| 消费 | 知识库 |
|---|---|
| ![消费](docs/demo/expenses.png) | ![知识库](docs/demo/knowledge.png) |

## 1. 项目背景

日常生活数据分散在待办软件、记账软件、备忘录和聊天工具里，用户需要反复切换应用，录入过程繁琐。

LifeAI 把这些功能整合到一个平台，让用户直接用自然语言操作：

- "明天下午三点提醒我去超市。"
- "今天午饭花了 28 元，帮我记到账本里。"
- "查询本月在餐饮上花了多少钱。"
- "从我上传的健身资料中找一个适合新手的训练方案。"

## 2. 核心功能

| 模块 | 说明 |
|---|---|
| 待办事项 | 增删改查、分类/优先级/截止时间、多维筛选、排序 |
| 消费记账 | 收支记录、月度汇总、类别占比、筛选分页 |
| 购物清单 | 清单与明细，支持 AI 批量添加 |
| AI 生活助手 | 10 个工具、写入需确认、只读直接回答 |
| 个人知识库 RAG | PDF/DOCX/TXT 上传、切分、向量化、带来源问答 |
| 用户系统 | JWT 登录、用户数据隔离、三级角色 |
| 管理后台 | 用户管理、AI 调用明细、角色说明、403 页 |
| Keycloak SSO | 两个前端单点登录 + 统一退出（BFF 模式） |
| 首页仪表盘 | 待办概览、本月消费、逾期提醒 |

## 3. 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 2、Vue Router 3、Vuex、Axios、Element UI |
| 后端 | Python 3.11、FastAPI、Pydantic v2 |
| ORM / 迁移 | SQLAlchemy 2.0、Alembic |
| 关系库 | MySQL 8.x |
| 向量库 | Milvus（`pymilvus` 官方客户端） |
| 嵌入模型 | `bge-small-zh-v1.5`（fastembed / ONNX，512 维，不需要 torch） |
| 大模型 | 任意 OpenAI 兼容接口 |
| 认证 | JWT（HS256）+ Keycloak 26 / OIDC / JWKS |
| 测试 | pytest（820 个用例，全部进程内，不联网不花钱） |

> **没有使用 LangChain。** 工具层是自写的函数 + Pydantic Schema，文本切分也是自写的字符切分（512 字符 / 64 重叠）。原计划里写了 LangChain，实际实现时去掉了 —— 只为调几个函数引入一整套框架不值。

## 5. 系统架构

```text
                     Keycloak 26（OIDC / JWKS）
                      /                      \
             life-web（8080）          admin-web（8082）
         生活管理端 / SSO 登录        管理后台 / SSO 登录
                      \                      /
                       \                    /
                        FastAPI（8000）
              JWT 校验 · 权限控制 · 用户隔离
                    /          |          \
                 MySQL      自研工具层    Milvus
                         10 个 AI 工具
                               |
                       OpenAI 兼容大模型
```

### SSO 采用 BFF 模式

浏览器只跟本项目后端打交道，`client_secret` 和换 token 都在后端完成。后端拿到 Keycloak 的 `id_token`、校验通过后，**签发原有的本地 JWT**。

这样做的好处：`require_role`、两级管理员、前端请求拦截器**一行都不用改**。Keycloak 只回答「你是谁」，角色仍由本地 `users.role` 决定。

```text
/auth/sso/login?app=life-web
  → 302 到 Keycloak 登录页
  → /auth/sso/callback（换 token、验 id_token 签名、落库、签本地 JWT）
  → 302 回发起方自己的 /sso/callback?token=…
  → 前端读完立即把地址栏洗掉
```

- **多应用**：`OIDC_CLIENTS=life-web=life-web:<secret>;admin-web=admin-web:<secret>`，加第三个前端只改 `.env`
- **静默探测**：登录页打开时带 `prompt=none` 问一次 Keycloak，已登录就无感进入；未登录返回 `login_required`，安静显示表单不弹红字
- **统一退出**：`id_token` 存 `sessionStorage`，退出时走 Keycloak `end_session` 清会话，否则关掉本地 JWT 后静默探测又会把人登回来

## 6. AI 助手设计

### 10 个工具，写入必须确认

| 工具 | 类型 | 用途 |
|---|---|---|
| `query_tasks` | 只读 | 查询当前用户待办 |
| `query_expenses` | 只读 | 查询消费数据 |
| `search_personal_documents` | 只读 | 检索个人知识库 |
| `create_task` | 写入 | 创建待办 |
| `update_task` | 写入 | 修改待办 |
| `delete_task` | 写入 | 删除待办 |
| `create_expense` | 写入 | 新增消费 |
| `update_expense` | 写入 | 修改消费 |
| `delete_expense` | 写入 | 删除消费 |
| `add_shopping_item` | 写入 | 往清单加物品 |

**只读工具立刻执行**，结果回灌给模型继续说话；**写入工具只出确认卡**，落 `ai_pending_actions` 表，用户点确认才写库。

### 三条安全护栏

1. **用户 ID 由服务端注入**，模型无权提供。工具函数签名里根本没有这个参数，模型编也编不出来。
2. **写入必须先看图**。`create_task` / `create_expense` 等返回结构化预览，用户确认后才执行；确认带幂等，重复点不会记两笔。
3. **改删要凭据**。`task_id` 只能来自 `query_tasks` 的返回，模型不能凭空猜一个 id。

### 一个真实踩到的坑

> Milvus 上 `insert` 之后 0 秒，`get` / `query` / `search` 会一起返回空，显式指定 `consistency_level="Strong"` 也不行，约 1 秒后才有数据。`search_chunks` 因此在首次检索为空时等 0.8 秒再查一次。

## 7. 个人知识库 RAG

实现要点：

- 支持 PDF / DOCX / TXT，单文件 ≤ 20MB，按 `sha256` 去重
- 正文**只存在 MySQL 的 `document_chunks` 表**，Milvus 只存向量 + `user_id` / `document_id` 两个过滤字段 —— 不把原文塞进向量库，删除和改切分都好办
- 嵌入用本地 `bge-small-zh-v1.5`（fastembed / ONNX），第一次跑会下载模型，之后离线可用
- 检索**强制带 `user_id` 过滤**，top_k=5，从根上杜绝跨用户检索
- 回答附带 `references[]`（文档名 / 页码 / 片段 / 相似度），前端渲染来源卡片；引用**不落库**，只存在于当次响应

## 8. 权限模型

| 角色 | 说明 |
|---|---|
| `user` | 普通用户，管自己的数据 |
| `vip` | 同 user（AI 每日额度已于 2026-11-10 取消，所有人不限次数） |
| `admin` | 管理员，可进管理后台 |

管理员分两级：

- **根管理员**：全站唯一（`ADMIN_ROOT_USER_ID` 或库中 `id` 最小的 admin），唯一能授予/撤销管理员，且谁都不能改他。换根只能改库或 `.env`，界面无入口。
- **普通管理员**：只能改普通用户和 VIP。

**所有权限判定在后端。** 整个 `/admin` 路由组挂 `require_role("admin")`，前端守卫只负责体验；隐藏按钮不是安全边界。

## 9. 数据库

13 张表，核心：

```text
users                用户
tasks                待办（硬删）
expenses             消费（硬删）
budgets              预算
shopping_lists       购物清单
shopping_items       购物明细
documents            知识库文档（硬删）
document_chunks      文档分块（正文唯一副本）
chat_sessions        AI 会话（软删，deleted_at）
chat_messages        AI 消息
ai_pending_actions   AI 待确认动作
ai_usage_logs        AI 调用计量（仅成本统计，不限流）
```

字段级定义见 [数据库设计](docs/数据库设计.md)。

## 10. 项目结构

```text
life-ai-platform/
├── backend/                 FastAPI 后端
│   ├── modules/lifeweb/     业务模块
│   │   ├── ai/              AI 工具层、确认机制、会话
│   │   ├── tasks/           待办
│   │   ├── expenses/        消费
│   │   ├── shopping/        购物
│   │   ├── knowledge/       知识库 RAG
│   │   └── dashboard/       首页统计
│   ├── alembic/             数据库迁移
│   └── tests/               820 个测试
├── life-web/                生活管理端（Vue 2，8080）
├── admin-web/               管理后台（Vue 2，8082）
├── analytics-web/           数据分析端（空目录，未开工）
├── keycloak/                Realm 配置（空目录）
├── deploy/                  部署配置（空目录）
├── docs/                    设计文档
└── scripts/                 开发辅助脚本
```

## 11. 本地运行

### 前置

- Python 3.11+
- Node.js 18+
- MySQL 8.x
- Milvus 2.x（知识库功能需要，不跑也能启动）

### 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

cp .env.example .env            # 然后填你自己的密钥
alembic upgrade head
uvicorn main:app --reload
```

`.env` 关键项：

```ini
DATABASE_URL=mysql+pymysql://root:password@127.0.0.1:3306/lifeai
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=

# SSO 全部留空即关闭 SSO，不影响账号密码登录
OIDC_ISSUER=
OIDC_CLIENTS=
OIDC_FRONTEND_CALLBACKS=
OIDC_REDIRECT_URI=
```

### 前端

```bash
cd life-web && npm install && npm run serve     # 8080
cd admin-web && npm install && npm run serve    # 8082
```

### 测试

```bash
cd backend
pytest                     # 820 passed，约 3.5 分钟
```

## 12. 版本历程

| 版本 | 内容 | 状态 |
|---|---|---|
| V1.0 | 待办 + 消费的增删改查、首页统计 | ✅ |
| V1.1 | AI 助手、工具层、确认机制 | ✅ |
| V1.2 | JWT 用户系统、数据隔离 | ✅ |
| V1.3 | Milvus RAG 个人知识库 | ✅ |
| V1.4 | 购物清单、管理后台、两级管理员 | ⚠️ 部分（生活记录/图表/报表未做） |
| V2.0 | Keycloak SSO、静默登录、统一退出 | ⚠️ 部分（Docker 部署、analytics-web 未做） |

## 13. 文档

- [需求规格说明书](需求规格说明书.md) — 完整需求（按计划写，部分未实现，以上文「未完成」清单为准）
- [项目规划](docs/项目规划.md) — 技术决策、阶段任务、风险登记
- [数据库设计](docs/数据库设计.md) — 表结构字段级定义（文档按 23 张表设计，实际落地 13 张）
- [接口设计](docs/接口设计.md) — 接口、错误码与权限矩阵
- [页面设计](docs/页面设计.md) — 路由表与交互规范
- [工程规范](docs/工程规范.md) — 目录、依赖、环境变量、Git 工作流
- [AI 设计](docs/AI设计.md) — 工具 Schema、上下文注入、确认机制、RAG 参数

## 14. 安全说明

- `.env` 与所有密钥**从不进版本库**，仓库内只有 `.env.example`
- `.git/hooks/pre-push` + `scripts/pre_push_secret_scan.py` 在每次推送前扫描 `sk-` 密钥、密钥赋值、复合 client 配置，命中即拒绝推送
- 检索与所有业务查询都按 `user_id` 强制隔离
- 密码用 bcrypt 哈希存储，JWT 不落 localStorage 以外的持久位置

> 本项目为学习实践项目，未经过生产级安全审计，**请勿直接用于生产环境**。

---

**License**：未指定许可证，保留所有权利。