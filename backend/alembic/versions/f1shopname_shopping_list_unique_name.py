"""V1.4：购物清单加 (user_id, name) 唯一约束

Revision ID: f1shopname
Revises: e1harddelete
Create Date: 2026-11-12 11:10:00

为什么加：

`add_shopping_item` 工具要「清单不存在就建一张」，靠的是
`shopping.crud.find_or_create_default_list()`。那张函数的并发降级说明里
留了一句话：两个请求同时找不到会各建一张同名清单。

唯一索引是唯一能根治的办法 —— 应用层的「先查再插」在并发下必然漏，
数据库的唯一约束才是真正的保证。加了它之后，撞车的那个请求会拿到
`IntegrityError`，代码里捕获它、回头再查一次，就拿到并发对手建的那张。

本迁移同时覆盖**页面手动建清单**（POST /shopping-lists）：
以前同一个人能建两张同名清单，现在第二张会被 409 拒掉。这是有意的行为变化。

⚠️ 遇到历史重复数据时**直接报错，不自动改数据**：

升级前库里若已有同名清单（同一个 user_id 下 name 相同），本迁移抛
RuntimeError 并列出冲突的清单 id，让加索引失败。理由是「自动合并 / 自动改名
/ 自动归档」都是不可逆地改写用户数据，该由人来决定留哪张、另一张怎么办。

处理办法（手工，在 develop 库上执行）：

    -- 1. 看冲突：每个 user_id + name 组合有哪几张
    SELECT user_id, name, GROUP_CONCAT(id ORDER BY id) AS ids, COUNT(*) AS n
    FROM shopping_lists GROUP BY user_id, name HAVING n > 1;

    -- 2. 决定留哪张（通常留 id 最小的），其余改名或归档
    UPDATE shopping_lists SET status = 'archived' WHERE id IN (...);

    -- 3. 重新跑 alembic upgrade head

幂等：索引已存在就直接跳过，重复执行不会报错。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1shopname"
down_revision: str | Sequence[str] | None = "e1harddelete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "shopping_lists"
_INDEX = "uk_shopping_lists_user_name"
_COLUMNS = ["user_id", "name"]


def _index_exists(table: str, index: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
        ),
        {"t": table, "i": index},
    ).scalar()
    return bool(row)


def _duplicates() -> list[tuple[int, str, str, int]]:
    """返回 [(user_id, name, 清单 id 串, 张数)]，为空表示没有冲突。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT user_id, name, GROUP_CONCAT(id ORDER BY id) AS ids, COUNT(*) AS n "
            f"FROM `{_TABLE}` GROUP BY user_id, name HAVING COUNT(*) > 1"
        )
    ).all()
    return [(int(r[0]), str(r[1]), str(r[2]), int(r[3])) for r in rows]


def upgrade() -> None:
    if _index_exists(_TABLE, _INDEX):
        return

    dupes = _duplicates()
    if dupes:
        detail = "; ".join(
            f"user_id={uid} name={name!r} ids=[{ids}] ×{n}" for uid, name, ids, n in dupes
        )
        raise RuntimeError(
            "shopping_lists 里已有同名清单，无法建唯一索引 "
            f"{_INDEX}。请先手工处理（归档或改名），冲突明细：{detail}。"
            "处理语句见本迁移文件开头的 docstring。"
        )

    op.create_index(_INDEX, _TABLE, _COLUMNS, unique=True)


def downgrade() -> None:
    if not _index_exists(_TABLE, _INDEX):
        return
    op.drop_index(_INDEX, table_name=_TABLE)