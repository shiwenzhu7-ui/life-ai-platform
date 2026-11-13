import request, { compactParams } from '@/utils/request'

// ---------------------------------------------------------------------------
// 管理员身份与用户/用量查询。
// 登录复用 life-web 的 POST /auth/login —— 同一个账号体系，后端不区分「从哪端登录」，
// 是不是管理员由 GET /admin/users 这类接口自己判（非管理员会拿到 403）。
// ---------------------------------------------------------------------------

// POST /auth/login → { access_token, token_type, expires_in, user }
// 故意不用 silent：登录失败要把后端那句「用户名或密码错误」原样弹出来
export function login(payload, options) {
  return request.post('/auth/login', payload, options)
}

// GET /auth/me → { id, username, nickname, role }
export function me(options) {
  return request.get('/auth/me', options)
}

// POST /auth/logout → 204。JWT 无状态，后端是空实现，前端清令牌即可
export function logout(options) {
  return request.post('/auth/logout', null, options)
}

// GET /admin/users → Page[{ id, username, nickname, email, role, status,
//   auth_source, created_at, last_login_at, usage_count }]
// params: { page, size, keyword, role, status }
export function listUsers(params, options) {
  return request.get('/admin/users', {
    params: compactParams(params),
    ...options
  })
}

// PATCH /admin/users/{id} ← { role?, status? }（至少给一个）
// 只能改身份信息，改不了用户名/密码。改自己 or 降级最后一个管理员会被后端 422 拦住
export function updateUser(userId, payload, options) {
  return request.patch(`/admin/users/${userId}`, payload, options)
}

// GET /admin/roles → [{ role, label, summary, abilities: string[] }]
// 静态文案，后端写死在 schema.ROLE_CATALOG 里，不查库
export function listRoles(options) {
  return request.get('/admin/roles', options)
}

// GET /admin/usage → { items: [...], total, page, size, pages, summary }
// params: { page, size, user_id, keyword, start, end, status }
// summary 是**整个筛选范围**的合计，不随翻页变化 —— 前端拿它画顶部的四张卡。
// start / end 的左闭右开由后端负责，前端只管传区间
export function listUsage(params, options) {
  return request.get('/admin/usage', {
    params: compactParams(params),
    ...options
  })
}