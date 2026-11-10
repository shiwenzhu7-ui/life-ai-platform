"""V1.2 B1：users 表补成全字段 + 给演示用户补上登录信息

Revision ID: b1users
Revises: a12pending
Create Date: 2026-11-10 14:50:00

背景：
    `docs/数据库设计.md:27` 当初的承诺是「V1.0 就把 password_hash / role / 配额
    等字段建好，V1.2 不用 ALTER TABLE」，但 V1.0 实际只建了 4 列
    （id / username / nickname / created_at，见迁移 a553bb786492 的说明：
    那两张表在接入 Alembic 之前就用 create_all() 建好了）。
    所以 V1.2 必须补一次列 —— 这就是本迁移。

做了什么：
  1. 补 10 列：email / avatar_url / password_hash / auth_source / keycloak_sub /
     role / status / daily_ai_quota / last_login_at / updated_at / deleted_at
     （nickname 和 created_at 本来就有）
  2. 建 3 个索引：uk_users_email (unique) / uk_users_keycloak_sub (unique) / idx_users_role
  3. 把演示用户 id=1 补成可登录账号：password_hash = bcrypt('demo1234')、
     daily_ai_quota=100（与 docs/数据库设计.md:73 的 seed 一致），
     并顺手把 nickname 从历史遗留的 'demo user' 改成文档里的 '演示用户'。

为什么先查表结构再 ALTER：
    本地库和别的开发机可能已经手工加过其中几列（V1.0 期间有人直接改过表）。
    加列前先查 information_schema，缺什么补什么，重复执行也不会报
    "Duplicate column name"。

回滚（downgrade）：
    删掉新增的列和索引。**注意**：downgrade 会丢掉所有用户的密码哈希，
    演示用户的密码也会被还原成「没设密码」。开发阶段无所谓，但别在生产库上跑。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1users"
down_revision: str | Sequence[str] | None = "a12pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 演示用户的口令。只在开发/演示环境使用；测试里也用这一个账号。
DEMO_PASSWORD = "demo1234"

# 预先算好的 bcrypt 哈希，避免迁移里为了跑 import 再装一遍 pwdlib。
# 明文 = demo1234，核对命令：
#   python -c "from pwdlib import PasswordHash; from pwdlib.hashers.bcrypt import BcryptHasher; \
#              print(PasswordHash((BcryptHasher(),)).verify('demo1234', '<hash>'))"
DEMO_PASSWORD_HASH = "$2b$12$tRFjoyN2mVZrSMGWxLVT5OmaCwnFNz4IszSc.0fkyvbHauEN6B4b6"

COLUMNS = {
    "email": "VARCHAR(120) NULL",
    "avatar_url": "VARCHAR(255) NULL",
    "password_hash": "VARCHAR(255) NULL",
    "auth_source": "ENUM('local','keycloak') NOT NULL DEFAULT 'local'",
    "keycloak_sub": "VARCHAR(64) NULL",
    "role": "ENUM('user','vip','admin') NOT NULL DEFAULT 'user'",
    "status": "ENUM('active','disabled') NOT NULL DEFAULT 'active'",
    "daily_ai_quota": "INT NOT NULL DEFAULT 50",
    "last_login_at": "DATETIME NULL",
    # updated_at 用 ON UPDATE CURRENT_TIMESTAMP：Python 里不用管，改行时 MySQL 自动刷
    "updated_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    "deleted_at": "DATETIME NULL",
}

# (索引名, 列, 是否唯一)
INDEXES = (
    ("uk_users_email", "email", True),
    ("uk_users_keycloak_sub", "keycloak_sub", True),
    ("idx_users_role", "role", False),
)


def _column_set() -> set[str]:
    """当前库里 users 表已有哪些列（用来跳过已经存在的那几列）。"""
    bind = op.get_bind()
    rows = bind.execute(sa.text("SHOW COLUMNS FROM `users`")).fetchall()
    return {row[0] for row in rows}


def _index_set() -> set[str]:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SHOW INDEX FROM `users`")).fetchall()
    return {row[2] for row in rows}


def upgrade() -> None:
    """补列 → 补索引 → 给演示用户写登录信息。"""
    existing_cols = _column_set()
    for name, ddl in COLUMNS.items():
        if name not in existing_cols:
            op.execute(f"ALTER TABLE `users` ADD COLUMN `{name}` {ddl}")

    existing_idx = _index_set()
    for name, column, unique in INDEXES:
        if name not in existing_idx:
            kind = "UNIQUE INDEX" if unique else "INDEX"
            op.execute(f"ALTER TABLE `users` ADD {kind} `{name}` (`{column}`)")

    # 演示用户 id=1：V1.0/V1.1 的所有业务数据都挂在它身上（user_id=1），
    # 补上密码后这份历史数据就能正常登录访问，不需要任何数据迁移。
    bind = op.get_bind()
    exists = bind.execute(sa.text("SELECT id FROM `users` WHERE id = 1")).scalar()
    if exists is not None:
        bind.execute(
            sa.text(
                "UPDATE `users` SET `password_hash` = :h, `daily_ai_quota` = 100, "
                "`auth_source` = 'local', `role` = 'user', `status` = 'active', "
                "`nickname` = '演示用户' "
                "WHERE id = 1 AND (`password_hash` IS NULL OR `password_hash` = '')"
            ),
            {"h": DEMO_PASSWORD_HASH},
        )


def downgrade() -> None:
    """删索引 → 删列。会丢掉密码哈希和角色信息。"""
    for name, _column, _unique in INDEXES:
        try:
            op.execute(f"ALTER TABLE `users` DROP INDEX `{name}`")
        except Exception:  # noqa: BLE001 —— 索引本来就不存在时允许回滚继续
            pass

    for name in COLUMNS:
        try:
            op.execute(f"ALTER TABLE `users` DROP COLUMN `{name}`")
        except Exception:  # noqa: BLE001 —— 列本来就不存在时允许回滚继续
            pass