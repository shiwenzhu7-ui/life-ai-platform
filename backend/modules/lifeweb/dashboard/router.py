"""首页接口 —— 只有一个 GET，没有写操作。

三件事都在别处，这里只做转手：
  参数（user_id / db）   由 core/deps.py 的两个 Depends 提供，绝不信请求里带的 user_id
  时间基准              here 取一次 now_beijing()，往下传；不在 queries 里各取一次
  形状                  response_model 写进 OpenAPI，顺带裁掉多余字段

没有 404/422 可言：用户没有数据时首页照样 200，数字为 0、列表为空。
唯一的失败是数据库不可用 → 500，由 core/errors.py 统一兜。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.time import now_beijing
from modules.lifeweb.dashboard import queries
from modules.lifeweb.dashboard.schema import DashboardOverview

# prefix 只写自己的路径段：/api/v1 由 app.py 挂载时统一加
router = APIRouter(prefix="/dashboard", tags=["首页"])


@router.get("/overview", response_model=DashboardOverview, summary="首页总览")
def dashboard_overview(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> DashboardOverview:
    """一次请求取齐首页所有数据：待办六数字、消费三金额、近 7 天、逾期与今日到期。

    时间基准在这里取：`now_bj` 决定「今天」「本月」「近 7 天」，
    同一个响应里的所有时间概念必须来自同一个 now。
    """
    return queries.overview(db, user_id, now_bj=now_beijing())
