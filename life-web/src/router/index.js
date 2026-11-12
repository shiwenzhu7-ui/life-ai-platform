import Vue from 'vue'
import VueRouter from 'vue-router'

import { hasValidToken } from '@/utils/auth'

Vue.use(VueRouter)

// 结构：BasicLayout 是外壳（左侧导航 + 顶部条），三条业务页是它的 children，
// 这样切页时只有 el-main 里的内容重建，导航和顶部条不动。
// 登录页和 404 放在最外层，不带布局——它们不该看到一套没有用户的导航。
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/login/index.vue'),
    // public 是路由守卫的放行标记。新加「忘了密码」这类页面时也要打上它。
    meta: { title: '登录', public: true }
  },
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
      },
      {
        path: 'shopping',
        name: 'shopping',
        component: () => import('../views/shopping/index.vue'),
        meta: { title: '购物清单' }
      },
      {
        path: 'knowledge',
        name: 'knowledge',
        component: () => import('../views/knowledge/index.vue'),
        meta: { title: '知识库' }
      },
      {
        path: 'ai',
        name: 'ai',
        component: () => import('../views/ai/index.vue'),
        meta: { title: 'AI 助手' }
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

// 路由守卫：没令牌一律回登录页，并把「本来想去哪」带上。
//
// 判断依据是 localStorage 里的令牌（utils/auth.js），不是 Vuex 的 state.token。
// 理由：守卫有可能在 Vue 实例挂载、store 初始化之前就被触发，
// 而 localStorage 是同步可读的、永远是可信来源；Vuex 只是它的内存投影。
//
// 这只是**前端体验**上的拦截，不是安全边界 ——
// 真把令牌藏起来不跳转，后端该 401 还是 401（每个接口都验签）。
router.beforeEach((to, from, next) => {
  const isPublic = Boolean(to.meta && to.meta.public)
  const loggedIn = hasValidToken()

  if (isPublic) {
    // 已登录还去登录页：直接回家，别让人看到登录框
    if (to.path === '/login' && loggedIn) return next('/')
    return next()
  }

  if (!loggedIn) {
    // redirect 只在真的「想去别的页」时带，避免 / → /login?redirect=/ 这种废话
    return next(
      to.fullPath && to.fullPath !== '/'
        ? { path: '/login', query: { redirect: to.fullPath } }
        : { path: '/login' }
    )
  }

  next()
})

// 标题栏跟着当前路由变，浏览器历史里也看得清是哪一页
router.afterEach((to) => {
  document.title = to.meta && to.meta.title ? `${to.meta.title} · LifeAI` : 'LifeAI'
})

export default router