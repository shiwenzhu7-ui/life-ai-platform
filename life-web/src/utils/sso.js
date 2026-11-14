// 单点登录（SSO）的前端侧工具：只管「跳过去」和「把后端回来的错误翻译成人话」。
//
// 为什么单独一个文件：
//   1. 发起登录是**整页跳转**，不是 axios 请求 —— 这里连 request.js 都不能 import，
//      否则别人以为它是个普通接口调用。跳转地址要能被单测/排查时一眼找到。
//   2. 后端 /auth/sso/callback 失败时会 302 回 ?sso_error=<机器码>，
//      机器码不能直接给用户看，翻译表必须只有一份。

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
export function redirectToSsoLogin(nextPath = '/') {
  const base = process.env.VUE_APP_API_BASE || '/api/v1'
  const query = nextPath && nextPath !== '/' ? `?next=${encodeURIComponent(nextPath)}` : ''
  window.location.href = `${base}/auth/sso/login${query}`
}