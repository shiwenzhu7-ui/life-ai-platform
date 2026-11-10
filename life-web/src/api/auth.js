import request from '@/utils/request'

// 认证 4 个接口，一一对应后端 modules/shared/auth/router.py。

// POST /auth/login → { access_token, token_type, expires_in, user }
// 故意不用 silent：登录失败要让 request.js 把后端那句「用户名或密码错误」弹出来。
export function login(payload, options) {
  return request.post('/auth/login', payload, options)
}

// POST /auth/register → 201 { id, username, nickname, role }
// 注册**不会**自动登录（后端设计如此），前端拿到结果后要再调一次 login。
export function register(payload, options) {
  return request.post('/auth/register', payload, options)
}

// GET /auth/me → { id, username, nickname, role }
export function me(options) {
  return request.get('/auth/me', options)
}

// POST /auth/logout → 204。本地 JWT 阶段后端是空实现，前端清令牌即可。
export function logout(options) {
  return request.post('/auth/logout', null, options)
}