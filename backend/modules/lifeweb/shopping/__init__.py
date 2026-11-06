"""购物清单模块（V1.1 先建表，V1.4 才有页面与接口）。

    model.py    shopping_lists + shopping_items 两张表

后续步骤加的东西：
    schema.py   清单/明细的请求与响应（V1.4）
    crud.py     清单与明细的读写（V1.4；本版本只被 AI 确认流调用）
    router.py   /shopping 系列接口（V1.4）
"""

from modules.lifeweb.shopping.model import ShoppingItem, ShoppingList

__all__ = ["ShoppingItem", "ShoppingList"]
