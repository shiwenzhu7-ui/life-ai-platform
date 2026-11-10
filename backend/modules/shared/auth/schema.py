"""auth 的出入参（V1.2）。

三条硬规则：
    1. 出参里**永远不出现 password_hash**。字段显式列出，不用「模型全量转字典」。
    2. 写接口用 model_config = ConfigDict(extra="forbid")：请求体多带一个 role，
       直接 422，而不是静默忽略 —— 静默忽略会让「我明明传了 role 怎么没生效」
       变成下一个 bug。
    3. 校验只做「格式对不对」；「重不重、能不能登」是 service 的事。
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 用户名：字母、数字、下划线，3~50 位。
# 不允许中文/空格：登录框和 URL 里都要用，少一类字符就少一类头疼。
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,50}$")

# bcrypt 只认前 72 字节。这里按**字节**卡上限，不按字符 ——
# 24 个汉字就是 72 字节，25 个就超了。
_PASSWORD_MAX_BYTES = 72


def _check_password_bytes(v: str) -> str:
    if len(v.encode("utf-8")) > _PASSWORD_MAX_BYTES:
        raise ValueError(f"密码过长：最多 {_PASSWORD_MAX_BYTES} 字节（约 24 个汉字）")
    return v


class RegisterIn(BaseModel):
    """注册请求。

    没有 role 字段 —— 角色只能由后端决定。前端就算传了也会因为 extra="forbid" 被拒。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=50, examples=["alice"])
    # 邮箱可选。空串会被归一成 None：MySQL 的唯一索引允许多个 NULL，
    # 但不允许多个空串 —— 不归一的话，第二个不填邮箱的人会撞键。
    email: str | None = Field(default=None, max_length=120, examples=["a@b.com"])
    password: str = Field(min_length=8, max_length=72, examples=["12345678"])

    @field_validator("username")
    @classmethod
    def _username_charset(cls, v: str) -> str:
        v = v.strip()
        if not _USERNAME_RE.match(v):
            raise ValueError("用户名只能包含字母、数字、下划线，长度 3~50")
        return v

    @field_validator("email")
    @classmethod
    def _email_normalize(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        # 只做最基本的形状检查：真正权威的校验是「能不能收到邮件」，V1.2 不发邮件，
        # 所以只拦明显不对的，不抄一份冗长的正则来假装严格。
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("邮箱格式不正确")
        return v.lower()  # 统一小写，避免 Alice@x.com 和 alice@x.com 绕过去重

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)


class LoginIn(BaseModel):
    """登录请求。故意不设 min_length 等约束：登录失败一律 401，
    不能因为「密码太短」返回 422 —— 那等于告诉攻击者口令规则。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=50, examples=["alice"])
    password: str = Field(max_length=128, examples=["12345678"])

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        # 超 72 字节的口令不可能注册成功过，直接放过到 service 去比对（会失败），
        # 但也不能让它进 bcrypt 触发异常。
        return v


class UserBrief(BaseModel):
    """注册响应 / 登录响应里的用户摘要（文档 §3 的字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    role: str


class UserOut(BaseModel):
    """GET /auth/me 的响应。

    六个字段，一个不多：password_hash、keycloak_sub、deleted_at 这类
    内部列不出去。多返回一个字段不会让功能更好用，只会多一个泄露面。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str | None = None
    role: str
    daily_ai_quota: int
    ai_used_today: int


class TokenOut(BaseModel):
    """登录响应（文档 §3）。expires_in 是秒数，前端拿它做「还剩多久」提示。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserBrief