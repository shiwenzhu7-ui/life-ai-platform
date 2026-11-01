"""用户模块（三端共用）。

表：users
归属判断：写入方是认证（V1.2 注册/登录）和管理后台（改角色），
所以放 shared，不放在任何一个端下面。
"""

from modules.shared.users.model import User

__all__ = ["User"]
