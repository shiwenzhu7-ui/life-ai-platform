import Vue from 'vue'
import VueRouter from 'vue-router'

import { hasValidToken } from '@/utils/auth'
import { safeNextPath } from '@/utils/sso'
import { createAuthGuard } from '@/utils/historyGuard'
import { createNavTree } from '@/utils/navTree'

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
    // Keycloak 登录完跳回来收令牌。必须 public —— 这一刻令牌还在 URL 上、
    // 还没进 localStorage，守卫按 hasValidToken() 判断会把刚登录的人踢回 /login。
    path: '/sso/callback',
    name: 'sso-callback',
    component: () => import('../views/sso/Callback.vue'),
    meta: { title: '正在登录', public: true }
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
    // 已登录还去登录页：把人送回应用，别让人看到登录框。
    //
    // 分层兜底（C 层），两道都要有：
    //   a. 到这里来 = 浏览器 ← 退回了登录页，或手敲了 /login。路由内 replace 会
    //      把这一格换成目标页 —— 但用户再按一次 ← 还会撞上更早的登录页格；
    //   b. 所以这里用**整页**跳转：整页导航会重新加载文档，登录页那几格被跳过的
    //      同时 JS 状态也归零，不会留下半截登录态。
    // 正常登录成功的路径**不该**走到这里 —— 那边已经是整页跳了（见
    // views/login/index.vue 的 leaveTo 和 views/sso/Callback.vue 的 goNext）。
    if (to.path === '/login' && loggedIn) {
      const target = safeNextPath(to.query.redirect)
      window.location.replace(target)
      return
    }
    // 已登录却落在 SSO 中转页（/sso/callback、/sso/... 之类）：
    // 这就是浏览器 ← 退回了登录流程里的一格。手里有能用的令牌 = 人还没退出，
    // 他看到的应该是应用，不是「登录没有完成」。
    //
    // 为什么在守卫里做而不是只在 SsoCallback 里做：守卫是**所有**入口的必经之路，
    // 而 Callback 只是个组件 —— 退到它时组件会被挂载，但地址栏已经改完了，
    // 用户先看到的是那一瞬间的失败页。守卫能在地址被换成中转页的同一时刻
    // 就整页弹回去，闪一下就没了。
    //
    // 用整页 replace 而不是 next(target)：中转页的格子在历史里删不掉（浏览器
    // 不允许），但整页跳至少保证「人不会停在那一页」。
    if (loggedIn && /^\/sso\//.test(to.path)) {
      window.location.replace(safeNextPath(to.query.next))
      return
    }
    // 注意：这里**不能**清「静默探测已试过」标记。
    // 清掉的后果是死循环：/sso/callback 失败 →「回登录页」→ /login 清标记 →
    // LoginView.created 看见标记不在又静默探测一次 → 又回到 /sso/callback。
    // 用户看到的就是「点回登录页没反应」（循环得太快，界面都没变）。
    // 标记该由用户真正动手时清：登录页收到 ?sso_error / 点了「用账号中心登录」，
    // 见 views/login/index.vue 和 onSsoLogin()。
    // 进了登录页就把历史栈锁上（见下面 historyGuard 里的「登录页的锁」）：
    // **没登录**站在登录页时，← 和 → 都不该把人带到别处（那多半是失败的
    // /sso/callback 或账号中心那一跳，回去只会再弹回来）。
    //
    // 只在「真的进登录页」时锁：已经在登录页、又导航到登录页（带了个 query、
    // 被 401 带着 ?redirect= 送回同一页）不重锁 —— 重锁会把上一格重新挪回来。
    // 已经锁着的话 historyGuard 内部自己会幂等返回。
    //
    // 注意：登录成功**不再**走 unpin()。登录一律整页跳走（见 LoginView.leaveTo /
    // Callback.goNext），整页加载后历史栈里根本没有哨兵那几格，锁自然消失。
    if (to.path === '/login' && (!from || from.path !== '/login')) authGuard.pinLoginPage()
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
  // 登录页的锁：离开登录页时按「地址对不对」清一次残留记号。
  // 为什么在 afterEach 而不是 before：beforeEach 跑的时候地址还没换，
  // 而 state 是跟着地址格走的 —— 必须等导航真的完成再看。
  authGuard.reconcile()
  // 面包屑导航树：每次真正落地的跳转记一笔，供「按 ← 回上一级」用。
  navTree.onRouteChanged(to)
})

// 浏览器前进/后退的闸门。
//
// 上面那个 beforeEach 只管「Vue Router 意义上的跳转」。用户点左上角 ← → 时，
// 浏览器可能直接从 bfcache 恢复一整页，beforeEach 一次都不会跑 —— 那就是
// 「退出登录后还能后退回登录中的页面」的成因。historyGuard 用 popstate /
// hashchange / pageshow 把浏览器那一侧也管起来。
//
// 为什么装载要等路由就绪再装：上面 beforeEach 里第一行就调了 authGuard.pinLoginPage()，
// 而这一行在首个导航发生时还没执行到（首屏加载时地址已经是 /login 了）。
// 不在加载那一刻补上，直接打开登录页的那一次就没有锁 —— 而「首页 → 登录页」这种
// 站内跳转又会被后面的进页判断挡回去，结果两处都不锁，等于没锁。
// 所以装好时若当前已经在登录页，必须无条件补一次（见 historyGuard 的 install）。
const authGuard = createAuthGuard({
  router,
  isLoggedIn: hasValidToken,
  loginPath: '/login'
})

// 面包屑导航树（把「访问顺序」的后退改成「层级」的后退，见 utils/navTree.js）。
// 必须在 authGuard 之前装：← 如果先被闸门处理掉，就轮不到这里决定「退到哪一级」。
const navTree = createNavTree({ router })
// 测试与排障用：把实例挂到 window 上，控制台里可以直接 __lifeaiNavTree.trail()
// 看「我现在在哪一级」。只读调试用，不参与任何业务逻辑。
if (typeof window !== 'undefined') window.__lifeaiNavTree = navTree
// vue-router 3 没有 isReady()（那是 v4 的），对应的回调接口是 onReady(cb, errCb)。
router.onReady(
  () => {
    navTree.install()
    authGuard.install()
  },
  () => {
    navTree.install()
    authGuard.install() // 首个导航失败（守卫 reject）时也要装：宁可多一层闸门
  }
)

export default router