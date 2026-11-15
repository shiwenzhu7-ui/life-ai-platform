// 截图演示：把「1→2→3→2 按 ← 只回上一级」的全过程拍下来，给人看。
//
// 用法：node scripts/navtree-shots.js --url http://localhost:8080 --root /
//   产出 $env:TEMP\navtree-shots\NN-*.png + 一份 steps.txt
//
// 与 navtree-check.js 的区别：那个只断言，这个要留下证据（截图 + 每步的地址）。

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
const ROOT_PATH = arg('root', '/')
const ROUTES = (arg('routes', '/tasks,/expenses,/knowledge') || '').split(',').map((s) => s.trim()).filter(Boolean)
const OUT = path.join(os.tmpdir(), 'navtree-shots')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function resolveToken() {
  const line = fs
    .readFileSync(path.join(__dirname, '../../backend/.env'), 'utf8')
    .split(/\r?\n/)
    .find((l) => /^JWT_SECRET\s*=/.test(l))
  const secret = line.split('=').slice(1).join('=').trim().replace(/^["']|["']$/g, '')
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
  const now = Math.floor(Date.now() / 1000)
  const h = b64({ alg: 'HS256', typ: 'JWT' })
  const b = b64({ sub: '1', iat: now, exp: now + 3600 })
  return `${h}.${b}.${crypto.createHmac('sha256', secret).update(`${h}.${b}`).digest('base64url')}`
}

function httpReq(url, method = 'GET') {
  return new Promise((res, rej) => {
    const r = require('http').request(url, { method }, (x) => {
      let b = ''
      x.on('data', (c) => (b += c))
      x.on('end', () => {
        try {
          res(JSON.parse(b))
        } catch (e) {
          rej(e)
        }
      })
    })
    r.on('error', rej)
    r.end()
  })
}

function connect(u) {
  return new Promise((res, rej) => {
    const ws = new WebSocket(u)
    ws.on('open', () => res(ws))
    ws.on('error', rej)
  })
}

function makeClient(ws) {
  let seq = 0
  const p = new Map()
  ws.on('message', (raw) => {
    const m = JSON.parse(raw)
    if (m.id && p.has(m.id)) {
      const { res, rej } = p.get(m.id)
      p.delete(m.id)
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
    }
  })
  const send = (method, params = {}) =>
    new Promise((res, rej) => {
      const id = ++seq
      p.set(id, { res, rej })
      ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => p.has(id) && rej(new Error(method + ' 超时')), 15000)
    })
  const ev = async (e) => {
    const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true })
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text)
    return r.result.value
  }
  return { send, ev }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true })
  const port = 9600 + Math.floor(Math.random() * 300)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'navshots-'))
  const edge = spawn(
    EDGE,
    ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, '--no-first-run', '--window-size=1280,820', 'about:blank'],
    { stdio: 'ignore' }
  )
  const steps = []
  let idx = 0
  try {
    for (let i = 0; i < 60; i++) {
      try {
        await httpReq(`http://127.0.0.1:${port}/json/version`)
        break
      } catch (e) {
        await sleep(200)
      }
    }
    const tab = await httpReq(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(BASE + '/')}`, 'PUT')
    const c = makeClient(await connect(tab.webSocketDebuggerUrl))
    await c.send('Page.enable')
    await c.send('Runtime.enable')
    await c.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 820, deviceScaleFactor: 1, mobile: false })
    await sleep(800)

    await c.send('Page.navigate', { url: BASE + '/' })
    await sleep(2500)
    await c.ev(`localStorage.setItem('lifeai_token', ${JSON.stringify(resolveToken())});
      localStorage.setItem('lifeai_token_expires_at', String(Date.now()+3600000));
      localStorage.setItem('lifeai_admin_token', ${JSON.stringify(resolveToken())});
      localStorage.setItem('lifeai_admin_token_expires_at', String(Date.now()+3600000)); 'ok'`)
    await c.ev(`(location.replace('${BASE}${ROUTES[0]}'), 'ok')`)
    await sleep(3000)

    async function shot(label) {
      const info = await c.ev('JSON.stringify({path: location.pathname, trail: window.__lifeaiNavTree ? window.__lifeaiNavTree.trail() : null})')
      const d = JSON.parse(info)
      const name = `${String(++idx).padStart(2, '0')}.png`
      const cap = await c.send('Page.captureScreenshot', { format: 'png' })
      fs.writeFileSync(path.join(OUT, name), Buffer.from(cap.data, 'base64'))
      const line = `${label}\t${d.path}\t${JSON.stringify(d.trail)}\t${name}`
      steps.push(line)
      console.log(line)
    }

    await shot('起点 '+ROUTES[0])
    await c.ev(`(document.querySelector('#app').__vue__.$router.push('${ROUTES[1]}'), 'ok')`)
    await sleep(900)
    await shot('进入 '+ROUTES[1])
    await c.ev(`(document.querySelector('#app').__vue__.$router.push('${ROUTES[2]}'), 'ok')`)
    await sleep(900)
    await shot('进入 '+ROUTES[2])

    // 第一次 ←
    await c.ev('(history.back(), "ok")')
    await sleep(1400)
    await shot('按 ← 第 1 次')

    // 第二次 ←
    await c.ev('(history.back(), "ok")')
    await sleep(1600)
    await shot('按 ← 第 2 次')

    // 第三次 ←
    await c.ev('(history.back(), "ok")')
    await sleep(1600)
    await shot('按 ← 第 3 次')

    fs.writeFileSync(path.join(OUT, 'steps.txt'), steps.join('\n'), 'utf8')
    console.log('\n截图目录: ' + OUT)
  } finally {
    edge.kill()
    process.exit(0)
  }
}

main()