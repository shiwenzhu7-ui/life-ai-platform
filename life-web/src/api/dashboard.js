import request from '@/utils/request'

// GET /dashboard/overview
// 返回 { tasks:{todo,doing,done,cancelled,overdue,today_due},
//        expense:{current_month_total,last_month_total,today_total},
//        recent_7d:[{date,expense}], focus_tasks:[{id,title,due_at,status,is_overdue}] }
export function getOverview(options) {
  return request.get('/dashboard/overview', options)
}
