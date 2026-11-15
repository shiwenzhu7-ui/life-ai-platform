// navTree 的纯逻辑测试：不开浏览器，用假 router + 假 window 跑。
//
// 为什么要单独测：树的规则就是用户给的那两条（1→2→1 / 1→2→3→2），
// 写错了在浏览器里表现为「按了没反应」或「退过头」，很难从截图分辨。
//
// 运行：node scripts/navtree-test.js
//   （需要先 npm i -D @babel/register 或用 --experimental 转译；
//    这里直接用极简的 import 替换跑，不引入构建链）

'use strict'

const fs = require('fs')
const path = require('path')
const vm = require('vm')

const src = fs.readFileSync(path.join(__dirname, '../src/utils/navTree.js'), 'utf8')
// 去掉 export 关键字，在 vm 里跑成普通函数
const code = src.replace(/^export /gm, '')

const fakeHistory = {
  state: null,
  length: 2,
  replaceState(s) {
    this.state = s
  },
  pushState(s) {
    this.state = s
    this.length += 1
  }
}
const ctx = {
  window: {
    history: fakeHistory,
    location: { pathname: '/', href: 'http://localhost/' },
    addEventListener() {},
    removeEventListener() {}
  },
  console
}
vm.createContext(ctx)
vm.runInContext(code + '\nthis.createNavTree = createNavTree;', ctx)
const { createNavTree } = ctx

// 假 router：能 push、能读 currentRoute，push 会同步改 currentRoute
function makeRouter(startPath) {
  const r = {
    currentRoute: { path: startPath },
    pushed: [],
    push(p) {
      this.pushed.push(p)
      this.currentRoute = { path: p }
      return Promise.resolve()
    }
  }
  return r
}

let pass = 0
let fail = 0
function check(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a === e) {
    pass++
    console.log(`  PASS ${name}`)
  } else {
    fail++
    console.log(`  FAIL ${name}\n       期望 ${e}\n       实际 ${a}`)
  }
}

async function main() {
  // 场景 A：1 → 2 → 1，在 1 上没有上级
  console.log('场景 A: 1 -> 2 -> 1，在 1 上再按 ← 应交给浏览器')
  {
    const router = makeRouter('/')
    const tree = createNavTree({ router })
    tree.onRouteChanged(router.currentRoute)
    router.push('/tasks'); tree.onRouteChanged(router.currentRoute) // 1 -> 2
    check('A1 在 /tasks，trail = [/, /tasks]', tree.trail(), ['/', '/tasks'])

    const handled = tree.goUp() // 在 2 上按 ←
    check('A2 从 /tasks 按 ← 被接管', handled, true)
    await new Promise((r) => setTimeout(r, 0))
    check('A3 落在 /', router.currentRoute.path, '/')
    check('A4 在根上再按 ← 不接管', tree.goUp(), false)
  }

  // 场景 B：1 → 2 → 3 → 2，在 2 上按 ← 必须回 1，不是回 3
  console.log('场景 B: 1 -> 2 -> 3 -> 2，在 2 上按 ← 应回 1')
  {
    const router = makeRouter('/')
    const tree = createNavTree({ router })
    tree.onRouteChanged(router.currentRoute)

    router.push('/tasks'); tree.onRouteChanged(router.currentRoute)
    router.push('/expenses'); tree.onRouteChanged(router.currentRoute)
    check('B1 trail = [/, /tasks, /expenses]', tree.trail(), ['/', '/tasks', '/expenses'])

    // 用户按 ← 从 /expenses 回 /tasks：浏览器把地址改成 /tasks，afterEach 先跑
    // （navTree 判定为「后退」而忽略），随后 popstate 接受这一格。
    // 这里要同时模拟两步，缺一步就不是真实时序。
    router.currentRoute = { path: '/tasks' }
    tree.onRouteChanged(router.currentRoute) // route-ignore-backward：不改 current
    tree.simulatePop('/tasks', 1) // pop-accept：把 current 挪到 /tasks
    check('B2 回到 /tasks 后 trail = [/, /tasks]', tree.trail(), ['/', '/tasks'])

    // 现在在 /tasks 上按 ←：应回 /，不能回 /expenses
    const handled = tree.goUp()
    check('B3 从 /tasks 按 ← 被接管', handled, true)
    await new Promise((r) => setTimeout(r, 0))
    check('B4 落在 / 而不是 /expenses', router.currentRoute.path, '/')
    check('B5 trail = [/]', tree.trail(), ['/'])
  }

  // 场景 C：回头路不该长出新节点（1 → 2 → 3 → 2 → 3 的来回）
  //
  // 真实时序里每次「回到走过的页」都是：地址变 → afterEach（被忽略）→ popstate 接受。
  // 只调 onRouteChanged 不调 simulatePop，等于模拟「用户点导航到已经去过的页」，
  // 那是另一种情况（会正常建新层级），因此这里必须补上 popstate 这一步。
  console.log('场景 C: 反复来回，树不该越走越长')
  {
    const router = makeRouter('/')
    const tree = createNavTree({ router })
    tree.onRouteChanged(router.currentRoute)
    const backTo = (p, d) => {
      router.currentRoute = { path: p }
      tree.onRouteChanged(router.currentRoute)
      tree.simulatePop(p, d)
    }
    router.push('/tasks'); tree.onRouteChanged(router.currentRoute)
    router.push('/expenses'); tree.onRouteChanged(router.currentRoute)
    // 退到 /tasks，再退到 /expenses（访问顺序上的兄弟），再退到 /tasks
    backTo('/tasks', 1)
    backTo('/expenses', 2)
    backTo('/tasks', 1)
    check('C1 在 /tasks 上 trail 仍是 [/, /tasks]', tree.trail(), ['/', '/tasks'])
  }

  // 场景 D：query 变化当同一个节点
  console.log('场景 D: /tasks?task=3 与 /tasks 是同一节点')
  {
    const router = makeRouter('/')
    const tree = createNavTree({ router })
    tree.onRouteChanged(router.currentRoute)
    router.push('/tasks'); tree.onRouteChanged(router.currentRoute)
    router.push('/tasks?task=3'); tree.onRouteChanged(router.currentRoute)
    check('D1 trail = [/, /tasks]', tree.trail(), ['/', '/tasks'])
  }

  // 场景 E：直接打开深页（/knowledge）当第一页
  console.log('场景 E: 直接打开 /knowledge，← 应回到 /')
  {
    const router = makeRouter('/knowledge')
    const tree = createNavTree({ router })
    tree.onRouteChanged(router.currentRoute)
    check('E1 trail = [/, /knowledge]', tree.trail(), ['/', '/knowledge'])
    const handled = tree.goUp()
    check('E2 被接管', handled, true)
    await new Promise((r) => setTimeout(r, 0))
    check('E3 落在 /', router.currentRoute.path, '/')
  }

  console.log(`\n${fail === 0 ? 'RESULT ALL PASS' : 'RESULT FAIL'}  (${pass} pass / ${fail} fail)`)
  process.exit(fail === 0 ? 0 : 1)
}

main()
