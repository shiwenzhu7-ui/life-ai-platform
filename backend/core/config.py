"""应用配置 —— 整个后端唯一读取 .env 的地方。

其余模块一律 `from core.config import settings`，
不允许再写 `os.getenv(...)`：同一个配置项散落在多处，改一处漏一处。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录：本文件在 backend/core/config.py，往上两层就是 backend/
BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        # .env 里还有 JWT_*、KEYCLOAK_* 等后面版本才用的键，忽略而不是报错
        extra="ignore",
        case_sensitive=False,  # DATABASE_URL 与 database_url 等价
    )

    # ---------- 应用 ----------
    app_name: str = "LifeAI"
    app_env: str = "dev"
    debug: bool = True
    port: int = 8000
    api_v1_prefix: str = "/api/v1"

    # ---------- 数据库 ----------
    # 默认空字符串：缺失时由 core.db 在导入阶段明确报错，
    # 而不是拿一个假默认值去连库、最后报个看不懂的连接错误
    database_url: str = ""

    # ---------- 跨域 ----------
    cors_origins: str = "http://localhost:8080,http://localhost:8081,http://localhost:8082"

    # ---------- 认证（V1.2）----------
    # 密钥只放 .env（已 gitignore），绝不打日志、绝不进响应体（NFR 11.1）。
    # 默认空串：没配时 core.security 拒绝签发令牌，而不是拿空串签出一个
    # 「任何人都能伪造」的通行证 —— 安静地不安全工作，比大声失败更危险。
    jwt_secret: str = ""
    # 只允许对称算法；V2.0 换 Keycloak 时改成 RS256 + JWKS 公钥验签
    jwt_algorithm: str = "HS256"
    # 令牌有效期（分钟），从签发那一刻算起，不随使用续期。
    # 接口返回的 expires_in = 本值 × 60（docs/接口设计.md §3 的 7200）
    jwt_expire_minutes: int = 120

    # ---------- LLM（V1.1 AI 助手）----------
    # 密钥只从环境变量读、绝不落库也绝不打进日志（NFR 11.1）。
    # 默认空串：没配 key 时 AI 接口应当明确报「未配置」，而不是拿假 key 去调、
    # 最后收到一个 401 让人误以为是网络问题。
    llm_api_key: str = ""
    # OpenAI 兼容端点；DeepSeek 官方也吃这个前缀
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    # 计价单价（元 / 百万 token），用来算 ai_usage_logs.cost_estimate。
    # 默认 0 = 只记 token 不估钱：没核实过官网价目表之前，写一个拍脑袋的数字
    # 比记 0 更糟（看起来像账单，实际是猜的）。要用就按官网当天价格填 .env。
    llm_price_in_per_mtok: float = 0.0
    llm_price_out_per_mtok: float = 0.0
    # 工具调用总开关（A5 起）。关掉之后：请求里不发 tools、提示词里不讲工具规则，
    # 退回 A4 的「纯聊天」形态 —— 一句话就能把「AI 乱建待办」这类事故刹住，
    # 不用回滚代码。默认开。
    llm_tools_enabled: bool = True

    # ---------- 知识库 / RAG（V1.3）----------
    # 向量库跑在虚拟机里的 Milvus standalone（Windows 上没装 docker，
    # 而 Milvus 常驻要 1–2GB，本机内存本来就紧）。默认值指向开发环境的那台；
    # 换机器就改 .env 的 VECTOR_URI。
    vector_uri: str = "http://192.168.88.134:19530"
    vector_db: str = "lifeai"
    vector_collection: str = "kb_chunks"
    # 嵌入模型：本地 fastembed 缓存里已有 ONNX 权重，不用联网下载、不装 torch。
    # 换成别的模型必须同时改 embedding_dim —— 维度变了，旧集合里的向量全部作废。
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    # 模型缓存目录。空串 = 用 fastembed 的默认位置；
    # 本机把权重放在 D 盘（C 盘只剩 ~50GB），所以 .env 里显式指过去。
    embedding_cache_dir: str = ""
    # 进程内常驻推理会话。首屏第一次检索要加载 ~0.4s 的模型，
    # 之后每条向量只要 2ms；但常驻本身占几十 MB，关掉就是每次请求都重新加载。
    embedding_warmup: bool = True

    # ---------- 文档切分（V1.3）----------
    # 字符数而非 token 数：中文按字切比分词器结果更稳，也不依赖模型自带的 tokenizer。
    kb_chunk_size: int = 512
    kb_chunk_overlap: int = 64
    # 上传大小上限（字节）。与 docs/接口设计.md §4 的 20MB 对齐。
    kb_max_upload_bytes: int = 20 * 1024 * 1024
    # 单次检索返回的片段数（docs/项目规划.md S4：top_k=5）
    kb_top_k: int = 5

    @property
    def llm_ready(self) -> bool:
        """key 是否已配置 —— AI 接口用它决定是不是直接拒绝服务。"""
        return bool(self.llm_api_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        """逗号分隔的字符串 → 列表，供 CORSMiddleware 使用。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内只构造一次（.env 只在启动时读一遍）。"""
    return Settings()


settings = get_settings()
