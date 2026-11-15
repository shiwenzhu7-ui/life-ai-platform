// 应用内的「层级」后退：把浏览器历史从「访问顺序」改造成「层级路径」。
//
// ## 要解决的事（用户原话）
//   1 → 2 → 1        在 1 上，没有左箭头了（退到头）
//   1 → 2 → 3 → 2    在 2 上按 ← 回到 1，**不是**回到「刚才从 3 过来的那个 2」
//
// 浏览器自己的历史是**访问顺序**：1,2,3,2 再按 ← 会退到 3。
// 用户要的是**层级顺序**：后退 = 回到上一级，跟「怎么进来的」无关。
//
// ## 怎么做到（核心只有一句：不让浏览器按访问顺序退）
//
//   每条历史格都带一个 state：{ navDepth: N }，记录「这一格在树上第几层」。
//   popstate 触发时（用户按了 ←），比较「落地的深度」和「上一格的深度」：
//
//     落地的深度 = 上一层   → 浏览器退对了，接受它（什么都不做）
//     落地的深度 ≥ 自己这层 → 浏览器退到了访问顺序上的兄弟页（比如 3 → 2 的 2），
//                             立刻 push 父节点，把它盖掉（用户看到的就是「回上一级」）
//
//   这样树只要记「每页是第几层 + 每个路径的父是谁」，不用维护整棵访问图。
//
// ## 为什么不直接改历史栈
//   做不到。浏览器不暴露别的格子的内容，history.go 只能移动不能删改；
//   跨文档的格被 go() 到时还会整页加载（实测：popstate 来不及跑）。
//
// ## 边界
//   1. 只用在登录后的应用内页面。登录页/SSO 页有另外的闸门（historyGuard）。
//   2. 根是 '/'。到根上再按 ← 就交给浏览器 —— 那是用户真要离开应用。
//   3. 树只活在内存里：刷新后从当前页重新开始长。

const ROOT = '/'

// state 里存深度的键。带 __ 前缀，避免和 Vue Router 自己的 state 冲突
// （vue-router 3 在 history 模式下的 state 是 { key: '1699...' }，两者共存）。
const DEPTH_KEY = '__navDepth'

// 路径比较时忽略 query/hash：/tasks?task=3 和 /tasks 是同一页的两种态。
function normalize(path) {
  if (!path) return ROOT
  const cut = path.split('?')[0].split('#')[0]
  return cut || ROOT
}

function readDepth() {
  const s = window.history.state
  const d = s && typeof s[DEPTH_KEY] === 'number' ? s[DEPTH_KEY] : null
  return d
}

// 给当前历史格打上深度标记。
// 用 replaceState 而不是 pushState：只改当前这一格的附加数据，不新增历史格。
function markDepth(depth) {
  const s = window.history.state || {}
  if (s[DEPTH_KEY] === depth) return
  const next = Object.assign({}, s)
  next[DEPTH_KEY] = depth
  window.history.replaceState(next, '', window.location.href)
}

export function createNavTree({ router, rootPath = ROOT } = {}) {
  if (!router) throw new Error('createNavTree 需要 router')
  // 应用自身的「首页」。默认 '/'，但管理后台的 '/' 会重定向到 /users ——
  // 那种情况下必须把根设成真正落地的页（/users），否则「回上一级」会撞上
  // 一次重定向，树里会多出 /login、/users 之类的假层级。
  const BASE_ROOT = normalize(rootPath)

  // 路径 → 父路径。只记「谁是谁的上一级」，不记顺序 —— 顺序无关才是重点。
  const parentOf = new Map([[BASE_ROOT, null]])
  // 当前站的位置（路径）
  let current = BASE_ROOT
  // 当前深度：根是 0，/tasks 是 1，更深依次加一
  let depth = 0
  let installed = false
  // 自己发起的 push（goTo 里用），处理期间忽略 afterEach 的记录
  let selfPush = false
  // 排障用的事件流水
  const log = []

  function parentPath(path) {
    return parentOf.has(path) ? parentOf.get(path) : BASE_ROOT
  }

  function depthOf(path) {
    let d = 0
    let p = normalize(path)
    const seen = new Set()
    while (p && p !== BASE_ROOT && !seen.has(p)) {
      seen.add(p)
      p = parentPath(p)
      d++
    }
    return d
  }

  // 记录一次真实的跳转：目标页的上一级 = 当前页。
  //
  // 唯一的判断：目标如果**已经是当前页的某一级祖先**，说明这是「回到走过的上级」，
  // 不再改写它的父（否则 1→2→3→2 里那个 2 会被挂到 3 下面，按 ← 又回 3）。
  function record(toPath) {
    const path = normalize(toPath)
    if (path === current) return
    // 已经是祖先（或就在树上更深的位置）：保留原有父子关系
    if (parentOf.has(path) && depthOf(path) <= depth) {
      current = path
      depth = depthOf(path)
      return
    }
    // 新页面：它的上一级就是现在的当前位置
    parentOf.set(path, current)
    depth = depth + 1
    current = path
  }

  // 跳到「上一级」，并新建一格历史（push 语义）。
  // 新建而不是 go(-1)：go(-1) 会落回访问顺序里的兄弟页，正是我们要消除的行为。
  function goUp() {
    if (!current || current === BASE_ROOT) return false
    const target = parentPath(current)
    // 排障用：记录每次「纠正后退」的目标，控制台里 __lifeaiNavTree.log 能看
    log.push({ type: 'goUp', from: current, depth, to: target, at: Date.now() })
    selfPush = true
    router
      .push(target)
      .catch(() => {})
      .then(() => {
        selfPush = false
        current = normalize(target)
        depth = depthOf(current)
        markDepth(depth)
      })
    return true
  }

  function onRouteChanged(route) {
    const path = normalize(route && route.path)
    // goUp 自己的 push：位置由 goUp 负责落地，这里不能重复记账
    if (selfPush) return

    // ⚠️ 这一次 afterEach 可能是**浏览器后退**引起的（Vue Router 自己的 popstate
    //    监听会 transitionTo）。判断依据：目标页已经在树上（说明来过），
    //    而且它的深度**比当前浅或相等** —— 那是「往上退」或「退到兄弟页」，
    //    不是往下走。这时不能 record：否则会把后台退到的兄弟页挂成当前页的
    //    子节点；也不能把 current 挪过去，挪了之后 popstate 的判据就失效了
    //    （实测：/expenses 按 ← 会被纠正成 / 而不是 /tasks）。
    //
    //    （这里曾经写成 `depthOf(path) > depth`，方向刚好写反 —— 后退的目标
    //     深度一定**更小**，写反等于永远不拦。）
    if (parentOf.has(path) && path !== current && depthOf(path) <= depth) {
      log.push({ type: 'route-ignore-backward', from: current, depth, to: path, at: Date.now() })
      return
    }

    if (path === BASE_ROOT) {
      current = BASE_ROOT
      depth = 0
      markDepth(0)
      return
    }
    record(path)
    markDepth(depth)
  }

  // 用户按了 ← / →。浏览器已经把地址换好了，这里只决定「接受还是纠正」。
  //
  // ## 为什么要在**捕获阶段**监听并 stopImmediatePropagation
  //   Vue Router 3 的 HTML5History 自己也在 window 上注册了 popstate
  //   （vue-router.esm.js:2591），它会 transitionTo(当前地址) —— 也就是
  //   **按浏览器访问顺序**切页。而这里要的恰恰是「不按访问顺序」。
  //
  //   如果两个监听都在冒泡阶段跑，执行顺序就取决于谁先 addEventListener：
  //   实测 Vue Router 先跑（它在 install 之前就注册好了），于是路由先被切到
  //   访问顺序的上一个兄弟页（/expenses → / 而不是 /tasks），我们再纠正时
  //   已经把那一页渲染出来了，用户会看到闪一下。
  //
  //   放在捕获阶段 + stopImmediatePropagation：我们先拿到事件，
  //   需要纠正的情况下直接**掐掉**这个 popstate，Vue Router 不会收到，
  //   也就不会 transitionTo。然后我们自己 push 到正确的上级。
  //
  //   不需要纠正的情况（真的是往回走一级）**不能**掐：要放行，让 Vue Router
  //   照常切页，否则地址和路由会不一致。
  function onPopState(ev) {
    if (!installed) return
    handlePop(ev, true)
  }

  // popstate 的判定内核。needEvent=true 时才真正去掐事件
  // （纯逻辑测试里没有真的 DOM 事件，掐不掐无所谓，但测的是同一段判断）。
  function handlePop(ev, needEvent) {
    const landed = normalize(window.location.pathname)
    const landedDepth = readDepth()

    // 落回了根：接受（再按一次就离开应用）
    // 注意：**只有用户真的是往回走到根**才走到这里。
    if (landed === BASE_ROOT) {
      log.push({ type: 'pop-root', from: current, depth, at: Date.now() })
      current = BASE_ROOT
      depth = 0
      markDepth(0)
      return
    }

    // 深度比当前浅 = 真的往回走了一级（浏览器退对了），放行给 Vue Router
    if (landedDepth !== null && landedDepth < depth) {
      log.push({ type: 'pop-accept', from: current, depth, landed, landedDepth, at: Date.now() })
      current = landed
      depth = landedDepth
      if (!parentOf.has(landed)) parentOf.set(landed, BASE_ROOT)
      return
    }

    // 这里要处理的两种情况：
    //   a. 深度 >= 当前：浏览器退到了「访问顺序」上的兄弟页（1→2→3→2 的那个 3）；
    //   b. 没有深度标记：退到了本会话之外的格子 —— 典型是刷新/深链进来后按 ←，
    //      上一格是登录流程的中转页（/api/v1/auth/sso/login…），整页加载，
    //      树根本来不及跑。人在应用里，就不该被带到那儿去。
    // 两种都掐掉事件，自己回上一级。
    log.push({ type: 'pop-correct', from: current, depth, landed, landedDepth, at: Date.now() })
    if (needEvent && ev && typeof ev.stopImmediatePropagation === 'function') ev.stopImmediatePropagation()
    goUp()
  }

  function install() {
    if (installed) return
    installed = true
    // capture: true —— 必须抢在 Vue Router 自己的 popstate 监听（冒泡阶段）之前跑
    window.addEventListener('popstate', onPopState, true)
    // 起始页也要进树：直接打开 /knowledge 时它是第一层
    const start = normalize(router.currentRoute && router.currentRoute.path)
    if (start === BASE_ROOT) {
      current = BASE_ROOT
      depth = 0
    } else {
      current = start
      depth = 1
      if (!parentOf.has(start)) parentOf.set(start, BASE_ROOT)
    }
    markDepth(depth)

    // ---------- 深链/刷新进来时，把「身后那一格」换成首页 ----------
    //
    // 场景：没登录时打开 /knowledge → 守卫把人送去 /login?sso… → 登录完整页跳回
    // /knowledge。这时历史是 [..., /login(/api/v1/auth/sso/login…), /knowledge]，
    // 在 /knowledge 上按 ← 会**整页加载**那个 SSO 中转页 —— 树来不及跑，
    // popstate 也拦不住（跨文档导航）。
    //
    // 浏览器不给删格子的 API，那就换一种：**把当前这一格换成首页 + 入口页**。
    //   replaceState('/')  → 当前格变成首页
    //   pushState(入口页)  → 再压一格回到入口页
    // 结果：历史 = [..., 首页, 入口页]，按 ← 就是首页，永远碰不到中转页。
    //
    // 只在「有历史可退」且入口不是首页时做，避免刷新时把首页刷成两张。
    if (start !== BASE_ROOT && window.history.length > 1) {
      try {
        const url = window.location.href
        window.history.replaceState(Object.assign({}, window.history.state, { [DEPTH_KEY]: 0 }), '', BASE_ROOT)
        window.history.pushState(Object.assign({}, window.history.state, { [DEPTH_KEY]: 1 }), '', url)
        log.push({ type: 'seed-root', entry: start, at: Date.now() })
      } catch (e) {
        // replaceState 在跨源/沙箱限制下会抛 SecurityError，退化成「不做」
        log.push({ type: 'seed-root-failed', entry: start, err: String(e), at: Date.now() })
      }
    }
  }

  function uninstall() {
    if (!installed) return
    installed = false
    window.removeEventListener('popstate', onPopState, true)
  }

  // 测试用（不参与业务）：直接走一遍「浏览器后退到了 path、深度 depth」的判定逻辑。
  // 真实场景里由 popstate 触发，这里给纯逻辑测试一个入口，免得测试只能
  // 去伪造 DOM 事件。参数与 onPopState 读到的两个值一一对应。
  function simulatePop(path, depthValue) {
    const ev = { stopImmediatePropagation() {} }
    // 让 readDepth() 读到的就是传入的深度
    window.history.state = Object.assign({}, window.history.state, { [DEPTH_KEY]: depthValue })
    window.location.pathname = path
    // 直接走判定逻辑：onPopState 开头有 installed 守卫，纯逻辑测试里没人装监听，
    // 所以这里调不经过守卫的那个内核（guard=false）。
    handlePop(ev, false)
  }

  // 调试/测试用：现在在哪一级、往上是什么
  function trail() {
    const out = []
    let p = current
    let guard = 0
    while (p && guard++ < 50) {
      out.unshift(p)
      if (p === BASE_ROOT) break
      p = parentPath(p)
    }
    if (out[0] !== BASE_ROOT) out.unshift(BASE_ROOT)
    return out
  }

  return { install, uninstall, onRouteChanged, goUp, trail, log, simulatePop, get current() { return current }, get depth() { return depth } }
}


