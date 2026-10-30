"""v1 业务路由汇总。

按版本逐步接入（见 docs/接口设计.md §16）：
    V1.0  categories / tasks / expenses / dashboard
    V1.1  ai / shopping-lists
    V1.2  auth / admin
    V1.3  documents / async-tasks
    V1.4  budgets / life / diaries / analytics
"""

from fastapi import APIRouter

api_router = APIRouter()
