// 演示脚本：在真实浏览器里按 ← / →，逐步打印「停在哪一页、历史长度、state」。
// 用来把「登录页历史锁 + 登录后整页跳」的效果摆给人看，不是断言测试
// （断言版是同目录的 history-lock-check.js）。
//
// 用法：
//   node scripts/history-demo.js                 跑 life-web + admin-web 全套演示
//   node scripts/history-demo.js --app life      只跑 life-web
//   node scripts/history-demo.js --app admin     只跑 admin-web
//   node scripts/history-demo.js --shots         顺便截图到 $env:TEMP\history-demo\
//
// 依赖：本机 Edge + node_modules 里的 ws（不装新包）。后端 8010、前端 8080/8082 要开着。
const { spawn } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')
const WebSocket = require('ws')

const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
].find((p) => fs.existsSync(p))
if (!EDGE) {
  console.error('找不到 Edge')
  process.exit(2)
}

const args = process.argv.slice(2)
const only = args.includes('--app') ? args[args.indexOf('--app') + 1] : 'all'
const wantShots = args.includes('--shots')

const APPS = {
  life: { name: 'life-web 生活端', base: 'http://localhost:8080', home: '/tasks', token: 'lifeai_token', exp: 'lifeai_token_expires_at', role: null },
  admin: { name: 'admin-web 管理后台', base: 'http://localhost:8082', home: '/users', token: 'lifeai_admin_token', exp: 'lifeai_admin_token_expires_at', role: 'lifeai_admin_last_role' }
}

const PORT = 9400 + Math.floor(Math.random() * 400)
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-demo-'))
const shotDir = path.join(os.tmpdir(), 'history-demo')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

let child = null
let ws = null
let seq = 0
const pending = new Map()

function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = ++seq
    pending.set(id, resolve)
    ws.send(JSON.stringify({ id, method, params }))
  })
}

async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails))
  return r.result && r.result.result ? r.result.result.value : undefined
}

async function shot(label) {
  if (!wantShots) return
  try {
    fs.mkdirSync(shotDir, { recursive: true })
    const r = await send('Page.captureScreenshot', { format: 'png' })
    if (r.result && r.result.data) {
      const file = path.join(shotDir, label + '.png')
      fs.writeFileSync(file, Buffer.from(r.result.data, 'base64'))
      console.log('      [截图] ' + file)
    }
  } catch (e) { /* 截图失败不影响演示 */ }
}

// 当前停在哪、历史里有什么
async function snap() {
  return evaluate(`({
    path: location.pathname,
    search: location.search,
    len: history.length,
    state: history.state,
    pwd: Boolean(document.querySelector('input[type=password]')),
    title: (document.querySelector('h1,h2,.title') || {}).innerText || ''
  })`)
}

function fmt(label, s) {
  const pin = s.state && s.state.__pin ? ' [哨兵{__pin}]' : ''
  const form = s.pwd ? ' [登录表单]' : ''
  const q = s.search ? decodeURIComponent(s.search) : ''
  console.log(
    '  ' + label.padEnd(22, ' ') + '→ 停在 ' + String(s.path).padEnd(12, ' ') + q.padEnd(26, ' ') +
    ' history.length=' + String(s.len).padEnd(3, ' ') + ' state=' + JSON.stringify(s.state) + pin + form
  )
  return s
}

async function steps(list) {
  for (const [label, action, wait] of list) {
    await evaluate(action)
    await sleep(wait || 900)
    const s = await snap()
    fmt(label, s)
    await shot(label.replace(/[^\w]+/g, '_'))
  }
}

// ---- 场景一：站在登录页，← → 按不动 ----
async function demoLock(app) {
  console.log('\n══ 场景 1｜未登录站在登录页：← 和 → 应该都按不动 ══')
  // 先把「静默探测已试过」种上：否则打开 /login 会先被扔去 Keycloak 再弹回来
  // （那是设计好的首次流程，会把演示拖成 20 秒），这里要直接看登录表单。
  await evaluate("sessionStorage.setItem('lifeai_sso_silent_tried', '1')")
  const url = app.base + '/login?_demo=' + Date.now()
  await evaluate('location.replace(' + JSON.stringify(url) + ')')
  await sleep(3500)
  await send('Page.bringToFront')
  await sleep(400)
  fmt('打开登录页', await snap())
  await shot('1-登录页')

  await steps([
    ['按 ← 第 1 次', 'history.back()'],
    ['按 ← 第 2 次', 'history.back()'],
    ['按 → 第 1 次', 'history.forward()'],
    ['按 → 第 2 次', 'history.forward()']
  ])
}

// ---- 场景二：登录成功后按 ← 回不到登录页 ----
//
// 这里需要一个「后端也认」的令牌，否则接口一 401，request.js 会把人送回
// /login?redirect=…（那是鉴权行为，会把演示搅成一团）。所以：
//   1. 优先读环境变量 DEMO_TOKEN；没有就用后端 .env 里的 JWT_SECRET 现签一张
//      （和 create_access_token 同样的载荷：sub/iat/exp）。
//   2. 签不出来就退化成「只验前端」：塞个假令牌，但仍**先拦掉网络**，
//      让前端处于「看起来已登录、接口返回空」的状态 —— 路由行为一样可观察。
async function resolveToken(app) {
  if (process.env.DEMO_TOKEN) return { token: process.env.DEMO_TOKEN, real: true }
  try {
    const envPath = path.join(__dirname, '..', '..', 'backend', '.env')
    const text = fs.readFileSync(envPath, 'utf8')
    const m = text.match(/^\s*JWT_SECRET\s*=\s*(.+)\s*$/m)
    if (!m) return { token: '', real: false }
    const secret = m[1].trim().replace(/^["']|["']$/g, '')
    const crypto = require('crypto')
    const b64u = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64')
      .replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
    const now = Math.floor(Date.now() / 1000)
    const head = b64u({ alg: 'HS256', typ: 'JWT' })
    const body = b64u({ sub: '1', iat: now, exp: now + 3600 })
    const data = head + '.' + body
    const sig = crypto.createHmac('sha256', secret).update(data).digest('base64')
      .replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
    return { token: data + '.' + sig, real: true }
  } catch (e) {
    return { token: '', real: false }
  }
}

async function demoAfterLogin(app) {
  console.log('\n══ 场景 2｜登录成功后按 ←：不能回到登录页 ══')
  const { token, real } = await resolveToken(app)
  console.log('  令牌来源：' + (real ? '后端 JWT_SECRET 现签（接口会认）' : '前端伪造（只验路由层）'))
  const useToken = token || 'demo.fake.token'
  await evaluate(`(function () {
    localStorage.setItem(${JSON.stringify(app.token)}, ${JSON.stringify(useToken)})
    localStorage.setItem(${JSON.stringify(app.exp)}, String(Date.now() + 3600000))
    ${app.role ? `localStorage.setItem(${JSON.stringify(app.role)}, 'admin')` : ''}
    return true
  })()`)
  // 等同 LoginView.leaveTo(target)：整页 replace，不是 router.replace
  await evaluate('location.replace(' + JSON.stringify(app.home) + ')')
  await sleep(3500)
  await send('Page.bringToFront')
  fmt('登录成功落地', await snap())
  await shot('2-登录成功落地')

  await steps([
    ['按 ← 第 1 次', 'history.back()'],
    ['按 ← 第 2 次', 'history.back()']
  ])
}

// ---- 场景三：登录后在应用内部正常前进后退（锁不该误伤） ----
async function demoInsideApp(app) {
  console.log('\n══ 场景 3｜登录后在应用内点侧边栏切页，再用 ← → 回去 ══')
  const sections = app.home === '/tasks'
    ? [['待办', '/tasks'], ['消费', '/expenses'], ['文档', '/documents']]
    : [['用户管理', '/users'], ['AI 用量', '/usage']]
  for (const [label, p] of sections) {
    const r = await evaluate(`(function () {
      var vm = document.querySelector('#app') && document.querySelector('#app').__vue__
      if (!vm || !vm.$router || !vm.$route) return 'no-router'
      if (vm.$route.path === ${JSON.stringify(p)}) return 'same'
      vm.$router.push(${JSON.stringify(p)}).catch(function () {})
      return 'ok'
    })()`)
    await sleep(1400)
    fmt('点「' + label + '」', await snap())
    if (r === 'no-router') break
  }
  await shot('3-应用内切页')

  await steps([
    ['按 ← 回上一页', 'history.back()'],
    ['再按 ← ', 'history.back()'],
    ['按 → 前进', 'history.forward()']
  ])
}

async function runApp(app) {
  console.log('\n\n############ ' + app.name + ' ############')
  // 干净起点：新标签打开 base，保证历史里只有它自己
  const tab = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(app.base + '/')}`, { method: 'PUT' })).json()
  if (ws) { ws.close(); await sleep(300) }
  ws = new WebSocket(tab.webSocketDebuggerUrl, { perMessageDeflate: false })
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('连 CDP 超时')), 8000)
    ws.on('open', () => { clearTimeout(timer); resolve() })
    ws.on('error', (e) => { clearTimeout(timer); reject(e) })
  })
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw)
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
  })
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Page.bringToFront')
  await sleep(2500)

  try {
    await demoLock(app)
    await demoAfterLogin(app)
    await demoInsideApp(app)
  } catch (e) {
    console.error('  演示中断：' + e.message)
  }
}

async function main() {
  child = spawn(EDGE, [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--disable-features=CalculateNativeWinOcclusion', '--no-proxy-server',
    'about:blank'
  ], { stdio: 'ignore' })
  for (let i = 0; i < 50; i += 1) {
    await sleep(200)
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      if (list.some((t) => t.type === 'page')) break
    } catch (e) { /* 还没起来 */ }
  }

  const list = only === 'all' ? ['life', 'admin'] : [only]
  for (const key of list) {
    if (!APPS[key]) { console.error('未知的 --app：' + key); continue }
    await runApp(APPS[key])
  }

  try { ws && ws.close() } catch (e) { /* ignore */ }
  try { child.kill() } catch (e) { /* ignore */ }
}

main()
  .then(() => {
    try { fs.rmSync(profile, { recursive: true, force: true }) } catch (e) { /* 用完就删 */ }
    process.exit(0)
  })
  .catch((e) => {
    console.error('演示脚本自身出错：', e.message)
    try { child.kill() } catch (x) { /* ignore */ }
    process.exit(3)
  })