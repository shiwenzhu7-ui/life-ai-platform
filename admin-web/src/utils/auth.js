// 令牌的唯一存取处。逻辑与 life-web/src/utils/auth.js 相同，
// 但**存储键不同** —— 两个前端跑在不同端口，localStorage 本来就互不可见；
// 键名也写清楚是不让「我复制粘贴时看混了」。
//
// 为什么是 localStorage 而不是 sessionStorage：
//   sessionStorage 按标签页隔离，开两个标签页对比会更别扭。代价是关掉浏览器仍登录。
//
// 安全提示：localStorage 能被任意一段注入脚本读走（XSS）。
// 后台页面不放用户可控的富文本，风险可控；以后要加，这里得重新评估。

const TOKEN_KEY = 'lifeai_admin_token'
// 过期时间戳（毫秒）。只用于本地先判断、省一次注定 401 的请求，真正判定永远在后端。
const EXPIRES_AT_KEY = 'lifeai_admin_token_expires_at'

function storage() {
  // 隐私模式 / 被禁用时 localStorage 会抛异常，全部降级成「不记住」，
  // 不能让存令牌失败把整个应用启动搞挂。
  try {
    return window.localStorage
  } catch (e) {
    return null
  }
}

export function getToken() {
  const s = storage()
  return s ? s.getItem(TOKEN_KEY) || '' : ''
}

export function getExpiresAt() {
  const s = storage()
  const raw = s ? s.getItem(EXPIRES_AT_KEY) : ''
  return raw ? Number(raw) || 0 : 0
}

// expiresIn 是登录接口返回的秒数。不传就只存令牌、不记过期时间。
export function setToken(token, expiresIn) {
  const s = storage()
  if (!s) return
  s.setItem(TOKEN_KEY, token || '')
  if (expiresIn) {
    s.setItem(EXPIRES_AT_KEY, String(Date.now() + Number(expiresIn) * 1000))
  } else {
    s.removeItem(EXPIRES_AT_KEY)
  }
}

export function clearToken() {
  const s = storage()
  if (!s) return
  s.removeItem(TOKEN_KEY)
  s.removeItem(EXPIRES_AT_KEY)
  // 角色缓存跟着令牌一起清：换账号时不能拿上一个账号的角色判断
  s.removeItem(ROLE_KEY)
}

// 故意预留 60 秒余量：卡在过期点上的请求会白跑一次 401
export function isExpired() {
  const at = getExpiresAt()
  return at > 0 && Date.now() > at - 60 * 1000
}

export function hasValidToken() {
  return Boolean(getToken()) && !isExpired()
}

// 登录成功后顺手记一笔「上次登录的是哪个角色」。
// 只用于路由守卫的**首次**放行判断（此时还没请求过 /auth/me，Vuex 里没有 user）：
//   已知非管理员 → 直接送 /403，省掉一次注定 403 的 /admin/users 请求；
//   不知道（首次访问 / 缓存被清）→ 放行，让布局去请求 /auth/me，拿到了再判。
// 它不是权限，篡改它最多让人多看一个 403 红条 —— 真正的门在 require_role("admin")。
const ROLE_KEY = 'lifeai_admin_last_role'

export function setLastRole(role) {
  const s = storage()
  if (!s) return
  if (role) s.setItem(ROLE_KEY, role)
  else s.removeItem(ROLE_KEY)
}

export function getLastRole() {
  const s = storage()
  return s ? s.getItem(ROLE_KEY) || '' : ''
}