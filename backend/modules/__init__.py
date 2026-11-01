"""业务模块区。

结构规矩：
    modules/
    ├── shared/        三端共用（用户、分类字典、分页）—— 只有一份
    ├── lifeweb/       生活管理端 8080
    ├── analyticsweb/  数据分析端 8081
    └── adminweb/      系统管理后台 8082

每个模块一个文件夹，里面四件套：
    model.py    这张表（只读聚合的模块没有这个文件）
    schema.py   请求/响应字段
    crud.py     只写 SQL
    router.py   接口

硬规矩：**一张表只能有一个 model.py**，放在它归属的模块里；
别的模块要用就 import，绝不复制（否则 SQLAlchemy 报 Table already defined）。
"""
