import Vue from 'vue'
import VueRouter from 'vue-router'

import store from '@/store'
import { getLastRole, hasValidToken } from '@/utils/auth'
import { clearSilentTried } from '@/utils/sso'

Vue.use(VueRouter)

// 结构：BasicLayout 是外壳（左侧导航 + 顶部条），业务页是它的 children。
// 登录页、403、404 放最外层，不带布局 —— 它们不该看到一套没有用户的导航。
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/login/index.vue'),
    meta: { title: '登录', public: true }
  },
  {
    path: '/sso/callback',
    name: 'sso-callback',
    component: () => import('../views/sso/Callback.vue'),
    // ⚠️ 必须 public：回调落地的一瞬间令牌还没进 localStorage，
    // 守卫要是拿 hasValidToken() 判断，会把刚登录的人踢回 /login —— 死循环。
    meta: { title: '正在登录', public: true }
  },
  {
    path: '/403',
    name: 'forbidden',
    component: () => import('../views/Forbidden.vue'),
    meta: { title: '无权访问', public: true }
  },
  {
    path: '/',
    component: () => import('../layouts/BasicLayout.vue'),
    // 后台默认落在用户管理，而不是空首页
    redirect: '/users',
    // 所有后台页都要求管理员。判不出来时（Vuex 空、localStorage 也没记）
    // 先放行，布局挂载后 fetchMe 拿到角色再决定要不要踢去 /403。
    meta: { requiresAdmin: true },
    children: [
      {
        path: 'users',
        name: 'users',
        component: () => import('../views/users/index.vue'),
        meta: { title: '用户管理' }
      },
      {
        path: 'usage',
        name: 'usage',
        component: () => import('../views/usage/index.vue'),
        meta: { title: 'AI 调用明细' }
      },
      {
        path: 'roles',
        name: 'roles',
        component: () => import('../views/roles/index.vue'),
        meta: { title: '角色说明' }
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

// 取角色：优先 Vuex（fetchMe 之后才有），退回 localStorage 的上次登录角色。
// 两者都没有就返回 ''（未知），调用方按「放行、稍后再判」处理。
function knownRole() {
  const user = store.state.user && store.state.user.user
  if (user && user.role) return user.role
  return getLastRole()
}

function isAdminRoute(to) {
  return to.matched.some((record) => record.meta && record.meta.requiresAdmin)
}

// 路由守卫：没令牌一律回登录页并带上「本来想去哪」；已知不是管理员的，后台页一律送 /403。
// ⚠️ 这里只是体验上的拦截，**不是安全边界** —— 每个后台接口都在后端验过 role。
router.beforeEach((to, from, next) => {
  const isPublic = Boolean(to.meta && to.meta.public)
  const loggedIn = hasValidToken()

  if (isPublic) {
    // 已登录还能看 /403（要给「换个账号」按钮），只有 /login 才弹走。
    // 非管理员也能重登（换管理员账号或走账号中心），所以不再拦 —— 原先这里
    // 只放 admin 走，二级/普通用户点「重新登录」会被无故弹回后台页。
    if (to.path === '/login' && loggedIn && knownRole() === 'admin') return next('/')
    // 路由没有整页刷新，上一个用户留下的「静默探测已试过」标记会挡住下一个人，
    // 所以在页面之间切换时清掉；整页刷新时这个守卫不会跑，标记仍然有效（防死循环）
    // 从别的页面（比如 /403 点「换个账号」）过来也不能被它挡住
    if (to.path === '/login' && !loggedIn) clearSilentTried()
    return next()
  }

  if (!loggedIn) {
    return next(
      to.fullPath && to.fullPath !== '/'
        ? { path: '/login', query: { redirect: to.fullPath } }
        : { path: '/login' }
    )
  }

  const role = knownRole()
  if (role && role !== 'admin' && isAdminRoute(to)) {
    return next({ path: '/403', replace: true })
  }

  next()
})

// 登录成功那一刻守卫已经放过了，页面的 created 也已经跑完；这里补一刀：
// 拿到 /auth/me 发现不是管理员，就换到 /403。没有这一刀，普通用户会先进
// /users 看一眼 403 红条才被送走。
function checkAdminAfterMe() {
  const current = router.currentRoute
  if (!current || !isAdminRoute(current)) return
  const user = store.state.user && store.state.user.user
  if (user && user.role && user.role !== 'admin') {
    router.replace({ path: '/403' })
  }
}

store.watch(
  (state) => state.user.user && state.user.user.role,
  () => checkAdminAfterMe()
)

router.afterEach((to) => {
  document.title =
    to.meta && to.meta.title ? `${to.meta.title} · LifeAI 管理后台` : 'LifeAI 管理后台'
})

export default router