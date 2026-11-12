"""购物的数据访问层 —— 只写 SQL，不写业务规则。

铁律（每个查询都必须满足）：必须带 user_id 过滤 —— 数据隔离的最后一道防线。
这一点和 tasks/crud.py 完全一样，没有例外。

删除是**硬删**（DELETE 语句，行真的消失）。`shopping_*` 两张表虽然还留着
`deleted_at` 列，但和 categories / users 一样「列保留、暂未使用」——
见 docs/数据库设计.md §2.1。所以查询侧不需要「排除已删除」的条件。

与 tasks/crud.py 的两处结构性差异：

  1. **父子两张表**。明细可以按 `user_id` 直接查（表里有冗余列），
     但「往某张清单里加一条」必须先确认那张清单属于本人 —— 否则
     知道一个清单 id 就能往别人清单里塞数据。crud 层只提供
     `get_list_for_user`，由 router 负责「取不到就 404」。

  2. **清单列表要带两个计数**。用一条 `LEFT JOIN + GROUP BY` 一次算完，
     不用 N+1 查询。**必须是 LEFT JOIN**：新清单一条明细都没有，
     用 INNER JOIN 它会直接从列表里消失。

写操作默认在本层 commit：现在都是单表写入（删清单是唯一的两步，
但两步在同一个事务里）。`commit=False` 的参数留给 V1.4 第 3 步的
AI 确认卡片复用 —— 那时写入要和「卡片置 confirmed」同事务。
"""

from typing import Sequence

from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.orm import Session

from core.time import now_utc
from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList
from modules.lifeweb.shopping.schema import ItemCreate, ItemUpdate, ListCreate, ListUpdate

# 清单列表的排序：新清单排前面，同一秒建的用 id 兜底（数据库不保证稳定顺序）
_LATEST_FIRST = (ShoppingList.created_at.desc(), ShoppingList.id.desc())

# 明细排序：没买的在前，再按 sort_order，最后 id 兜底。
#
# 为什么不直接按 is_purchased 升序：MySQL 里 0 < 1，升序天然把「没买」(0)
# 排在前面，正好是想要的，不用写 CASE。sort_order 留给以后拖动排序用，
# 现在全是 0，所以实际顺序就是「没买的按 id 升序，买过的按 id 升序」。
_ITEM_ORDER = (ShoppingItem.is_purchased.asc(), ShoppingItem.sort_order.asc(), ShoppingItem.id.asc())


def _limit_offset(page: int, size: int) -> tuple[int, int]:
    """(limit, offset)，顺便把非法值夹回合理区间。

    校验已经在 router 的 Query(ge=1) 做过了，这里再夹一次是最后一道：
    offset 若为负数 SQL 会直接报错（→500）。
    """
    size = min(max(1, int(size)), 100)
    page = max(1, int(page))
    return size, (page - 1) * size


# ---------------------------------------------------------------- 清单


def _list_base(user_id: int) -> Select:
    """清单查询的公共起点：限本人。"""
    return select(ShoppingList).where(ShoppingList.user_id == user_id)


def _counts_subquery(user_id: int):
    """按 list_id 汇总的 {总条数, 未购条数}。

    单独抽成函数是因为两个查询要用它两次（列表页、详情页），
    写两遍 SQL 早晚会有一遍改了另一遍没改。

    `SUM(CASE WHEN ...)` 而不是 `count(*) - sum(is_purchased)`：
    后者在 is_purchased 存了除 0/1 以外的值时会算错，前者只认「等于 0」。
    """
    return (
        select(
            ShoppingItem.list_id.label("list_id"),
            func.count(ShoppingItem.id).label("item_count"),
            func.sum(
                case((ShoppingItem.is_purchased == 0, 1), else_=0)
            ).label("unpurchased_count"),
        )
        .where(ShoppingItem.user_id == user_id)
        .group_by(ShoppingItem.list_id)
        .subquery()
    )


def list_lists(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    size: int = 20,
    status: str | None = "active",
) -> tuple[list[tuple[ShoppingList, int, int]], int]:
    """清单列表，每项带 (清单, 总条数, 未购条数)。

    `status=None` 表示不筛选（归档的也一并返回）。

    返回元组列表而不是 ORM 对象列表：计数是 join 出来的额外列，
    往 ORM 对象上挂临时属性会让别处的代码也看着它 ——
    显式返回元组，谁用谁拆。

    **必须是 LEFT JOIN**：新清单一条明细都没有，用 INNER JOIN
    它会直接从列表里消失。`func.coalesce(..., 0)` 负责把 NULL 变成 0。
    """
    limit, offset = _limit_offset(page, size)

    counts = _counts_subquery(user_id)

    base = _list_base(user_id)
    if status is not None:
        base = base.where(ShoppingList.status == status)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = (
        base.outerjoin(counts, counts.c.list_id == ShoppingList.id)
        .add_columns(
            func.coalesce(counts.c.item_count, 0),
            func.coalesce(counts.c.unpurchased_count, 0),
        )
        .order_by(*_LATEST_FIRST)
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(stmt).all()
    return [(row[0], int(row[1]), int(row[2])) for row in rows], int(total)


def get_list_for_user(db: Session, user_id: int, list_id: int) -> ShoppingList | None:
    """按 ID 取一张清单。

    不属于该用户时返回 None，对外统一表现为 404 ——
    403 等于告诉对方「这张清单存在，只是你没权限」。
    """
    return db.scalar(_list_base(user_id).where(ShoppingList.id == list_id))


def list_counts(db: Session, user_id: int, list_id: int) -> tuple[int, int]:
    """单张清单的 (总条数, 未购条数)，给详情接口用。"""
    row = db.execute(
        select(
            func.count(ShoppingItem.id),
            func.sum(case((ShoppingItem.is_purchased == 0, 1), else_=0)),
        ).where(ShoppingItem.user_id == user_id, ShoppingItem.list_id == list_id)
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def create_list(
    db: Session, user_id: int, payload: ListCreate, *, commit: bool = True
) -> ShoppingList:
    """新建清单。

    `user_id` / `status` 由服务端决定，不读请求体：一个是安全边界，
    另一个是「新建出来的必然是 active」这条规则。

    commit 之后的 refresh 不能省：created_at 是数据库默认值，
    不 refresh 拿回来就是 None，响应里会缺字段。
    """
    shopping_list = ShoppingList(
        user_id=user_id,
        name=payload.name,
        note=payload.note,
        status="active",
    )
    db.add(shopping_list)
    if not commit:
        db.flush()  # 只为拿到 id；没提交，外面回滚它就不存在
        return shopping_list
    db.commit()
    db.refresh(shopping_list)
    return shopping_list


def update_list(
    db: Session, shopping_list: ShoppingList, payload: ListUpdate, *, commit: bool = True
) -> ShoppingList:
    """局部更新一张清单。

    核心是 `exclude_unset=True`：只吐出「请求体里真的出现过」的字段。
      {"name": "x"}    → {"name": "x"}    只改名字
      {"note": null}   → {"note": None}   清空备注
      {}               → {}               一个都不改（schema 已拦）

    不能直接用 model_dump()：那会把没传的字段也当成 None 写回去，
    一次改名字就把备注清空了。
    """
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shopping_list, key, value)

    if commit:
        db.commit()
        db.refresh(shopping_list)
    else:
        db.flush()
    return shopping_list


def delete_list(db: Session, shopping_list: ShoppingList, *, commit: bool = True) -> int:
    """硬删一张清单，**连同它的全部明细**，返回删掉的明细条数。

    两步必须在同一个事务里：先删明细，再删清单。
    反过来或者分两次提交，中途崩掉就会留下一堆明细 —— 这些行
    在页面上永远看不到（清单没了就没入口），却还在按 user_id
    占用统计。这种「孤儿数据」最难查。

    不依赖数据库外键级联：这两张表本来就没建外键
    （与项目其它表一致），级联行为不该由一个不存在的约束来保证。

    明细按 `list_id` 删，**不额外加 user_id 过滤**：
    list_id 来自已经校验过归属的那张清单，它是唯一且不可伪造的；
    再加上 user_id 反而会掩盖「明细的 user_id 和清单不一致」这种脏数据
    （真出现了也不该让它们漏网留下）。
    """
    removed = db.execute(
        delete(ShoppingItem).where(ShoppingItem.list_id == shopping_list.id)
    ).rowcount or 0
    db.delete(shopping_list)
    if commit:
        db.commit()
    else:
        db.flush()
    return int(removed)


# ---------------------------------------------------------------- 明细


def _item_base(user_id: int) -> Select:
    """明细查询的公共起点：限本人。"""
    return select(ShoppingItem).where(ShoppingItem.user_id == user_id)


def list_items(
    db: Session,
    user_id: int,
    list_id: int,
    *,
    purchased: bool | None = None,
    limit: int | None = None,
) -> list[ShoppingItem]:
    """一张清单里的明细。

    `purchased=None` 表示都返回（详情页的默认行为）；
    `True` / `False` 只返回已购 / 未购的 —— AI 查「我还要买什么」时用。
    `limit=None` 表示不截断。
    """
    stmt = _item_base(user_id).where(ShoppingItem.list_id == list_id)
    if purchased is not None:
        stmt = stmt.where(ShoppingItem.is_purchased == (1 if purchased else 0))
    stmt = stmt.order_by(*_ITEM_ORDER)
    if limit is not None:
        stmt = stmt.limit(max(1, int(limit)))
    return list(db.scalars(stmt).all())


def get_item_for_user(db: Session, user_id: int, item_id: int) -> ShoppingItem | None:
    """按 ID 取一条明细。不属于该用户时返回 None（对外 404）。"""
    return db.scalar(_item_base(user_id).where(ShoppingItem.id == item_id))


def _next_sort_order(db: Session, user_id: int, list_id: int) -> int:
    """新物品排在最后：当前最大值 + 1，空清单从 0 开始。

    不加这个的话新物品全是 0，排序就退化成「按 id」——
    现在看是对的，等以后加了拖动排序，用户手工调过的顺序会被覆盖。
    """
    current = db.scalar(
        select(func.max(ShoppingItem.sort_order)).where(
            ShoppingItem.user_id == user_id, ShoppingItem.list_id == list_id
        )
    )
    return 0 if current is None else int(current) + 1


def create_item(
    db: Session,
    user_id: int,
    list_id: int,
    payload: ItemCreate,
    *,
    source: str = "manual",
    commit: bool = True,
) -> ShoppingItem:
    """往清单里加一条。

    `user_id` 同时写进 list_id 那一行和明细本身：明细表的 user_id 是冗余列，
    它的意义就是「不用 join 清单表也能按用户过滤」，两张表必须一致。

    `source` 默认 manual（页面加）；AI 确认后写入时传 "ai"。
    由调用方传而不是从请求体读 —— 否则客户端一句话就能冒充机器人。
    """
    item = ShoppingItem(
        list_id=list_id,
        user_id=user_id,
        name=payload.name,
        quantity=payload.quantity,
        unit=payload.unit,
        category_id=payload.category_id,
        is_purchased=0,
        note=payload.note,
        sort_order=_next_sort_order(db, user_id, list_id),
        source=source,
    )
    db.add(item)
    if not commit:
        db.flush()
        return item
    db.commit()
    db.refresh(item)
    return item


def update_item(
    db: Session, item: ShoppingItem, payload: ItemUpdate, *, commit: bool = True
) -> ShoppingItem:
    """局部更新一条明细（不含勾选 —— 那是 change_purchased 的事）。"""
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)

    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.flush()
    return item


def change_purchased(
    db: Session, item: ShoppingItem, is_purchased: bool, *, commit: bool = True
) -> ShoppingItem:
    """勾选 / 取消勾选，并维护 purchased_at —— 两条规则缺一不可。

    规则 1（幂等）：已经买过再勾一次，purchased_at 不许刷新。
        否则「购买时间」会变成「最后一次点它的时间」，
        统计「上周买了什么」会漂。
    规则 2（清空）：取消勾选时 purchased_at 一律置 None。
        否则会出现「没买却有购买时间」的自相矛盾数据。

    注意判断顺序：先读旧的 item.is_purchased 再赋值，
    写成 item.is_purchased = ... 之后再去判断，规则 1 就失效了
    （和 tasks/crud.py 的 change_status 是同一个坑）。
    """
    if is_purchased:
        if not item.is_purchased or item.purchased_at is None:
            item.purchased_at = now_utc()
    else:
        item.purchased_at = None

    item.is_purchased = 1 if is_purchased else 0
    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.flush()
    return item


def delete_item(db: Session, item: ShoppingItem, *, commit: bool = True) -> None:
    """硬删除一条明细。接口回 204，没有响应体。"""
    db.delete(item)
    if commit:
        db.commit()
    else:
        db.flush()


# ---------------------------------------------------------------- 给 AI 工具用


def search_items(
    db: Session,
    user_id: int,
    *,
    keyword: str | None = None,
    list_id: int | None = None,
    purchased: bool | None = None,
    limit: int = 20,
) -> list[tuple[ShoppingItem, str]]:
    """跨清单查购物明细，返回 [(明细, 所属清单名)]。

    给 V1.4 第 3 步的 `query_shopping_items` 之类的工具用（现在还没有
    这个工具，这个函数是提前备好的 —— 写它是因为加物品之后必然要能查，
    而且 Join 清单表拿名字这件事放 crud 里比放工具里干净）。

    排序：没买的在前，最近加的在后。
    """
    stmt = (
        select(ShoppingItem, ShoppingList.name)
        .join(ShoppingList, ShoppingList.id == ShoppingItem.list_id)
        .where(ShoppingItem.user_id == user_id, ShoppingList.user_id == user_id)
    )

    if list_id is not None:
        stmt = stmt.where(ShoppingItem.list_id == list_id)

    if purchased is not None:
        stmt = stmt.where(ShoppingItem.is_purchased == (1 if purchased else 0))

    if keyword and keyword.strip():
        like = _like(keyword)
        stmt = stmt.where(
            or_(
                ShoppingItem.name.like(like, escape="\\"),
                ShoppingItem.note.like(like, escape="\\"),
            )
        )

    stmt = stmt.order_by(
        ShoppingItem.is_purchased.asc(),
        ShoppingItem.id.desc(),
    ).limit(min(max(1, int(limit)), 100))

    return [(row[0], row[1]) for row in db.execute(stmt).all()]


def _like(keyword: str) -> str:
    """把用户输入包成 LIKE 模式。

    用户输入永远不能直接拼进 LIKE：% 和 _ 是通配符，一个「100%」
    能把整张表捞出来。顺序不能反 —— 必须先把 \\ 换成 \\\\，
    否则后面加进去的转义符会被再转义一次（和 tasks/crud.py 同一份逻辑）。
    """
    raw = (
        keyword.strip()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{raw}%"


def find_or_create_default_list(
    db: Session, user_id: int, name: str = "默认清单", *, commit: bool = True
) -> ShoppingList:
    """按名字找清单，没有就建一个 —— 给 AI 的 `add_shopping_item` 用。

    为什么要「懒创建」而不是建用户时就塞一张：绝大多数用户第一次用的
    是 AI（页面上他会自己建清单），先建一张空清单只会在左栏留个空壳。

    匹配规则是「本人、这个名字、active」：归档的同名清单不算，
    否则「默认清单」归档之后 AI 会一直往一张看不见的清单里写。

    并发说明：两个请求同时找不到会各建一张同名清单。
    这是可接受的降级（多了张空清单，数据没错），
    加唯一索引能根治，但那份迁移还没做 —— 记在这里，别当成已解决。
    """
    existing = db.scalar(
        select(ShoppingList).where(
            ShoppingList.user_id == user_id,
            ShoppingList.name == name,
            ShoppingList.status == "active",
        )
    )
    if existing is not None:
        return existing

    shopping_list = ShoppingList(user_id=user_id, name=name, status="active")
    db.add(shopping_list)
    if not commit:
        db.flush()
        return shopping_list
    db.commit()
    db.refresh(shopping_list)
    return shopping_list