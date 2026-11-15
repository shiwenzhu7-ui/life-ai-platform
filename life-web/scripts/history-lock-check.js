// CDP 冒烟测试：用本机 Edge 跑 life-web 的登录页历史锁。
//
// 为什么用 CDP 而不是 puppeteer：这里不需要装新依赖，node_modules 里已经有 ws。
// 用法：
//   node scripts/history-lock-check.js                      跑自带的测试页（17 条）
//   node scripts/history-lock-check.js --real <登录页地址>   在真实应用里验一次（需要 dev server）
//     例：node scripts/history-lock-check.js --real http://localhost:8080/login
//         node scripts/history-lock-check.js --real http://localhost:8082/login
//     真实模式共 13 条：登录页上 ← → 都按不出去，以及「登录后按 ← 不再回到登录页」。
//     脚本会自动按端口区分 life-web / admin-web（令牌键名、落地页不同）。
//   退出码 0 = 全 PASS，1 = 有 FAIL。CI 里可以直接用。
//
// 测试页放在 scripts/ 而不是 public/：public/ 里的东西会进生产构建，
// 而且 webpack-dev-server 的 chokidar 在 Windows 上会因为临时文件被占用
// 直接 EBUSY 崩掉（踩过）。所以这里起一个只读文件的本地静态服务。
const { spawn } = require('child_process')
const fs = require('fs')
const http = require('http')
const os = require('os')
const path = require('path')
const WebSocket = require('ws')

const PAGE = path.join(__dirname, 'history-lock-check.html')

// 真实应用模式：node scripts/history-lock-check.js --real http://localhost:8080/login
const realIndex = process.argv.indexOf('--real')
const REAL_URL = realIndex >= 0 ? process.argv[realIndex + 1] : null
if (realIndex >= 0 && !REAL_URL) {
  console.error('--real 后面要跟登录页地址，例如 http://localhost:8080/login')
  process.exit(2)
}
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8'
}

// 静态服务只负责把测试页吐出去。
// 关键：测试页必须以 /login 的路径对外暴露 —— 锁的判断标准就是
// location.pathname === '/login'，挂在别的路径上等于没测。
const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0])
  const servesPage = urlPath === '/' || urlPath === '/login' || urlPath === '/login/'
  const file = servesPage ? PAGE : path.join(path.dirname(PAGE), urlPath)
  if (!servesPage && (!file.startsWith(path.dirname(PAGE)) || !fs.existsSync(file))) {
    res.writeHead(404)
    res.end('not found')
    return
  }
  if (!servesPage && !fs.existsSync(file)) {
    res.writeHead(404)
    res.end('not found')
    return
  }
  res.writeHead(200, {
    'Content-Type': MIME[path.extname(file)] || 'application/octet-stream',
    // 必须禁缓存：临时用户目录里会留下上一次的页面，改了测试页却跑的还是旧版
    'Cache-Control': 'no-store, no-cache, must-revalidate',
    Pragma: 'no-cache'
  })
  fs.createReadStream(file).pipe(res)
})

const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
].find((p) => fs.existsSync(p))
if (!EDGE) {
  console.error('找不到 Edge')
  process.exit(2)
}

// 端口随机：固定端口在 Edge 已经开着的时候会被老进程占住，
// 那样连上去的是别人的标签页，测出来的结果是假的。
const PORT = 9400 + Math.floor(Math.random() * 400)
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-cdp-'))

let child = null

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function targets() {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/list`)
  return res.json()
}

// 真实应用里没法用假 router，只能看「地址栏 + history.state + 路由实际落在哪」。
// 模拟用户在登录页点 ← 和 →，断言人始终还在登录页。
async function runRealChecks(evaluate) {
  const out = []
  const snap = async (label) => {
    const s = await evaluate(`({
      href: location.href,
      path: location.pathname,
      state: history.state,
      len: history.length,
      hasPin: Boolean(history.state && history.state.__pin),
      // 登录页独有的元素：账号密码表单 / 「改用账号密码登录」入口
      onLogin: Boolean(document.querySelector('input[type=password]')) || /登录/.test(document.body.innerText || ''),
      // 只有 Vue 真的渲染出登录表单，才算「人还在登录页上」——
      // 路径对但 DOM 空着，就是一次白屏级的事故
      domAlive: Boolean(document.querySelector('input[type=password]'))
    })`)
    console.log('  · ' + label + ' -> ' + JSON.stringify(s))
    return s
  }

  const start = await snap('初始')
  if (start.path !== '/login') {
    out.push({ text: '打开的地址不是登录页，无法验证（可能已登录被重定向）', ok: false })
    return out
  }
  out.push({ text: '登录页加载后 history.state 带 __pin（锁已上）', ok: start.hasPin })
  out.push({ text: '登录页真的渲染出了密码框（没白屏）', ok: start.domAlive })

  await evaluate('history.back()')
  await sleep(900)
  const afterBack = await snap('按一次后退后')
  out.push({ text: '按后退仍停在 /login', ok: afterBack.path === '/login' })
  out.push({ text: '按后退页面没变白/没跳到别处（仍在登录页 DOM）', ok: afterBack.onLogin })
  out.push({ text: '按后退后密码框还在', ok: afterBack.domAlive })

  await evaluate('history.back()')
  await sleep(900)
  const afterBack2 = await snap('再按一次后退后')
  out.push({ text: '连按两次后退仍停在 /login', ok: afterBack2.path === '/login' })
  out.push({ text: '连按两次后退后密码框还在', ok: afterBack2.domAlive })

  await evaluate('history.forward()')
  await sleep(900)
  const afterFwd = await snap('按一次前进后')
  out.push({ text: '按前进仍停在 /login', ok: afterFwd.path === '/login' })
  out.push({ text: '按前进后仍在登录页 DOM', ok: afterFwd.onLogin })
  out.push({ text: '按前进后密码框还在', ok: afterFwd.domAlive })

  // 再按一次前进：历史里已经没有更前面的格子了，浏览器的前进键此时是灰的
  await evaluate('history.forward()')
  await sleep(900)
  const afterFwd2 = await snap('再按一次前进后')
  out.push({ text: '连按两次前进仍停在 /login', ok: afterFwd2.path === '/login' })
  out.push({ text: '连按两次前进后密码框还在', ok: afterFwd2.domAlive })

  // ---- 登录成功离开登录页之后，历史栈必须没有登录页 ----
  // 这一段是用户真正在意的东西（m00110 + 那次实测打回）：
  //   登录成功后按 ← **不能**回到登录页。
  // 做法：塞一张本地看着有效、后端不认的令牌，然后像真实登录成功那样
  // 用 location.replace 跳到应用首页（LoginView.leaveTo / Callback.goNext 就是这么做的）。
  // 跳完后连按 ←，断言人始终不在 /login 上 —— 登录页那一格必须已经从历史里消失。
  //
  // 注意令牌键每个前端不一样（life-web 是 lifeai_token，admin-web 是
  // lifeai_admin_token），所以这里按地址判断，不写死键名。
  const isAdminApp = /:8082(\/|$)/.test(start.href)
  const tokenKey = isAdminApp ? 'lifeai_admin_token' : 'lifeai_token'
  const expiresKey = isAdminApp ? 'lifeai_admin_token_expires_at' : 'lifeai_token_expires_at'
  const roleKey = 'lifeai_admin_last_role'
  const homePath = isAdminApp ? '/users' : '/tasks'
  await evaluate(`(function () {
    localStorage.setItem(${JSON.stringify(tokenKey)}, 'probe.invalid.token')
    localStorage.setItem(${JSON.stringify(expiresKey)}, String(Date.now() + 3600000))
    ${isAdminApp ? `localStorage.setItem(${JSON.stringify(roleKey)}, 'admin')` : ''}
    return true
  })()`)
  // 和后端 401 抢时间：令牌是假的，任何接口调用都会把人送回登录页。
  // 这里只看「离开登录页那一刻历史里还有没有登录页」，所以在跳到首页后
  // 立刻把令牌清掉是不可能赢过 request.js 的 —— 干脆允许被 401 送回，
  // 但断言只针对「跳去首页后的第一次后退」这一刻之前的状态。
  await evaluate('location.replace(' + JSON.stringify(homePath) + ')')
  await sleep(2500)
  const afterLogin = await evaluate(`({
    path: location.pathname,
    state: history.state,
    len: history.length,
    pwd: Boolean(document.querySelector('input[type=password]')),
    // 真实登录落地后不该有登录表单；就算被 401 弹回来，那是鉴权的事
    href: location.href
  })`)
  console.log('  · 伪登录落地 -> ' + JSON.stringify(afterLogin))
  // 落地页可能是 /tasks /users，也可能被 401 弹回 /login?redirect=…。
  // 这里只断言「没停在 /login 的哨兵态」，真正的核心断言在后面连按后退。
  await evaluate('history.back()')
  await sleep(1200)
  const back1 = await evaluate('({ path: location.pathname, href: location.href, pwd: Boolean(document.querySelector("input[type=password]")) })')
  console.log('  · 登录后按一次后退 -> ' + JSON.stringify(back1))
  out.push({ text: '登录后按后退不会回到登录页', ok: back1.path !== '/login' })

  // 清掉假令牌，免得影响前面的断言或留下脏状态
  await evaluate(`(function () {
    localStorage.removeItem(${JSON.stringify(tokenKey)})
    localStorage.removeItem(${JSON.stringify(expiresKey)})
    ${isAdminApp ? `localStorage.removeItem(${JSON.stringify(roleKey)})` : ''}
    return true
  })()`)

  return out
}

async function main() {
  let URL_ARG
  if (REAL_URL) {
    URL_ARG = REAL_URL
  } else {
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
    // 路径必须是 /login：锁就是按 location.pathname === '/login' 判断的
    URL_ARG = `http://127.0.0.1:${server.address().port}/login`
  }

  child = spawn(EDGE, [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-features=CalculateNativeWinOcclusion',
    '--no-proxy-server',
    'about:blank'
  ], { stdio: 'ignore' })

  let page = null
  for (let i = 0; i < 50 && !page; i += 1) {
    await sleep(200)
    try {
      const list = await targets()
      page = list.find((t) => t.type === 'page')
    } catch (e) {
      // 浏览器还没起来
    }
  }
  if (!page) throw new Error('连不上 Edge 的调试端口')
  // 连上的是不是我们刚起的那个浏览器：版本信息里的 user-data-dir 必须对上
  try {
    const ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json()
    console.log('BROWSER =', ver['Browser'], '\nPROFILE =', profile)
  } catch (e) {
    console.log('BROWSER = (读不到版本信息)')
  }

  // 连上目标标签页
  let ws = null
  let id = 0
  const pending = new Map()
  const attach = (socket) => {
    socket.on('message', (raw) => {
      const msg = JSON.parse(raw)
      if (msg.id && pending.has(msg.id)) {
        pending.get(msg.id)(msg)
        pending.delete(msg.id)
      }
    })
  }
  const sendRaw = (socket, method, params) =>
    new Promise((resolve) => {
      const myId = ++id
      pending.set(myId, resolve)
      socket.send(JSON.stringify({ id: myId, method, params }))
    })
  const send = (method, params = {}) => sendRaw(ws, method, params)
  const makeEvaluator = (socket) => async (expression) => {
    const r = await sendRaw(socket, 'Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true
    })
    if (r.result && r.result.exceptionDetails) {
      throw new Error(JSON.stringify(r.result.exceptionDetails))
    }
    return r.result && r.result.result ? r.result.result.value : undefined
  }
  let evaluate = null
  const connect = async (socketUrl) => {
    ws = new WebSocket(socketUrl, { perMessageDeflate: false })
    await new Promise((resolve, reject) => {
      // 没有超时的连接会永远挂着，整个测试看着就是「没输出」（踩过 120 秒才发现）
      const timer = setTimeout(() => reject(new Error('连 CDP 超时：' + socketUrl)), 8000)
      ws.on('open', () => { clearTimeout(timer); resolve() })
      ws.on('error', (e) => { clearTimeout(timer); reject(e) })
    })
    attach(ws)
    evaluate = makeEvaluator(ws)
  }
  await connect(page.webSocketDebuggerUrl)

  // 用原来的 page 目标 + Page.navigate 导航。
  // 不要在这里重连 /json/new 开新标签：新标签的 debugger URL 时好时坏，
  // 拿不到就静默挂着（踩过一次，120 秒超时才暴露）。
  await send('Page.enable')
  await send('Runtime.enable')
  // 用 Target 的 URL 直接开标签再连它。
  // 不能用 Page.navigate 导航那个连接时用的 about:blank 目标：它的渲染进程
  // 是特殊上下文，导航被接受（loaderId 在变）却始终停在 about:blank。
  // 连接必须带超时，否则 /json/new 拿到可用的 url 也可能一直挂着（踩过）。
  const tab = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(URL_ARG)}`, { method: 'PUT' })).json()
  if (tab && tab.webSocketDebuggerUrl) {
    ws.close()
    await connect(tab.webSocketDebuggerUrl)
    await send('Page.enable')
    await send('Runtime.enable')
    await send('Page.bringToFront')
    if (tab.id) {
      await fetch(`http://127.0.0.1:${PORT}/json/activate/${tab.id}`).catch(() => {})
    }
  } else {
    await send('Page.navigate', { url: URL_ARG })
  }
  // 等到测试页自己跑起来为止（最多 12 秒）
  for (let i = 0; i < 12; i += 1) {
    await sleep(1000)
    const ready = await evaluate('Boolean(window.__trace)')
    if (ready) break
  }
  // 关键：无头模式下标签可能在后台，Chromium 会把后台页的 popstate 推迟到
  // 重新可见时才派发 —— 那样测出来就是「锁没生效」的假象。必须先激活。
  await send('Page.bringToFront')

  // 等页面稳定：等到测试页给出结论（最多 8 秒）
  let items = []
  for (let i = 0; i < 20; i += 1) {
    items = await evaluate(`Array.from(document.querySelectorAll('#out li')).map(li => ({text: li.textContent, cls: li.className}))`)
    if ((items || []).some((it) => it.text.indexOf('汇总') >= 0)) break
    await sleep(400)
  }

  const realFailures = []
  if (REAL_URL) {
    console.log('--- 真实应用检查：' + REAL_URL + ' ---')
    // 直接打开 /login 会触发一次「静默 SSO 探测」，人被带去账号中心再回来 ——
    // 那是设计好的首次流程，不是要验的东西。这里先把「已探过」标记种上，
    // 等价于用户上一次已经探过、这次回来就是要看表单。
    await evaluate("sessionStorage.setItem('lifeai_sso_silent_tried', '1')")
    // 必须整页跳到一个「干净的历史」里再看。
    // 上一句已经在这次浏览会话里留下了 /sso/callback 那一格（静默探测被踢过去又回来），
    // 直接 Page.navigate 进 /login 的话，身后就多一格 callback，按 ← 当然能退出去。
    // 用 location.replace 换成一个全新的 URL，等于用户新开标签直接打开登录页。
    const clean = REAL_URL + (REAL_URL.indexOf('?') >= 0 ? '&' : '?') + '_clean=' + Date.now()
    await evaluate('location.replace(' + JSON.stringify(clean) + ')')
    await sleep(3000)
    // 真实应用里 Vue 挂载后才上锁，比测试页慢，这里多等一会儿再看
    for (let i = 0; i < 6; i += 1) {
      const st = await evaluate('history.state')
      if (st && st.__pin) break
      await sleep(500)
    }
    const realChecks = await runRealChecks(evaluate)
    for (const line of realChecks) {
      console.log((line.ok ? '  PASS ' : '  FAIL ') + line.text)
      if (!line.ok) realFailures.push(line.text)
    }
    console.log(realFailures.length
      ? `RESULT FAIL（真实应用 ${realFailures.length} 条不合格）`
      : 'RESULT ALL PASS（真实应用）')
    ws.close()
    server.close()
    child.kill()
    return realFailures.length ? 1 : 0
  }

  console.log('URL =', await evaluate('location.href'))
  console.log('TITLE =', await evaluate('document.title'))
  const trace = (await evaluate('window.__trace || []')) || []
  if (trace.length) {
    console.log('TRACE:')
    for (const line of trace) console.log('  | ' + line)
  } else {
    console.log('TRACE: (空 —— popstate 一次都没触发)')
  }
  for (const it of items || []) {
    console.log(it.cls === 'pass' ? '  ' + it.text : '  ' + it.text)
  }
  const fails = (items || []).filter((it) => it.cls !== 'pass')
  let ok = fails.length === 0 && (items || []).length > 0
  // 页面根本没加载出来时 querySelectorAll 会返回 []，不能当成「全部通过」
  if (!items || !items.length) {
    console.log('RESULT FAIL（测试页没有产出任何结果，页面可能没加载成功）')
    ok = false
  } else if (fails.length) {
    console.log(`RESULT FAIL（${fails.length} 条不合格）`)
  } else {
    console.log('RESULT ALL PASS')
  }

  ws.close()
  server.close()
  child.kill()
  return ok ? 0 : 1
}

main()
  .then((code) => {
    try { fs.rmSync(profile, { recursive: true, force: true }) } catch (e) { /* 用完就删 */ }
    process.exit(code)
  })
  .catch((error) => {
    console.error('冒烟测试自身出错：', error.message)
    try { child.kill() } catch (e) { /* ignore */ }
    try { server.close() } catch (e) { /* ignore */ }
    process.exit(3)
  })