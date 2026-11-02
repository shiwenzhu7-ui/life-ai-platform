import request, { compactParams } from '@/utils/request'

// 待办 6 个接口，一一对应后端 modules/lifeweb/tasks/router.py。
// 页面只调这里的函数，不拼 url。
//
// 每个函数的第二个参数是 axios 的配置（如 { silent: true } 关掉自动报错提示），
// 它不会被拼进 query string——silent 只是前端行为，不该出现在后端日志里。

// GET /tasks → { items, total, page, size, pages }
// params: page, size, status[], priority, keyword, due_from, due_to, overdue, category_id
export function listTasks(params, options) {
  return request.get('/tasks', { params: compactParams(params), ...options })
}

// POST /tasks → 201 + 单条
export function createTask(payload, options) {
  return request.post('/tasks', payload, options)
}

// GET /tasks/{id} → 单条
export function getTask(taskId, options) {
  return request.get(`/tasks/${taskId}`, options)
}

// PATCH /tasks/{id} → 只传要改的字段（后端按 model_fields_set 判断三态）
export function updateTask(taskId, payload, options) {
  return request.patch(`/tasks/${taskId}`, payload, options)
}

// PATCH /tasks/{id}/status → { status: 'todo' | 'doing' | 'done' | 'cancelled' }
export function changeTaskStatus(taskId, status, options) {
  return request.patch(`/tasks/${taskId}/status`, { status }, options)
}

// DELETE /tasks/{id} → 204 无响应体
export function deleteTask(taskId, options) {
  return request.delete(`/tasks/${taskId}`, options)
}
