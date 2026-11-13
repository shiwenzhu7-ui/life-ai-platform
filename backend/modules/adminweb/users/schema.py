"""管理后台的请求/响应字段。

与 lifeweb 各模块的 schema 不同的一点：这里的出参**会读别人的数据**
（管理员看所有用户），所以 `user_id` 是被查询的对象，不是"当前用户"。

`UserOut` 这个名字在 `modules/shared/auth/schema.py` 已经有了，是「我自己的资料」。
那边的字段是给普通用户看的（4 个），这里要给管理员看更多（注册时间、调用次数），
所以另起一个名字，不共用。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

# 必须与 users.model 里两个 ENUM 一字不差
AdminRole = Literal["user", "vip", "admin"]
AdminStatus = Literal["active", "disabled"]

# 三角色的能力说明：`/admin/roles` 的原样返回内容。
# 写死在这里而不是建一张 roles 表 —— 角色是代码里的 ENUM，多一张表就多一处失同步。
# 改了这里的文案，前端 `/roles` 页自动跟着变，不用动前端。
ROLE_CATALOG: list[dict] = [
    {
        "role": "user",
        "label": "普通用户",
        "summary": "默认角色，注册即得。",
        "abilities": [
            "使用待办、消费记录、日记、知识库等全部个人功能",
            "调用 AI 助手（受限流约束）",
            "进不了管理后台：/admin/* 一律 403",
        ],
    },
    {
        "role": "vip",
        "label": "VIP",
        "summary": "付费/受赠用户，V1.2 起不再有独立的额度差异。",
        "abilities": [
            "与普通用户能力相同",
            "AI 每日额度已于 2026-11-10 取消，VIP 不再享有额外次数",
            "进不了管理后台：/admin/* 一律 403",
        ],
    },
    {
        "role": "admin",
        "label": "管理员",
        "summary": "唯一能进管理后台的角色。",
        "abilities": [
            "查看全部用户，改角色、启用/禁用账号",
            "查看全站 AI 调用明细与合计",
            "个人功能与普通用户相同",
            "不能修改自己的角色，也不能禁用自己",
        ],
    },
]


class AdminUserOut(BaseModel):
    """用户列表里的一行。"""

    id: int
    username: str
    nickname: str | None
    email: str | None
    role: AdminRole
    status: AdminStatus
    auth_source: str
    created_at: datetime
    last_login_at: datetime | None
    # 累计 AI 调用次数（ai_usage_logs 的行数），列表页顺手展示，省一次点开
    usage_count: int = 0

    @classmethod
    def from_model(cls, user, *, usage_count: int = 0) -> "AdminUserOut":
        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            email=user.email,
            role=user.role,
            status=user.status,
            auth_source=user.auth_source,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
            usage_count=usage_count,
        )


class AdminUserUpdate(BaseModel):
    """改角色 / 改状态。两个字段都可选，至少要给一个。

    为什么不做成一次改很多用户：批量禁用是危险操作，真需要时再单开一个
    带二次确认的接口，不要顺手加进来。
    """

    role: AdminRole | None = None
    status: AdminStatus | None = None

    @model_validator(mode="after")
    def at_least_one(self) -> "AdminUserUpdate":
        if self.role is None and self.status is None:
            raise ValueError("没有需要修改的字段")
        return self


class RoleOut(BaseModel):
    """角色说明里的一张卡（只读，给管理员对照用）。"""

    role: AdminRole
    label: str
    summary: str
    abilities: list[str]


class UsageOut(BaseModel):
    """AI 调用明细里的一行。"""

    id: int
    user_id: int
    username: str | None
    purpose: str
    model: str
    token_in: int
    token_out: int
    cost_estimate: float
    latency_ms: int
    status: str
    error_code: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, row, *, username: str | None = None) -> "UsageOut":
        return cls(
            id=row.id,
            user_id=row.user_id,
            username=username,
            purpose=row.purpose,
            model=row.model,
            token_in=row.token_in,
            token_out=row.token_out,
            cost_estimate=float(row.cost_estimate or 0),
            latency_ms=row.latency_ms,
            status=row.status,
            error_code=row.error_code,
            created_at=row.created_at,
        )


class UsageSummaryOut(BaseModel):
    """筛选范围内的合计 —— 放列表接口同一个响应里，省一次请求。"""

    token_in: int = 0
    token_out: int = 0
    cost_estimate: float = 0.0
    calls: int = 0
    failed: int = 0


class UsagePageOut(BaseModel):
    """usage 明细的分页响应：多带一个合计。

    不能直接用 `Page[UsageOut]`：合计是这次筛选的汇总，翻页时不该变，
    前端拿它画「今日共花 X 元」。
    """

    items: list[UsageOut]
    total: int
    page: int
    size: int
    pages: int
    summary: UsageSummaryOut