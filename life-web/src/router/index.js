import Vue from 'vue'
import VueRouter from 'vue-router'

Vue.use(VueRouter)

// 结构：BasicLayout 是外壳（左侧导航 + 顶部条），三条业务页是它的 children，
// 这样切页时只有 el-main 里的内容重建，导航和顶部条不动。
// 404 放在最外层，不带布局——用户输错地址时不该看到一套空白导航。
const routes = [
  {
    path: '/',
    component: () => import('../layouts/BasicLayout.vue'),
    children: [
      {
        path: '',
        name: 'dashboard',
        component: () => import('../views/dashboard/index.vue'),
        meta: { title: '首页' }
      },
      {
        path: 'tasks',
        name: 'tasks',
        component: () => import('../views/tasks/index.vue'),
        meta: { title: '待办' }
      },
      {
        path: 'expenses',
        name: 'expenses',
        component: () => import('../views/expenses/index.vue'),
        meta: { title: '消费' }
      }
    ]
  },
  {
    path: '*',
    name: 'not-found',
    component: () => import('../views/NotFound.vue'),
    meta: { title: '页面不存在' }
  }
]

const router = new VueRouter({
  mode: 'history',
  base: process.env.BASE_URL,
  routes
})

// 标题栏跟着当前路由变，浏览器历史里也看得清是哪一页
router.afterEach((to) => {
  document.title = to.meta && to.meta.title ? `${to.meta.title} · LifeAI` : 'LifeAI'
})

export default router
