"""应用配置：进程内唯一读取环境变量的地方。

其余模块一律通过 `from app.core.config import settings` 访问配置，
不允许在业务代码里直接 `os.getenv`。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录，用于定位 .env
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 应用
    app_name: str = "LifeAI"
    app_env: str = "dev"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    tz: str = "Asia/Shanghai"

    # 数据库
    database_url: str = "mysql+pymysql://root:root@127.0.0.1:3306/lifeai?charset=utf8mb4"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # 跨域
    cors_origins: str = "http://localhost:8080,http://localhost:8081,http://localhost:8082"

    # 认证（V1.2 启用）
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 120

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
