// 单点登录（SSO）的前端侧工具：只管「跳过去」和「把后端回来的错误翻译成人话」。
//
// 为什么单独一个文件：
//   1. 发起登录是**整页跳转**，不是 axios 请求 —— 这里连 request.js 都不能 import，
//      否则别人以为它是个普通接口调用。跳转地址要能被单测/排查时一眼找到。
//   2. 后端 /auth/sso/callback 失败时会 302 回 ?sso_error=<机器码>，
//      机器码不能直接给用户看，翻译表必须只有一份。
//
// 和 life-web 的同名文件几乎一样，只多一个 APP_NAME：
//   Keycloak 里 admin-web 是**独立的客户端**（client_id=admin-web）。
//   /auth/sso/login?app=admin-web 告诉后端「这次登录用 admin-web 的凭据换 token、
//   成功了回 admin-web 的 /sso/callback」，前端能做的只有把 app 带上。

// 本前端在 `.env` OIDC_CLIENTS / OIDC_FRONTEND_CALLBACKS 里的名字
export const APP_NAME = 'admin-web'

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

// 站内路径白名单：/users 可以，//evil.com 和 https://evil.com 不行。
// 后端已经过滤过一次（router.py 里那段 next_path 判断），这里是双保险：
// 当有人绕过接口直接构造 /sso/callback?token=x&next=//evil.com 时仍挡得住。
export function safeNextPath(raw, fallback = '/users') {
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
export function redirectToSsoLogin(nextPath = '/', silent = false) {
  const base = process.env.VUE_APP_API_BASE || '/api/v1'
  const params = [`app=${encodeURIComponent(APP_NAME)}`]
  if (nextPath && nextPath !== '/') params.push(`next=${encodeURIComponent(nextPath)}`)
  if (silent) params.push('silent=true')
  window.location.href = `${base}/auth/sso/login?${params.join('&')}`
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

export function saveIdToken(idToken) {
  if (!idToken || typeof idToken !== 'string') return
  try {
    window.sessionStorage.setItem(ID_TOKEN_KEY, idToken)
  } catch (e) {
    // 隐私模式下拿不到 storage：退出会退化成「只退本地」，不影响登录
  }
}

export function takeIdToken() {
  try {
    const v = window.sessionStorage.getItem(ID_TOKEN_KEY) || ''
    window.sessionStorage.removeItem(ID_TOKEN_KEY)
    return v
  } catch (e) {
    return ''
  }
}

export function clearIdToken() {
  try {
    window.sessionStorage.removeItem(ID_TOKEN_KEY)
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
export function redirectToSsoLogout({ redirect = '/login' } = {}) {
  const base = process.env.VUE_APP_API_BASE || '/api/v1'
  const params = [`app=${encodeURIComponent(APP_NAME)}`]
  if (redirect) params.push(`redirect=${encodeURIComponent(redirect)}`)
  const idToken = takeIdToken()
  if (idToken) params.push(`id_token=${encodeURIComponent(idToken)}`)
  window.location.href = `${base}/auth/sso/logout/confirm?${params.join('&')}`
}