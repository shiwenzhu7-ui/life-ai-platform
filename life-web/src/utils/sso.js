// 单点登录（SSO）的前端侧工具：只管「跳过去」和「把后端回来的错误翻译成人话」。
//
// 为什么单独一个文件：
//   1. 发起登录是**整页跳转**，不是 axios 请求 —— 这里连 request.js 都不能 import，
//      否则别人以为它是个普通接口调用。跳转地址要能被单测/排查时一眼找到。
//   2. 后端 /auth/sso/callback 失败时会 302 回 ?sso_error=<机器码>，
//      机器码不能直接给用户看，翻译表必须只有一份。
//
// 和 admin-web 的同名文件几乎一样，只多一个 APP_NAME：
//   Keycloak 里 life-web 是**独立的客户端**（client_id=life-web）。
//   /auth/sso/login?app=life-web 告诉后端「这次登录用 life-web 的凭据换 token、
//   成功了回 life-web 的 /sso/callback」，前端能做的只有把 app 带上。

// 本前端在 `.env` OIDC_CLIENTS / OIDC_FRONTEND_CALLBACKS 里的名字
export const APP_NAME = 'life-web'

// 后端 router.py 里 back_with_error() 的全部取值，新增一个这里就得跟着加。
const SSO_ERROR_TEXT = {
  access_denied: '你在账号中心点了取消，没有完成登录',
  sso_denied: '账号中心拒绝了这次登录，请重试',
  state_expired: '登录等待时间太长（超过 10 分钟），请重新登录',
  state_mismatch: '这次登录请求对不上号，为安全起见已作废，请重试',
  missing_code: '账号中心没有带回授权码，请重试',
  sso_token_rejected: '账号中心拒绝了我们的凭据，请联系管理员',
  sso_bad_id_token: '账号中心返回的身份凭证校验失败，请联系管理员',
  sso_bad_nonce: '账号中心返回的身份凭证对不上号，请重试',
  sso_unavailable: '连不上账号中心，请稍后再试',
  user_disabled: '这个账号已被管理员禁用，请联系管理员'
}

const FALLBACK_TEXT = '登录失败，请重试或改用账号密码登录'

// 这些码**不算错误**，不能让用户看到红字：
//   sso_silent —— 静默探测发现账号中心那边没登着，正常情况，显示密码框就行
//   unconfigured —— 后端还没配 SSO，本地账号密码登录完全可用
const QUIET_CODES = ['sso_silent', 'unconfigured']

export function isQuietSsoError(code) {
  return QUIET_CODES.indexOf(code) !== -1
}

// sso_error 机器码 → 用户看得懂的一句话。未知码也要给兜底文案，不能弹个空消息。
export function ssoErrorText(code) {
  if (!code) return FALLBACK_TEXT
  return SSO_ERROR_TEXT[code] || FALLBACK_TEXT
}

// 站内路径白名单：/tasks 可以，//evil.com 和 https://evil.com 不行。
// 后端已经过滤过一次（router.py 里那段 next_path 判断），这里是双保险：
// 当有人绕过接口直接构造 /sso/callback?token=x&next=//evil.com 时仍挡得住。
export function safeNextPath(raw, fallback = '/') {
  if (!raw || typeof raw !== 'string') return fallback
  if (!raw.startsWith('/') || raw.startsWith('//')) return fallback
  // 反斜杠在部分浏览器里等价于斜杠，/\evil.com 会被当成 //evil.com
  if (raw.includes('\\')) return fallback
  return raw
}

// 跳到后端发起 SSO。注意是 window.location 整页跳转：
// Keycloak 要接管整个浏览器，用 axios 或 router.push 都做不到。
//
// silent=true 时带 prompt=none（OIDC 静默认证）：Keycloak 绝不弹登录框，
// 已经登着就把 code 发回来（用户感觉是「秒进」），没登着就回 sso_error=sso_silent。
// 这是「A 应用登录过，打开 B 应用直接进去」的实现方式。
// 用 location.replace 而不是 href：跳去账号中心这一下**不该在历史栈里留一格**。
// 用 href 的话，登录页身后就压着「登录页」这一格，账号中心那格再压上去 ——
// 用户在登录页按「后退」要走两下才出得去，而且中间还会闪一下账号中心。
// replace 把这格换掉，代价是「从账号中心回来按后退」会直接跳过账号中心，
// 这在 Web 上本来就是正常行为（Google/GitHub 登录后也是这样）。
export function redirectToSsoLogin(nextPath = '/', silent = false) {
  const base = process.env.VUE_APP_API_BASE || '/api/v1'
  const params = [`app=${encodeURIComponent(APP_NAME)}`]
  if (nextPath && nextPath !== '/') params.push(`next=${encodeURIComponent(nextPath)}`)
  if (silent) params.push('silent=true')
  // 记下出发时的历史长度：回来之后要把这一趟留下的中转格全清掉
  markSsoDeparture()
  window.location.replace(`${base}/auth/sso/login?${params.join('&')}`)
}

// ---------- 关于「登录流程留在历史栈里的格子」----------
//
// 事实（实测过，别再来一遍）：从 /login 出发做 SSO，路上要经过
//   /api/v1/auth/sso/login → Keycloak → /api/v1/auth/sso/callback → /sso/callback
// 这串 302。浏览器给**每次重定向各记一个历史条目**，而 history.replace 只能换掉
// 当前这一格，replaceState 也只能改当前格。**没有任何 Web API 能删掉别的格。**
//
// 试过并证明行不通的：
//   history.go(-N) 再 go(+N) —— 退到跨文档的中转格时，那个文档已经被销毁，
//   当前文档的 popstate 还没来得及跑，人就被扔在中转页上了。探针实测 got=[]。
//
// 所以结论是：中转格删不掉，只能让它「不好看但不伤人」——
// 已登录的人退到 /sso/callback 时不该再看到「登录没有完成」，
// 这一点在 views/sso/Callback.vue 的 created 里处理（有令牌就直接回应用）。
//
// 下面这个标记函数保留：它记下「这一趟 SSO 是从多长的历史出发的」，
// 供 Callback 判断「我是不是被 ← 退回来的」（长度没变 = 是退回来的，
// 长度变长了 = 是新的一趟登录），从而决定是跳回去还是正常显示错误。
const SSO_BASELINE_KEY = 'lifeai_sso_baseline_len'

// 出发去做 SSO 的那一刻调用。只在「当前历史长度比上次记的大」时更新，
// 避免死循环复发时把基线越推越高。
export function markSsoDeparture() {
  try {
    const now = window.history.length
    const prev = Number(window.sessionStorage.getItem(SSO_BASELINE_KEY) || 0)
    if (!prev || now > prev) {
      window.sessionStorage.setItem(SSO_BASELINE_KEY, String(now))
    }
  } catch (e) {
    // 隐私模式：退化成「不清历史」，功能不受影响
  }
}

export function clearSsoBaseline() {
  try {
    window.sessionStorage.removeItem(SSO_BASELINE_KEY)
  } catch (e) {
    // 同上
  }
}

// 「登录中转」留下的地址：后端发起登录的接口、后端 callback、前端 callback 页。
// 只要落在这些地址上，就说明用户退到了登录流程里，不该让他看见。
// 目前由 views/sso/Callback.vue 使用：已登录时直接送回应用。
export function isSsoTransitUrl(href) {
  return /\/auth\/sso\/(login|callback|logout)/.test(href) || /\/sso\/callback/.test(href)
}

// 静默探测只试一次：不试的话每次打开登录页都要多跳一转；
// 试了不防重复的话，账号中心没登着时会被无限弹回来（/login → sso → /login → …）。
// 用 sessionStorage 而不是 localStorage：同一标签页里只认第一次，
// 用户手动回来登录时还能再探一次，但也不会被记到下次开浏览器。
const SILENT_TRIED_KEY = 'lifeai_sso_silent_tried'

export function markSilentTried() {
  try {
    window.sessionStorage.setItem(SILENT_TRIED_KEY, '1')
  } catch (e) {
    // 隐私模式禁用 storage 就退化成「每个人手工点按钮」，不影响正确性
  }
}

export function clearSilentTried() {
  try {
    window.sessionStorage.removeItem(SILENT_TRIED_KEY)
  } catch (e) {
    // 同上
  }
}

export function silentAlreadyTried() {
  try {
    return window.sessionStorage.getItem(SILENT_TRIED_KEY) === '1'
  } catch (e) {
    return false
  }
}

// ---------- 退出（V2.0）----------

// Keycloak 的 id_token，只在「这次登录」期间有用：退出时当 id_token_hint 交给
// 后端，后端转给 Keycloak，Keycloak 才知道该清哪个会话。
//
// 为什么存 sessionStorage 而不是 localStorage：
//   1. 关掉标签页就该失效 —— 它和「这一次登录」绑定；
//   2. 它是 Keycloak 签的、能证明身份的令牌，长期留在盘上等于多一个泄露面。
// 为什么存前端而不是后端：
//   后端无状态（不建会话表），退出的凭据必须由调用方带回来。
const ID_TOKEN_KEY = 'lifeai_sso_id_token'

// id_token 的 aud —— 也就是「这个 token 是哪个应用换来的」。
//
// 为什么必须和 id_token 一起存：
//   Keycloak 的注销端点有个硬规则：一旦带上 id_token_hint，就会拿这个 token
//   的 aud 去比对请求里的 client_id，对不上直接报「无效重定向uri」（400 白页）。
//   同一个浏览器里两个前端共用一套 Cookie，但各自换到的 id_token 是不同
//   client 签的。如果在 8080 登录过、再到 8082 退出，手里那个 token 的 aud
//   是 life-web，而请求带的是 admin-web，Keycloak 就翻脸了。
//   所以退出时先对一下，对不上干脆不带 hint —— 不带 hint 一样能清会话。
const AUD_KEY = 'lifeai_sso_id_token_aud'

// aud 可能是字符串（单值）也可能是数组（多值），统一成字符串数组。
function audList(claims) {
  const aud = claims && claims.aud
  if (!aud) return []
  return Array.isArray(aud) ? aud.map(String) : [String(aud)]
}

// 读出 JWT 的 payload。只读不验签：它的用途只是「本地比对一下 unverified 的
// aud」，真正的验签在后端（decode_id_token）。这里手写 base64 解码是为了不去
// 依赖 atob 的补位规则。
function readClaims(jwtToken) {
  try {
    const parts = String(jwtToken).split('.')
    if (parts.length < 2) return null
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
    const binary = window.atob(padded)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
    const json = new TextDecoder('utf-8').decode(bytes)
    return JSON.parse(json)
  } catch (e) {
    return null
  }
}

export function saveIdToken(idToken, app = APP_NAME) {
  if (!idToken || typeof idToken !== 'string') return
  const claims = readClaims(idToken)
  const aud = audList(claims)
  const owner = aud.indexOf(app) >= 0 ? app : aud[0] || app
  try {
    window.sessionStorage.setItem(ID_TOKEN_KEY, idToken)
    window.sessionStorage.setItem(AUD_KEY, owner)
  } catch (e) {
    // 隐私模式下拿不到 storage：退出会退化成「只退本地」，不影响登录
  }
}

// 取出并清掉 id_token，**只在本应用能认领它时返回**，否则返回空串。
// 认领条件：token 的 aud 里包含本应用名，或者没有记录归属时以 aud 首项为准。
// 认不领也一并删掉 —— 那是别的应用的东西，留着只会下次再踩同一个坑。
export function takeIdToken(app = APP_NAME) {
  try {
    const value = window.sessionStorage.getItem(ID_TOKEN_KEY) || ''
    const owner = window.sessionStorage.getItem(AUD_KEY) || ''
    window.sessionStorage.removeItem(ID_TOKEN_KEY)
    window.sessionStorage.removeItem(AUD_KEY)
    if (!value) return ''
    const aud = audList(readClaims(value))
    if (!aud.length) return value // 解不出来就信它，交给 Keycloak 去拒
    const mine = aud.indexOf(app) >= 0
    const matchesOwner = !owner || owner === app
    return mine && matchesOwner ? value : ''
  } catch (e) {
    return ''
  }
}

export function clearIdToken() {
  try {
    window.sessionStorage.removeItem(ID_TOKEN_KEY)
    window.sessionStorage.removeItem(AUD_KEY)
  } catch (e) {
    // 同上
  }
}

// 退出：整页跳到后端的 /auth/sso/logout/confirm，带上 id_token 和自己要回的地址。
//
// 为什么整页跳而不是 axios：
//   后端要 302 到**另一个域**的 Keycloak，axios 跟不了跨域跳转；只有浏览器
//   能带着用户走过去。跳完之后 Keycloak 会清掉自己的会话 Cookie，再 302
//   回到 redirect 指定的登录页 —— 那一刻两个前端就都真的退出去了。
//
// 为什么是 /confirm 而不是 /logout：
//   /logout 只负责「回到前端、让前端把 id_token 带上来」，免得我们把 id_token
//   放在 URL 里暴露给一段先执行 JS 的页面。这里 id_token 已经在手上，直接
//   交给 confirm 一步到位。
//
// 为什么 id_token 要「认领」才带（takeIdToken 里判断 aud）：
//   Keycloak 拿 id_token_hint 的 aud 去比对 client_id，对不上就是 400
//   「无效重定向uri」。跨应用（在 8080 登录、到 8082 退出）时手里那个 token
//   不是本应用签的，带了反而炸；不带 hint 照样能清 Keycloak 会话，只是少一层
//   「确认这次退出就是给这个会话的」。宁可少带，也不能让退出卡在 400。
//
// 为什么用 location.replace 而不是 location.href：
//   href 会在历史里**再压一格**。退出这条路上已经有 Keycloak 的注销页和
//   post_logout_redirect 回来的登录页两跳，如果起点也留一格，用户按后退就会
//   退回「刚才那个已经退出的后台页」。replace 把起点这一格直接换掉，后退栈
//   里就不存在它了。
export function redirectToSsoLogout({ redirect = '/login' } = {}) {
  const base = process.env.VUE_APP_API_BASE || '/api/v1'
  const params = [`app=${encodeURIComponent(APP_NAME)}`]
  if (redirect) params.push(`redirect=${encodeURIComponent(redirect)}`)
  const idToken = takeIdToken(APP_NAME)
  if (idToken) params.push(`id_token=${encodeURIComponent(idToken)}`)
  window.location.replace(`${base}/auth/sso/logout/confirm?${params.join('&')}`)
}