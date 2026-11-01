"""三端共用的领域代码。

放这里的标准：**被两个以上的端用到**。
   users        用户表 + 认证 —— 三端都要（/auth/me、/admin/users）
   categories   分类字典 —— 待办、消费、预算、生活、购物、分析都用
   pagination   分页结构 —— 所有列表接口都用

只被一个端用到的，放到那个端下面（modules/lifeweb/…），不要放这里。
"""
