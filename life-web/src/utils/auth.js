// 令牌的唯一存取处。
//
// 为什么单开一个文件：B7 选的是 localStorage，但这是个**可以有替代方案**的决定
// （Cookie+HttpOnly 更安全，代价是后端要改发 Cookie 并加 CSRF 防护）。
// 全项目只有这里碰存储，将来要换，改这一个文件就够，不用全项目搜索 localStorage。
//
// 为什么是 localStorage 而不是 sessionStorage：
//   sessionStorage 按**标签页**隔离 —— 标签 A 登录了，标签 B 打开 /tasks 还是未登录。
//   多人测试时开两个标签页对比会很别扭，所以选 localStorage。
//   代价：关掉浏览器再打开仍然是登录状态。真正的「记住我」开关需要后端双令牌，
//   V1.2 不做（详见 B7 的讨论）。
//
// 安全提示：localStorage 里的令牌能被任意一段注入脚本读走（XSS）。
// 本项目没有把用户输入渲染成 HTML 的地方，风险可控；如果以后加了富文本，
// 这里要重新评估。

const TOKEN_KEY = 'lifeai_token'
// 过期时间戳（毫秒）。存它只为了「本地先判断，省一次注定 401 的请求」，
// 真正的判定始终在后端 —— 改本地时钟绕不过去。
const EXPIRES_AT_KEY = 'lifeai_token_expires_at'

function storage() {
  // 隐私模式 / 被禁用的极端情况下 localStorage 抛异常，这里全部降级成「不记住」，
  // 不能让一个存令牌失败把整个应用启动搞挂。
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
}

// 本地判断「这张令牌八成已经没用了」。
// 故意预留 60 秒余量：刚好卡在过期点上的请求会白跑一次 401，不如提前算它过期。
export function isExpired() {
  const at = getExpiresAt()
  return at > 0 && Date.now() > at - 60 * 1000
}

// 有没有一张「看起来还能用」的令牌。路由守卫用它决定放不放行。
export function hasValidToken() {
  return Boolean(getToken()) && !isExpired()
}