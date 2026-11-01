"""共享地基：配置、数据库、依赖、时间、错误。

这一层全站只有一份，谁都不许复制。业务代码一律靠 import 用它。
"""

from core.config import settings

__all__ = ["settings"]
