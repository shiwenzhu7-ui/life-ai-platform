import request, { compactParams } from '@/utils/request'

// GET /categories?scope=task → CategoryOut[]（不分页）
// scope 取值：expense / income / task / shopping / diet / exercise / mood
export function listCategories(scope, options) {
  return request.get('/categories', { params: compactParams({ scope }), ...options })
}
