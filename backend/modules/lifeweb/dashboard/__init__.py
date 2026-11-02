"""首页仪表盘 —— 只读聚合。

本模块**没有 model.py，也没有自己的表**：它只读 tasks 与 expenses，
所以 modules/registry.py 不需要登记它（那里只登记「有表的模块」）。

常规模块是四件套 model / schema / crud / router，这里是三件套：
    schema.py   对外的形状
    queries.py  取数（跨两张表，所以不叫 crud）
    router.py   接口
"""
