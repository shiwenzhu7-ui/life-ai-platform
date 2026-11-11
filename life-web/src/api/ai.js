import request, { compactParams } from '@/utils/request'

// AI 助手 6 个接口，一一对应后端 modules/lifeweb/ai/router.py。
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

// GET /ai/sessions/{id} → 单条会话（字段与列表每一项完全一致）
// 打开 /ai?session=5 时用它拿标题：不能指望「列表里恰好有这一条」——
// 列表有分页，以后还会有搜索筛选，翻不到就永远显示不出标题。
export function getSession(sessionId, options) {
  return request.get(`/ai/sessions/${sessionId}`, options)
}

// GET /ai/sessions/{id}/messages?order=desc → { items, total, page, size, pages }
// order=desc 时第一页是最新 N 条，前端按「旧 → 新」显示要自己反转。
// 每项除了正文，还带 pending_action：这条助手消息产生的确认卡片（没有则为 null），
// 刷新页面后靠它把卡片原样画回来。
// references 同理：这一轮检索命中的知识库出处，存在消息上，刷新后照样能画出来。
// 没有引用时是空数组，不是 null。
export function listMessages(sessionId, params, options) {
  return request.get(`/ai/sessions/${sessionId}/messages`, {
    params: compactParams(params),
    ...options
  })
}

// POST /ai/chat → { session_id, message_id, reply, pending_action, usage, references }
// 模型可能要几十秒，必须覆盖全局 10s 超时：
// 超时早了，后端其实成功、库里已有回复，用户却以为没发出去会重发一次。
// pending_action 不为 null 时表示「模型想改数据但还没改」，前端要渲染确认卡片。
// references 是这一轮检索到的知识库出处（文件/页码/段落/摘要），没有命中时为空数组。
export function chat(payload, options) {
  return request.post('/ai/chat', payload, { timeout: 70000, ...options })
}

// POST /ai/actions/{id}/confirm → { action_id, action_type, status, preview, created, executed_at }
// created 是真正建出来的业务记录（{ type: 'task', id: 150 }），卡片拿它做「查看待办」的跳转。
// payload_override 只在用户改了草稿时传；不传（或传 null）= 按卡片原样执行。
// 409 = 这张卡已被处理过（用户连点、或另一个标签页先点了），410 = 已过期。
export function confirmAction(actionId, payloadOverride, options) {
  return request.post(
    `/ai/actions/${actionId}/confirm`,
    { payload_override: payloadOverride || null },
    options
  )
}

// POST /ai/actions/{id}/cancel → 同上，created 恒为 null
export function cancelAction(actionId, options) {
  return request.post(`/ai/actions/${actionId}/cancel`, {}, options)
}

// GET /ai/sessions/{id}/messages 返回的每条消息现在也带 pending_action（可为 null）：
// 刷新页面时靠它把卡片原样画回来，见 listMessages 的注释。
