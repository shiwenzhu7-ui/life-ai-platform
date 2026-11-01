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

    @property
    def cors_origin_list(self) -> list[str]:
        """逗号分隔的字符串 → 列表，供 CORSMiddleware 使用。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内只构造一次（.env 只在启动时读一遍）。"""
    return Settings()


settings = get_settings()
