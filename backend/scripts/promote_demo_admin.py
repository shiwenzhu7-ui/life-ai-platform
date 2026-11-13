"""把 demo 账号提升为管理员（一次性脚本）。

为什么写脚本而不是手工 UPDATE：`role` 是 ENUM，写错值会静默变成空字符串，
这个脚本会先查再改、再读回来确认。

顺带把另一个账号「111」（user_id=5）保持普通用户，用来验证门禁。
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select

from core.db import SessionLocal
from modules.shared.users.model import User

with SessionLocal() as db:
    rows = db.execute(select(User).order_by(User.id)).scalars().all()
    print("改之前的角色：")
    for row in rows:
        print(f"  id={row.id} username={row.username} role={row.role} status={row.status}")

    demo = db.execute(select(User).where(User.username == "demo")).scalar_one_or_none()
    if demo is None:
        raise SystemExit("找不到 demo 账号，先跑一次登录或在种子数据里建一个")

    demo.role = "admin"
    db.commit()

    db.refresh(demo)
    print(f"\ndemo 现在：role={demo.role} status={demo.status}")
    if demo.role != "admin":
        raise SystemExit("改失败了，role 不是 admin")