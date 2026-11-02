import request, { compactParams } from '@/utils/request'

// 消费 6 个接口，对应后端 modules/lifeweb/expenses/router.py。
// 第二个参数是 axios 配置（如 { silent: true }），不会拼进 query string。

// GET /expenses → { items, total, page, size, pages }
// params: page, size, type, category_id, keyword, date_from, date_to
export function listExpenses(params, options) {
  return request.get('/expenses', { params: compactParams(params), ...options })
}

// POST /expenses → 201 + 单条（金额在前端一律用字符串传，避免浮点误差）
export function createExpense(payload, options) {
  return request.post('/expenses', payload, options)
}

// GET /expenses/{id}
export function getExpense(expenseId, options) {
  return request.get(`/expenses/${expenseId}`, options)
}

// PATCH /expenses/{id} → 只传要改的字段
export function updateExpense(expenseId, payload, options) {
  return request.patch(`/expenses/${expenseId}`, payload, options)
}

// DELETE /expenses/{id} → 204 无响应体
export function deleteExpense(expenseId, options) {
  return request.delete(`/expenses/${expenseId}`, options)
}

// GET /expenses/summary?month=YYYY-MM
// month 省略时后端默认当月；返回 { period, expense_total, income_total, balance, by_category, recent_7d }
export function getExpenseSummary(month, options) {
  return request.get('/expenses/summary', { params: compactParams({ month }), ...options })
}
