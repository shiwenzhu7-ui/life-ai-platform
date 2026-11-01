"""消费模块（生活管理端 /expenses）。

表：expenses —— 归属判断：只有用户自己写它，所以放 lifeweb 而不是 shared。

四件套：
    model.py   这张表
    schema.py  请求/响应字段
    crud.py    只写 SQL
    router.py  接口
"""

from modules.lifeweb.expenses.model import Expense  # noqa: F401
