"""分类字典模块（三端共用）。

表：categories（已由 Alembic 迁移 482d5e0a4da8 建出，并写入 41 条系统预置分类）
归属判断：seed、用户自建二级分类、管理后台维护都会写它，写入方跨三端，所以放 shared。
V1.0 只有只读接口（GET /categories），写接口 V1.4 再开。
"""
