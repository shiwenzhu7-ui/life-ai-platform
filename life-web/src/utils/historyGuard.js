// 浏览器前进/后退的「登录态闸门」。
//
// 为什么需要这个文件：
//   点浏览器左上角的 ← → 时，浏览器操作的是**它自己的历史栈**，不是 Vue Router。
//   在单页应用里这两件事会分叉：
//     1. 同一份文档内的 hash/history 跳转 —— 走 router.beforeEach，能被拦住；
//     2. 离开本页后按「后退」回来 —— 浏览器可能直接从 **bfcache**（往返缓存）
//        把整个页面连同 JS 运行时一起恢复，**beforeEach 一次都不会跑**。
//   第 2 种就是「退出登录后还能退回到登录中的页面」的成因：令牌早就清了，
//   但画面是退出前的快照，连数据都还是旧的。
//
// 所以闸门必须有三层，缺一层就有缝：
//   层 1  router.beforeEach        —— 管正常的路由跳转（由 router/index.js 负责）
//   层 2  popstate / hashchange    —— 管浏览器前进后退造成的地址变化
//   层 3  pageshow + persisted     —— 管 bfcache 恢复，这是唯一能救回来的时机
//
// 设计原则：**闸门只搬人，不改数据**。
//   真正的鉴权永远在后端（每个接口都验签）。这里做的只是「别让人看见不属于
//   当前登录态的画面」，把用户体验拉回正轨。

// 一个可重复注册、可注销的闸门。两个前端用法一致，只有 isLoggedIn 和
// 登录页路径可能不同，所以做成工厂函数。
export function createAuthGuard({
  router,
  isLoggedIn,
  loginPath = '/login',
  // 本应用里「不需要登录就能看」的路由判定；默认读 meta.public
  isPublicRoute = (route) => Boolean(route && route.meta && route.meta.public)
} = {}) {
  if (!router) throw new Error('createAuthGuard 需要 router')
  if (typeof isLoggedIn !== 'function') throw new Error('createAuthGuard 需要 isLoggedIn()')

  let installed = false

  // 把浏览器送到登录页，并把「本来在哪」带上（方便登录后回来）。
  // replace 而不是 push：不能让登录页成为历史里新的一格，否则用户再按后退
  // 又退回刚才那页，来回拉扯。
  function toLogin(from) {
    const current = router.currentRoute
    if (current && current.path === loginPath) return
    const fullPath = from && from !== loginPath ? from : ''
    const target =
      fullPath && fullPath !== '/'
        ? { path: loginPath, query: { redirect: fullPath }, replace: true }
        : { path: loginPath, replace: true }
    router.replace(target).catch(() => {
      // 导航被取消（比如用户紧接着又点了一次）时 Vue Router 会 reject，
      // 这里吞掉：闸门不该因为一次重复导航就抛异常
    })
  }

  // 拿当前页面判断一次：该不该在。返回 true 表示「放行」，false 表示「已被送走」。
  function check() {
    const route = router.currentRoute
    if (!route) return true
    if (isPublicRoute(route)) {
      // 已登录还停在登录页：回家。这一条和 router 里的守卫互补 ——
      // 从 bfcache 恢复时只有这个函数会跑。
      if (route.path === loginPath && isLoggedIn()) {
        router.replace('/').catch(() => {})
        return false
      }
      return true
    }
    if (!isLoggedIn()) {
      toLogin(route.fullPath)
      return false
    }
    return true
  }

  // ---------- 登录页的「锁」----------
  //
  // 要的效果：人站在登录页时，浏览器的 ← 和 → 都不该把人带到任何别的地方。
  //   往前一页 —— 登录页不是从哪个页面「进」来的（首次打开 / 整页跳回来），
  //                历史里往前要么是新标签页，要么就是刚才那个失败的 /sso/callback
  //                —— 两者都只会把人甩出登录页。
  //   往后一页 —— 同理，多半是账号中心那一跳或上一轮的死循环，回去了也只会再弹回来。
  //   登录成功离开登录页之后，存档栈**照常可用**（那是用户真正的浏览历史）。
  //
  // 做法：站在登录页时往历史栈里压一个**同地址哨兵**（pushState，不触发路由），
  // 于是 ⌊后⌋ 从哨兵退回登录页、⌊前⌋ 从登录页进到哨兵 —— 两个方向都落在登录页自己身上，
  // 看起来就是「按了没反应」。等登录成功后离开登录页，哨兵在 popstate 里被清掉。
  //
  // 为什么不是「用 location.replace 把历史格冲掉」：
  //   history.length 减不掉、replaceState 也改不了别的格，这些 API 都只能动当前格。
  //   给当前格加一个哨兵是唯一一种**只靠当前页就能做到、且不需要 popstate 反复搬人**的办法。
  //   popstate 里再搬人有它的代价：每一步都会闪一下「账号中心」的界面。
  //
  // 为什么不 popstate/preventDefault：
  //   浏览器的前进后退**不可取消**，没有 API 能拦住它。能做到「按了像没按」只有这一种。
  //
  // 边界（必须说清楚）：
  //   1. 这一格是从「别的网页」过来的（比如从别的站点跳过来），「后退」离开本站
  //      是浏览器层面的行为，网页拦不住；这里管住的是**站内**的前进后退。
  //   2. 浏览器自带的「长按后退键选历史」面板绕不开；手机上也没有 ← 可「禁用」。
  //   3. 地址栏直接改地址、刷新，本来就不受历史栈约束。
  // 所以这层是**体验**，不是安全边界 —— 没令牌的人照样进不去后台（守卫 + 后端 401）。

  // 哨兵格的标记。state 里的键叫 __pin / __leaving，同源下任何页面都读得到，
  // 所以用本应用自己的名字当标记，别和别的页面打架。

  // 哨兵格的状态里记两个字段（哨兵不带 URL，地址栏不动）：
  //   __pin     —— 本应用压的哨兵，popstate 里据此认出「这一格该被丢掉」
  //   __leaving —— 哨兵被用户「前进」到了，说明人正在往外走，下一个 popstate 放行
  // 放在 state 里而不是模块变量里：bfcache 恢复时 JS 内会重置，state 不会。
  const PIN_STATE = { __pin: true }
  const LEAVE_STATE = { __pin: true, __leaving: true }

  let pinned = false

  function currentState() {
    return (window.history && window.history.state) || null
  }

  // 站在登录页时调用：历史栈顶端没有哨兵就压一个。
  // 重复调用要幂等 —— 每次进登录页的 created/guard 都会来一次，重复压会让
  // 「后退」多按几次才有效，反而显得更怪。
  //
  // 两个条件都满足才压，缺一个都会出事：
  //   1. 地址真的在登录页 —— 只看 state 不行：哨兵格被 router.replace 换到别的
  //      地址上时（/login → /sso/callback），state 会跟着格一起过去，那时若只看
  //      state 就会在别的页面上再压一个哨兵，把顺序全打乱。
  //   2. 当前格位置上没有哨兵 —— 已经在登录页原格（state 为空）时才压。
  function pinLoginPage() {
    // 只有当前路由真的是登录页才锁，其他页面调进来直接忽略
    const route = router.currentRoute
    if (!route || route.path !== loginPath) return
    const state = currentState()
    if (pinned || isPinnedState(state)) {
      pinned = true
      return
    }
    try {
      window.history.pushState(PIN_STATE, '', window.location.href)
      pinned = true
    } catch (e) {
      // 极端情况（history 被禁、配额）下就不锁了：功能不受影响，只是少一层体验
    }
  }

  // 成功离开登录页**不需要**再调这个：登录一律整页跳（LoginView.leaveTo /
  // Callback.goNext），新文档里根本没有哨兵格，锁自然消失。
  // 保留它是给「注销 / 异常情况下仍走了 SPA 导航」的兜底 —— 宁可多摘一次。
  function unpin() {
    pinned = false
    const state = currentState()
    if (state && state.__pin) {
      try {
        window.history.replaceState(null, '', window.location.href)
      } catch (e) {
        // 同上，纯体验
      }
    }
  }

  // 每次 Vue Router 完成一次导航后都要调一次。
  // 为什么需要：router.replace('/sso/callback') 这种换地址的操作，浏览器会把
  // 上一个格的 state 一起带到新地址上（__pin 跟着跑）。那一格已经不是登录页了，
  // 残留的记号会一路传到后面的页面，并在 popstate 里被误当成哨兵。
  // 所以这里按「地址对不对」做一次校正，与 history 事件无关。
  function reconcile() {
    const pathname = (window.location && window.location.pathname) || ''
    const onLogin = pathname === loginPath || pathname === loginPath + '/'
    if (!onLogin && isPinnedState(currentState())) {
      unpin()
    }
  }

  function isPinnedState(state) {
    return Boolean(state && state.__pin)
  }

  // 层 2：浏览器前进/后退改了地址。popstate 在 history 模式下触发，
  // hashchange 在 hash 模式下触发；两个都挂上，不管用哪种 mode 都覆盖。
  function onPopState() {
    // 层 2a：登录页的哨兵。先处理它，不然 vue-router 会先一步按地址去导航。
    //
    // 判「在不在登录页」用的是 location.pathname，不是 router.currentRoute：
    // popstate 触发时 Vue Router 的异步守卫还没跑完，currentRoute 还停在旧地址，
    // 用它判断就会出现「人已经到 /sso/callback 了，代码还以为在登录页」——
    // 于是错误地再压一个哨兵，把历史栈彻底搅乱。
    const pathname = (window.location && window.location.pathname) || ''
    const onLogin = pathname === loginPath || pathname === loginPath + '/'
    const state = currentState()

    if (onLogin) {
      if (state && state.__leaving) {
        // 用户从登录页「前进」走了：这是他自己要往外走，放行，并且把哨兵撤掉，
        // 免得他回头时又被锁在登录页、出不去。
        unpin()
      } else if (pinned || isPinnedState(state)) {
        // 用户在登录页上按了 ← 或 →，被哨兵接住（地址没变）。
        // 把哨兵重新摆好，让他再按也一样；Vue Router 那边地址没变，不会导航。
        pinLoginPage()
      }
    } else if (isPinnedState(state)) {
      // 地址已经不是登录页了，history.state 却还带着哨兵：这是跟过来的残留
      // （router.replace 改地址时 state 会一起搬过去）。必须清掉，否则后续
      // 在别的页面上按 ← → 都会撞见这个记号，哨兵逻辑会跟着乱。
      unpin()
    }

    // 地址已经被浏览器改完了，这里只能「纠正」不能「阻止」：
    // 没登录就再 replace 回登录页；登录了就什么都不做（让他正常后退）
    check()
  }

  // 层 3：bfcache。persisted=true 表示这是一次从缓存里的复活，
  // 不是正常加载 —— 此时页面上的 DOM/JS 全是离开前的样子，必须重新判断。
  function onPageShow(event) {
    if (!event || !event.persisted) return
    const route = router.currentRoute
    // 复活后 Vuex 还是旧的，Vue Router 的 currentRoute 也是旧的。
    // 但它和 localStorage 里的令牌一定有一个对不上，check() 就能发现。
    if (!check()) return
    // 放行了也要让各页面重新拉一次数据：bfcache 里的请求结果可能已经过期。
    window.dispatchEvent(new CustomEvent('lifeai:resumed'))
    if (route && route.meta && route.meta.title) {
      document.title = route.meta.title
    }
  }

  // 层 3 的补充：pageshow 之外的另一个入口是 visibilitychange。
  // 有些浏览器在后台标签页恢复时会刷新数据但不会触发 pageshow，
  // 令牌过期又恰好发生在切换标签页期间，这里补一刀（很轻，只是读 localStorage）。
  function onVisibility() {
    if (document.visibilityState !== 'visible') return
    check()
  }

  function install() {
    if (installed) return
    installed = true
    window.addEventListener('popstate', onPopState)
    window.addEventListener('hashchange', onPopState)
    window.addEventListener('pageshow', onPageShow)
    document.addEventListener('visibilitychange', onVisibility)
    // 首次挂载也跑一次：防止「上次退出后直接刷新了一个后台页」的情况
    check()
    // 装载时人已经在登录页（首次进站 / 整页跳回来 / 刷新）也要上锁。
    // 这里按地址判，不按路由：router 刚就绪，currentRoute 未必已经同步过来，
    // 而 pinLoginPage 内部自己会再校验一遍路由，误判的代价只是不压哨兵。
    const pathname = (window.location && window.location.pathname) || ''
    if (pathname === loginPath || pathname === loginPath + '/') pinLoginPage()
  }

  function uninstall() {
    if (!installed) return
    installed = false
    window.removeEventListener('popstate', onPopState)
    window.removeEventListener('hashchange', onPopState)
    window.removeEventListener('pageshow', onPageShow)
    document.removeEventListener('visibilitychange', onVisibility)
  }

  return { install, uninstall, check, pinLoginPage, unpin, reconcile }
}