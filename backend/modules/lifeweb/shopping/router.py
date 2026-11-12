"""购物接口 —— 只做三件事，不含业务规则。

  1. 绑定并校验 HTTP 参数（Query / Body / Path）
  2. 注入 user_id（由 JWT 解析，见 core/deps.py）
  3. 调 crud，把 ORM 对象转成响应 Schema

十一个接口，两条前缀：

    /shopping-lists                      清单
    /shopping-lists/{list_id}/items      清单里的明细
    /shopping-items/{item_id}            跨清单的明细操作（不重复写 list_id）
    /shopping-items/{item_id}/purchased  勾选

路由顺序提醒（和 expenses 一样的老坑）：
固定路径必须写在带参数的路径**之前**。本文件里没有裸 `/{id}` 吞掉子路径的
风险（`/{list_id}` 和 `/{list_id}/items` 段数不同），但以后加
`/shopping-lists/stats` 这类接口时记得往前放。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.deps import get_current_user_id
from core.errors import not_found, validation_error
from modules.lifeweb.shopping import crud
from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList
from modules.lifeweb.shopping.schema import (
    ItemCreate,
    ItemOut,
    ItemPurchased,
    ItemUpdate,
    ListCreate,
    ListDetailOut,
    ListOut,
    ListStatus,
    ListUpdate,
)
from modules.shared.categories import crud as categories_crud
from modules.shared.pagination import Page

router = APIRouter(prefix="/shopping-lists", tags=["购物"])

# 明细单独一个前缀，因为它的 id 是全局唯一的，不需要回指清单
item_router = APIRouter(prefix="/shopping-items", tags=["购物"])


# ---------------------------------------------------------------- 公用


def _get_list_or_404(db: Session, user_id: int, list_id: int) -> ShoppingList:
    """取一张清单，取不到就 404。

    详情 / 改名 / 删除 / 加物品 / 列明细 五个接口共用这一个入口。
    别人家的清单也是 404，不是 403 —— 403 等于承认「这张确实存在」。
    """
    shopping_list = crud.get_list_for_user(db, user_id, list_id)
    if shopping_list is None:
        raise not_found("清单不存在")
    return shopping_list


def _get_item_or_404(db: Session, user_id: int, item_id: int) -> ShoppingItem:
    """取一条明细，取不到就 404。"""
    item = crud.get_item_for_user(db, user_id, item_id)
    if item is None:
        raise not_found("物品不存在")
    return item


def _names(db: Session) -> dict[int, str]:
    """整页共用一张 {分类 id: 名字}，避免每条记录再查一次库。"""
    return categories_crud.name_map(db)


def _list_out(db: Session, user_id: int, shopping_list: ShoppingList) -> ListOut:
    """清单 → 响应体，顺手把两个计数查出来。

    单条操作（新建、改名）也要返回计数：前端拿响应直接替换那一行，
    不用为了一个角标再发一次列表请求。空清单这次查询是廉价的
    （`list_counts` 走 idx_shopping_items_list，且结果为空）。
    """
    item_count, unpurchased = crud.list_counts(db, user_id, shopping_list.id)
    return ListOut.from_model(
        shopping_list, item_count=item_count, unpurchased_count=unpurchased
    )


# ---------------------------------------------------------------- 清单


@router.get("", response_model=Page[ListOut], summary="购物清单列表")
def list_lists(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    status: ListStatus | None = Query(
        default="active",
        description="active / archived；不传默认只看在用的",
    ),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Page[ListOut]:
    """清单列表，每条带 `item_count` 和 `unpurchased_count`。

    默认 `status=active`：归档的清单不该占着左栏位置。
    想翻归档的，显式传 `?status=archived`。

    计数由 crud 用一条 `LEFT JOIN + GROUP BY` 算完（不是逐条查），
    所以 20 张清单也只有一次额外查询。
    """
    rows, total = crud.list_lists(db, user_id, page=page, size=size, status=status)
    return Page.create(
        [
            ListOut.from_model(
                shopping_list, item_count=item_count, unpurchased_count=unpurchased
            )
            for shopping_list, item_count, unpurchased in rows
        ],
        total,
        page,
        size,
    )


@router.post("", response_model=ListOut, status_code=201, summary="新建清单")
def create_list(
    payload: ListCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ListOut:
    """新建清单。

    能走到这里说明请求体已过校验（空名字 422）。
    新建出来一律 `status=active`，且不带明细 —— 所以两个计数都是 0。
    """
    shopping_list = crud.create_list(db, user_id, payload)
    return _list_out(db, user_id, shopping_list)


@router.get("/{list_id}", response_model=ListDetailOut, summary="清单详情（含明细）")
def get_list(
    list_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ListDetailOut:
    """清单本身 + 全部明细，一次拿全。

    行数上限：单张清单的明细一般在几十条以内，这里不做分页 ——
    加物品时立刻能看到，比翻页更重要。真要遇到上千条的清单，
    说明这个用户该把清单拆开了，不是接口该改。
    """
    shopping_list = _get_list_or_404(db, user_id, list_id)
    items = crud.list_items(db, user_id, list_id)
    names = _names(db)
    return ListDetailOut(
        list=_list_out(db, user_id, shopping_list),
        items=[ItemOut.from_model(item, names) for item in items],
    )


@router.patch("/{list_id}", response_model=ListOut, summary="修改清单")
def update_list(
    list_id: int,
    payload: ListUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ListOut:
    """改名 / 改备注 / 归档。

    归档就是 `{"status": "archived"}`：数据都还在，只是列表默认不显示，
    随时可以用 `?status=archived` 翻回来，或者再 PATCH 回 active。

    三种在进 crud 之前就被拦下的请求体（都是 422）：
      空对象 {}        没有需要修改的字段
      name 传 null     数据库 NOT NULL，而且没名字的清单没法显示
      status 瞎写      只认 active / archived
    """
    shopping_list = _get_list_or_404(db, user_id, list_id)
    shopping_list = crud.update_list(db, shopping_list, payload)
    return _list_out(db, user_id, shopping_list)


@router.delete("/{list_id}", status_code=204, summary="删除清单")
def delete_list(
    list_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> None:
    """删除清单，**连同它的全部明细**（硬删，没有回收站）。

    删一张清单等于删掉它底下所有东西，前端必须做二次确认，
    并且确认文案里要写清楚「N 条物品会一起消失」—— 这是这个页面里
    唯一一个「点一下损失最大」的按钮。

    返回 204 无响应体。删完再查详情是 404，连删两次第二次也是 404
    （第二次根本查不到这张清单）。
    """
    crud.delete_list(db, _get_list_or_404(db, user_id, list_id))


# ---------------------------------------------------------------- 明细（挂在清单下）


@router.get("/{list_id}/items", response_model=list[ItemOut], summary="清单里的物品")
def list_items(
    list_id: int,
    purchased: bool | None = Query(
        default=None, description="true 只看买过的 / false 只看没买的 / 不传看全部"
    ),
    limit: int | None = Query(
        default=None, ge=1, le=200, description="最多返回几条；不传不截断"
    ),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> list[ItemOut]:
    """清单里的物品。

    为什么这里返回**裸列表**而不是分页对象：指定了清单就等于划定了范围，
    前端要的是一份完整列表（详情页本来也是整份返回的）。
    真需要截断用 `limit` —— 但那是给「首页显示前三条」这类场景，
    不是给分页翻页用的。

    先校验清单归属再查明细：否则知道一个 list_id 就能看到别人清单的内容
    （即使明细表里也有 user_id，两道过滤都做才算稳）。
    """
    _get_list_or_404(db, user_id, list_id)
    items = crud.list_items(db, user_id, list_id, purchased=purchased, limit=limit)
    names = _names(db)
    return [ItemOut.from_model(item, names) for item in items]


@router.post("/{list_id}/items", response_model=ItemOut, status_code=201, summary="添加物品")
def create_item(
    list_id: int,
    payload: ItemCreate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ItemOut:
    """往清单里加一条。

    分类校验和待办/消费一致：`category_id=0` 表示未分类直接放行；
    非 0 必须存在、`scope='shopping'`、属于本人或系统，否则 422
    （是请求体里某个字段不合法，不是「资源不存在」）。

    `source` 固定写 `manual`（页面加的）。AI 加的那条走 ai/execute.py，
    那是另一条路径 —— 客户端不能通过请求体冒充机器人。
    """
    _get_list_or_404(db, user_id, list_id)
    if payload.category_id:
        if not categories_crud.exists_visible(db, user_id, payload.category_id, scope="shopping"):
            raise validation_error("分类不存在")
    item = crud.create_item(db, user_id, list_id, payload, source="manual")
    return ItemOut.from_model(item, _names(db))


# ---------------------------------------------------------------- 明细（独立前缀）


@item_router.get("/{item_id}", response_model=ItemOut, summary="物品详情")
def get_item(
    item_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ItemOut:
    """取一条物品。单条接口为前端「编辑弹窗」准备，避免从列表里翻。"""
    item = _get_item_or_404(db, user_id, item_id)
    return ItemOut.from_model(item, _names(db))


@item_router.patch("/{item_id}", response_model=ItemOut, summary="修改物品")
def update_item(
    item_id: int,
    payload: ItemUpdate,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ItemOut:
    """改名字 / 数量 / 单位 / 分类 / 备注。

    **不能改勾选状态** —— 那是 PATCH /{item_id}/purchased 的事，
    因为它要联动 purchased_at（见 schema.ItemUpdate 的 docstring）。
    ItemUpdate 没声明 is_purchased，所以传了也不会报错、更不会生效；
    前端别传，传了是白传。
    """
    item = _get_item_or_404(db, user_id, item_id)
    if payload.category_id:
        if not categories_crud.exists_visible(db, user_id, payload.category_id, scope="shopping"):
            raise validation_error("分类不存在")
    item = crud.update_item(db, item, payload)
    return ItemOut.from_model(item, _names(db))


@item_router.patch("/{item_id}/purchased", response_model=ItemOut, summary="勾选 / 取消勾选")
def change_item_purchased(
    item_id: int,
    payload: ItemPurchased,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> ItemOut:
    """标记已购 / 未购。

    为什么单独一个接口而不并进 PATCH /{item_id}：
      1. 前端在这一步只发 `{"is_purchased": true}`，不用把整个对象传来
      2. 它要联动 purchased_at —— 规则比「改个名字」重
      3. 以后要加「买了就记一笔消费」这类副作用，加这里不会影响普通编辑

    purchased_at 的维护在 crud.change_purchased：连勾两次不刷新时间，
    取消勾选清空。
    """
    item = _get_item_or_404(db, user_id, item_id)
    item = crud.change_purchased(db, item, payload.is_purchased)
    return ItemOut.from_model(item, _names(db))


@item_router.delete("/{item_id}", status_code=204, summary="删除物品")
def delete_item(
    item_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> None:
    """删掉清单里的一条（硬删，没有回收站）。

    删完再查 → 404；连删两次第二次也是 404；删别人的还是 404。
    """
    crud.delete_item(db, _get_item_or_404(db, user_id, item_id))


# 供 modules/lifeweb/__init__.py 汇总
routers = [router, item_router]