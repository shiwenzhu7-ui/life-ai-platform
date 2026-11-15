import Vue from 'vue'
import VueRouter from 'vue-router'

import store from '@/store'
import { getLastRole, hasValidToken } from '@/utils/auth'

import { createAuthGuard } from '@/utils/historyGuard'
import { createNavTree } from '@/utils/navTree'

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
    // 管理员已登录还去登录页：送回后台（C 层兜底）。
    // 用**整页**跳转：路由内 replace 只换掉当前格，用户再按一次 ← 还会撞上更早的
    // 登录页格；整页导航重新加载文档，登录页的历史格被跳过，JS 状态也归零。
    // 正常登录成功不走这里 —— 那边已经是整页跳了（LoginView.leaveTo / Callback.goNext）。
    if (to.path === '/login' && loggedIn && knownRole() === 'admin') {
      window.location.replace('/')
      return
    }
    // 已登录却落在 SSO 中转页（/sso/callback 等）：这是浏览器 ← 退回了登录流程里
    // 的一格。手里有能用的令牌 = 人还没退出，该看到的是后台，不是「登录没有完成」。
    // 放在守卫里而不是只在 SsoCallback 里：守卫是所有入口的必经之路。
    if (loggedIn && /^\/sso\//.test(to.path)) {
      window.location.replace('/')
      return
    }
    // 注意：这里**不能**清「静默探测已试过」标记。
    // 清掉的后果是死循环：/sso/callback 失败 →「回登录页」→ /login 清标记 →
    // LoginView.created 看见标记不在又静默探测一次 → 又回到 /sso/callback。
    // 用户看到的就是「点回登录页没反应」（循环太快，界面都没变）。
    // 标记该由用户真正动手时清：登录页收到 ?sso_error / 点了「用账号中心登录」，
    // 见 views/login/index.vue 和 onSsoLogin()。
    // 进了登录页就把历史栈锁上（见下面 historyGuard 里的「登录页的锁」）：
    // **没登录**站在登录页时，← 和 → 都不该把人带到别处。
    //
    // 只在「真的进登录页」时锁：已经在登录页、又导航到登录页（带 query、被 401
    // 带着 ?redirect= 送回来）不重锁。已经锁着的情况 historyGuard 内部幂等。
    //
    // 注意：登录成功**不再**走 unpin() —— 登录一律整页跳，整页加载后
    // 历史栈里没有哨兵那几格，锁自然消失。
    if (to.path === '/login' && (!from || from.path !== '/login')) authGuard.pinLoginPage()
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
  // 登录页的锁：离开登录页时按「地址对不对」清一次残留记号。
  // 为什么在 afterEach：beforeEach 跑的时候地址还没换，而 state 跟着地址格走。
  authGuard.reconcile()
  // 层级导航树：每次真正落地的跳转记一笔，供「按 ← 回上一级」用。
  navTree.onRouteChanged(to)
})

// 浏览器前进/后退的闸门（与 life-web 同一个模块）。
//
// 上面的 beforeEach 只管 Vue Router 意义上的跳转；用户点左上角 ← → 时浏览器
// 可能直接从 bfcache 恢复整页，beforeEach 不会跑 —— 那样退出登录后还能后退
// 回后台页面。historyGuard 用 popstate / hashchange / pageshow 补上这一层。
//
// 装载要等路由就绪：beforeEach 里第一行就用 authGuard.pinLoginPage()，
// 而这一行在首个导航发生时还没执行到。不晚一步装，直接打开登录页那一次就没锁。
const authGuard = createAuthGuard({
  router,
  isLoggedIn: hasValidToken,
  loginPath: '/login'
})

// 层级导航树（与 life-web 同一个模块）：按 ← 回「上一级」而不是「上一个访问过的页」。
//
// rootPath 必须是 '/users' 而不是 '/'：管理后台的 '/' 只是个转发，
// 真正落地的是 /users。拿 '/' 当根的话，每次「回上一级」都会撞一次重定向，
// 树里会冒出 /login、/users 这类假层级（实测：/users 退到底变成 /login→/users）。
const navTree = createNavTree({ router, rootPath: '/users' })
// 测试与排障用：控制台里 __lifeaiNavTree.trail() 看当前在第几级。
if (typeof window !== 'undefined') window.__lifeaiNavTree = navTree

// vue-router 3 没有 isReady()（那是 v4 的），对应的回调接口是 onReady(cb, errCb)。
router.onReady(
  () => {
    navTree.install()
    authGuard.install()
  },
  () => {
    navTree.install()
    authGuard.install() // 首个导航失败时也要装：宁可多一层闸门
  }
)

export default router