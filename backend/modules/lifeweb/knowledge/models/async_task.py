"""async_tasks 表（后台任务进度）。

设计依据 `docs/数据库设计.md` §3.21，迁移 `d1knowledge`。

V1.3 用它记录文档处理进度：
上传接口把文档置 `pending` 就立刻返回，真正的解析/切块/嵌入在 FastAPI
`BackgroundTasks` 里跑（不引入 Celery：单机练习项目，多一个 broker 是纯负担）。

**为什么还要这张表，而不是只看 documents.status？**
`documents.status` 只能说「卡在哪一步」，这张表能说「第几次尝试、还有几次机会、
上一步花了多久、为什么失败」。补偿重试（`attempts` 上限 3）要有地方记账，
不然失败了就永远是失败，用户只能删了重传。

状态机：

    queued ──▶ running ──▶ success
       └──────── │ ─────▶ failed（attempts 未满时可由补偿任务重新置 queued）

`progress` 是给人看的粗粒度百分比（0/10/40/90/100），不追求精确 ——
真实的进度要靠在代码里埋回调，收益不值。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

ASYNC_TASK_TYPES = ("doc_parse", "doc_embed", "report_gen", "cleanup", "remind")
ASYNC_STATUSES = ("queued", "running", "success", "failed")

# 重试上限。设计文档写的「上限 3」，这里作为常量暴露出来，
# 补偿逻辑不要在别处再写一个魔数
MAX_ATTEMPTS = 3


class AsyncTask(Base):
    __tablename__ = "async_tasks"
    __table_args__ = (
        # 「哪些任务在排队/跑了很久」—— 补偿任务与运维排查都用它
        Index("idx_async_status", "task_type", "status"),
        # 「我这个用户的文档处理历史」
        Index("idx_async_user", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    task_type: Mapped[str] = mapped_column(
        Enum(*ASYNC_TASK_TYPES, name="async_task_type"), nullable=False
    )
    # 关联的业务对象：doc_parse / doc_embed 都是 documents.id。
    # 可空是因为 report_gen 这类任务没有单一目标
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        Enum(*ASYNC_STATUSES, name="async_status"), nullable=False, server_default="queued"
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # 阶段说明，给人看的一句话，如「正在解析第 3/12 页」
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AsyncTask id={self.id} type={self.task_type} status={self.status}>"