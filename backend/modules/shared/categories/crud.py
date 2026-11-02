"""categories 的数据库访问。

V1.0 只有读。写（用户自建二级分类）在 V1.4，那时在这里加 create / rename。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.shared.categories.model import Category


def list_visible(db: Session, user_id: int, *, scope: str | None = None) -> list[Category]:
    """列出当前用户能看到的分类：系统预置（owner_user_id=0）+ 本人自建。

    scope 传 None 时返回全部用途域 —— 前端一次拿全，切页面不用再请求。
    排序：先按用途域（同一批分类挨在一起），再按层级，最后按 sort_order，
    末位加 id 兜底，保证多次请求顺序稳定（排序键有可能重复）。
    """
    conds = [
        Category.deleted_at.is_(None),
        # 系统预置 + 自己建的；别人的自定义分类不可见
        Category.owner_user_id.in_((0, user_id)),
    ]
    if scope is not None:
        conds.append(Category.scope == scope)

    stmt = (
        select(Category)
        .where(*conds)
        .order_by(
            Category.scope,
            Category.parent_id,
            Category.sort_order,
            Category.id,
        )
    )
    return list(db.scalars(stmt).all())


def exists_visible(db: Session, user_id: int, category_id: int, *, scope: str) -> bool:
    """这个分类存在、属于本人或系统、且确实用在 scope 这个域里吗？

    三条件缺一不可：
      存在       —— 否则前端传个不存在的 ID 也能存进待办，列表里显示空分类
      域匹配     —— 否则「餐饮」（expense 域）会被塞进待办，统计口径立刻乱掉
      归属可见   —— 否则能拿别人的私有分类 ID 试探出「它存在」
    """
    stmt = (
        select(Category.id)
        .where(
            Category.id == category_id,
            Category.scope == scope,
            Category.deleted_at.is_(None),
            Category.owner_user_id.in_((0, user_id)),
        )
        .limit(1)
    )
    return db.scalar(stmt) is not None


def name_map(db: Session) -> dict[int, str]:
    """{分类 id: 分类名}，给列表接口一次性取名字用。

    为什么不用 JOIN：一条待办 JOIN 出名字，每行都多带 32 字节；
    而分类总共 41 条，一次查完也就一个键值对字典，
    既避免 N+1，又不用改查询结构和分页逻辑。
    """
    rows = db.execute(select(Category.id, Category.name).where(Category.deleted_at.is_(None)))
    return {category_id: name for category_id, name in rows}
