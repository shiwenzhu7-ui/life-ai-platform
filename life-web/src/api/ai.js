import request, { compactParams } from '@/utils/request'

// AI 助手 5 个接口，一一对应后端 modules/lifeweb/ai/router.py。
// 页面只调这里的函数，不拼 url。
//
// 第二个参数是 axios 配置（{ silent: true } 关掉自动报错提示），
// 它不会被拼进 query string。

// GET /ai/sessions → { items, total, page, size, pages }
// 每项：{ id, title, model, message_count, last_message_at, created_at, updated_at }
export function listSessions(params, options) {
  return request.get('/ai/sessions', { params: compactParams(params), ...options })
}

// POST /ai/sessions → 201 + 单条
// 请求体可省略（后端补默认标题「新对话」）；传 { title } 则用指定标题
export function createSession(payload, options) {
  return request.post('/ai/sessions', payload || {}, options)
}

// GET /ai/sessions/{id}/messages?order=desc → { items, total, page, size, pages }
// order=desc 时第一页是最新 N 条，前端按「旧 → 新」显示要自己反转
export function listMessages(sessionId, params, options) {
  return request.get(`/ai/sessions/${sessionId}/messages`, {
    params: compactParams(params),
    ...options
  })
}

// POST /ai/chat → { session_id, message_id, reply, usage }
// 模型可能要几十秒，必须覆盖全局 10s 超时：
// 超时早了，后端其实成功、库里已有回复，用户却以为没发出去会重发一次。
export function chat(payload, options) {
  return request.post('/ai/chat', payload, { timeout: 70000, ...options })
}
