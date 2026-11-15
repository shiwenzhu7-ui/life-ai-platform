// navTree 的真机验证：在真实 life-web 上按浏览器 ← →，看地址栏是不是按「层级」走。
//
// 用户要的两条规则（原话）：
//   1 -> 2 -> 1        在 1 上，没有左箭头了
//   1 -> 2 -> 3 -> 2   在 2 上按 ← 只回 1，不是回 3
//
// 用法：
//   node scripts/navtree-check.js --url http://localhost:8080
//
// 依赖：node_modules 里的 ws；本机 Edge。
//
// 环境上的三个坑（都踩过，别再改回去）：
//   1. 无头 Edge 会推迟后台标签的 popstate —— 每一步都要叫醒标签。
//   2. Page.bringToFront 在无头下**永不返回**，front() 里容错处理。
//   3. 刚开的新标签地址是 about:blank，那里 localStorage 拒绝访问。
//      必须先 Page.navigate 到同源页，才能写令牌。
//   4. 用 location.replace 进场景而不是 Page.navigate —— 后者会往历史里压格，
//      起点就不干净了，测出来的 len 全是噪音。

'use strict'

const path = require('path')
const fs = require('fs')
const os = require('os')
const crypto = require('crypto')
const { spawn } = require('child_process')
const WebSocket = require('ws')

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 ? process.argv[i + 1] : fallback
}

const BASE = arg('url', 'http://localhost:8080')

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms))
}

// 无头下 Page.bringToFront 可能永不返回，包一层容错
async function front(client) {
  try {
    await client.send('Page.bringToFront')
  } catch (e) {
    /* 忽略：某些无头版本不认这个命令 */
  }
}

// 令牌：读 backend/.env 的 JWT_SECRET 签一张后端认的 HS256。
// 前端只看 localStorage 里的 token 和过期时间，但后端会验签 —— 所以要真签。
function resolveToken() {
  const envPath = path.join(__dirname, '../../backend/.env')
  const line = fs
    .readFileSync(envPath, 'utf8')
    .split(/\r?\n/)
    .find((l) => /^JWT_SECRET\s*=/.test(l))
  if (!line) throw new Error('backend/.env 里没找到 JWT_SECRET')
  const secret = line.split('=').slice(1).join('=').trim().replace(/^["']|["']$/g, '')
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
  const now = Math.floor(Date.now() / 1000)
  const head = b64({ alg: 'HS256', typ: 'JWT' })
  const body = b64({ sub: '1', iat: now, exp: now + 3600 })
  const sig = crypto.createHmac('sha256', secret).update(`${head}.${body}`).digest('base64url')
  return `${head}.${body}.${sig}`
}

function httpJson(url, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = require('http').request(url, { method }, (res) => {
      let buf = ''
      res.on('data', (c) => (buf += c))
      res.on('end', () => {
        try {
          resolve(JSON.parse(buf))
        } catch (e) {
          reject(new Error(`${url} 返回的不是 JSON: ${buf.slice(0, 200)}`))
        }
      })
    })
    req.on('error', reject)
    req.end()
  })
}

async function waitForDevTools(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      return await httpJson(`http://127.0.0.1:${port}/json/version`)
    } catch (e) {
      await sleep(200)
    }
  }
  throw new Error(`Edge 的 DevTools 端口 ${port} 15 秒内没起来`)
}

function connect(wsUrl, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl, { perMessageDeflate: false })
    const timer = setTimeout(() => {
      ws.terminate()
      reject(new Error('连 CDP 超时'))
    }, timeoutMs)
    ws.on('open', () => {
      clearTimeout(timer)
      resolve(ws)
    })
    ws.on('error', (e) => {
      clearTimeout(timer)
      reject(e)
    })
  })
}

function makeClient(ws) {
  let seq = 0
  const pending = new Map()
  ws.on('message', (raw) => {
    let msg
    try {
      msg = JSON.parse(raw)
    } catch (e) {
      return
    }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.error) reject(new Error(JSON.stringify(msg.error)))
      else resolve(msg.result)
    }
  })
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++seq
      pending.set(id, { resolve, reject })
      ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id)
          reject(new Error(`${method} 超时`))
        }
      }, 15000)
    })

  async function evalJs(expr) {
    const r = await send('Runtime.evaluate', {
      expression: expr,
      returnByValue: true,
      awaitPromise: true
    })
    if (r.exceptionDetails) {
      const d = r.exceptionDetails
      throw new Error(`${d.text} ${d.exception ? d.exception.description : ''}`)
    }
    return r.result.value
  }

  return { send, evalJs, close: () => ws.close() }
}

async function snapshot(client, label) {
  const s = await client.evalJs(`(() => {
    const r = window.__lifeaiNavTree
    return {
      path: location.pathname + location.search,
      len: history.length,
      trail: r && r.trail ? r.trail() : 'NO_TREE'
    }
  })()`)
  if (label) console.log(`  ${label} -> ${JSON.stringify(s)}`)
  return s
}

// 按一次浏览器的 ←，并等 popstate 稳定
async function pressBack(client, settleMs = 1200) {
  await client.evalJs(`(window.history.back(), 'ok')`)
  await front(client)
  await sleep(settleMs)
}

// 应用内跳转（等价于点侧边栏）
async function goto(client, p) {
  await client.evalJs(`(document.querySelector('#app').__vue__.$router.push('${p}'), 'ok')`)
  await sleep(700)
}

async function main() {
  const token = resolveToken()
  const port = 9400 + Math.floor(Math.random() * 400)
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'navtree-'))
  let edge = null
  let client = null
  let pass = 0
  let fail = 0
  const check = (name, ok, detail) => {
    if (ok) {
      pass++
      console.log(`  PASS ${name}`)
    } else {
      fail++
      console.log(`  FAIL ${name}${detail ? ` — ${detail}` : ''}`)
    }
  }

  try {
    edge = spawn(
      EDGE,
      [
        '--headless=new',
        `--remote-debugging-port=${port}`,
        `--user-data-dir=${userDir}`,
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-extensions',
        'about:blank'
      ],
      { stdio: 'ignore' }
    )
    await waitForDevTools(port)

    const tab = await httpJson(
      `http://127.0.0.1:${port}/json/new?${encodeURIComponent(BASE + '/')}`,
      'PUT'
    )
    client = makeClient(await connect(tab.webSocketDebuggerUrl))
    await client.send('Page.enable')
    await client.send('Runtime.enable')
    await front(client)
    await sleep(800)

    // 先落到同源页，才能写 localStorage（about:blank 的 storage 是拒绝访问的）
    await client.send('Page.navigate', { url: BASE + '/' })
    await sleep(2500)
    await front(client)
    await sleep(300)
    const injected = await client.evalJs(`(() => {
      try {
        localStorage.setItem('lifeai_admin_token', ${JSON.stringify(token)});
        localStorage.setItem('lifeai_admin_token_expires_at', String(Date.now() + 3600000));
        return 'ok';
      } catch (e) { return 'err: ' + e.message }
    })()`)
    if (injected !== 'ok') throw new Error(`写令牌失败：${injected}`)

    // 干净起点：用 location.replace 进 /users（不往历史里压格）
    await client.evalJs(`(location.replace('${BASE}/users'), 'ok')`)
    await sleep(3000)
    await front(client)
    await sleep(500)

    let s = await snapshot(client, '起点')
    if (!/\/users/.test(s.path)) throw new Error(`起点不对：${s.path}（后端在跑吗？令牌生效了吗？）`)
    check('0 导航树已挂上', s.trail !== 'NO_TREE', String(s.trail))
    check('0b 树里只有 [/ , /users]', JSON.stringify(s.trail) === JSON.stringify(['/users']), JSON.stringify(s.trail))

    // ---------- 场景 1：/users -> /usage，按 ← 回 /users ----------
    console.log('\n场景 1: /users -> /usage，按 ← 应回 /users')
    await goto(client, '/usage')
    s = await snapshot(client, '到 /usage')
    check('1a 在 /usage', /\/usage/.test(s.path), s.path)
    check('1b 树叠到 [/ , /users, /usage]', JSON.stringify(s.trail) === JSON.stringify(['/users', '/usage']), JSON.stringify(s.trail))

    await pressBack(client)
    s = await snapshot(client, '按 ←')
    check('1c 回到 /users', /\/users/.test(s.path), s.path)
    check('1d 树退到 [/ , /users]', JSON.stringify(s.trail) === JSON.stringify(['/users']), JSON.stringify(s.trail))

    // ---------- 场景 2：1→2→3→2，在 2 上按 ← 回 1，不是回 3 ----------
    console.log('\n场景 2: /users -> /usage -> /roles -> 退到 /usage -> 再按 ← 应回 /users')
    await goto(client, '/usage')
    await goto(client, '/roles')
    s = await snapshot(client, '到 /roles')
    check('2a 树叠到 [/, /users, /usage, /roles]', JSON.stringify(s.trail) === JSON.stringify(['/users', '/usage', '/roles']), JSON.stringify(s.trail))

    await pressBack(client)
    s = await snapshot(client, '按 ← 离开 /roles')
    check('2b 回到 /usage', /\/usage/.test(s.path), s.path)

    // 此刻站在 /usage 上。访问顺序里的上一格是 /roles —— 但层级上的上级是 /users
    await pressBack(client, 1500)
    s = await snapshot(client, '在 /usage 上再按 ←')
    check('2c 应回 /users（上一级），不能回 /roles', /\/users/.test(s.path), s.path)

    // ---------- 场景 3：一直退到头，停在应用内 ----------
    console.log('\n场景 3: 从 /users 一直按 ←，应停在 /users 且绝不出现 /login 或 /sso/')
    let stop = null
    let prev = null
    for (let i = 0; i < 5; i++) {
      await pressBack(client, 1200)
      stop = await snapshot(client)
      if (stop.path === '/') break
      if (prev && stop.path === prev) break
      prev = stop.path
    }
    console.log(`  停下位置 -> ${JSON.stringify(stop)}`)
    check('3a 停在根 /users', stop.path === '/users', stop.path)
    check('3b 全程没出现 /login 或 /sso/', !/login|sso/.test(stop.path), stop.path)

    // 在根上再按 ← 不该被接管，应该真的离开（或停在原地）
    await pressBack(client, 1500)
    const afterRoot = await snapshot(client, '在根上再按 ←')
    check('3c 根上再按 ← 未被树接管（离开或停在 /）', !/sso\/callback/.test(afterRoot.path), afterRoot.path)

    // ---------- 场景 4：深链进来，← 回首页 ----------
    console.log('\n场景 4: 直接打开 /roles，按 ← 应回 /')
    await client.evalJs(`(location.replace('${BASE}/roles'), 'ok')`)
    await sleep(3000)
    await front(client)
    await sleep(500)
    s = await snapshot(client, '深链落地')
    if (s.trail === 'NO_TREE') throw new Error('导航树没挂上')
    check('4a 在 /roles', /\/roles/.test(s.path), s.path)
    check('4b 深链被当成根的子节点 [/, /roles]', JSON.stringify(s.trail) === JSON.stringify(['/users', '/roles']), JSON.stringify(s.trail))
    await pressBack(client)
    s = await snapshot(client, '按 ←')
    check('4c 回到根 /users', s.path === '/users', s.path)
  } catch (e) {
    fail++
    console.log(`  ERROR ${e.message}`)
  } finally {
    try {
      if (client) client.close()
    } catch (e) {}
    try {
      if (edge) edge.kill()
    } catch (e) {}
    console.log(`\n${fail === 0 ? 'RESULT ALL PASS' : 'RESULT FAIL'}  (${pass} pass / ${fail} fail)`)
    process.exit(fail === 0 ? 0 : 1)
  }
}

main()



