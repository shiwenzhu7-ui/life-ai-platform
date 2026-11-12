import request, { compactParams } from '@/utils/request'

// 购物 11 个接口，一一对应后端 modules/lifeweb/shopping/router.py。
// 页面只调这里的函数，不拼 url。
//
// 命名沿用 tasks.js：动词 + 名词。清单是 List、物品是 Item，
// 别用 Shopping 前缀——模块名已经在文件名里了。
//
// 参数顺序固定「路径参数 → query/payload → options」，
// options 是 axios 配置（{ silent: true } 关掉自动报错提示），
// 不会被拼进 query string。

// ---------------------------------------------------------------- 清单

// GET /shopping-lists → { items, total, page, size, pages }
// 每项带 item_count / unpurchased_count（后端算好的，前端不用自己数）。
//
// params.status 只认 'active' / 'archived'，不传是 'active'（归档的默认看不见）。
// 别传 status: null —— compactParams 会把它删掉，结果还是 'active'。
export function listShoppingLists(params, options) {
  return request.get('/shopping-lists', {
    params: compactParams(params),
    ...options
  })
}

// POST /shopping-lists → 201 + 单条
// payload: { name, note? }。status 固定 active，user_id 来自令牌，都别传。
export function createShoppingList(payload, options) {
  return request.post('/shopping-lists', payload, options)
}

// GET /shopping-lists/{id} → { list, items }
// 包了一层：list 是清单本身（带两个计数），items 是全部明细。
// 注意这个接口的明细**不分页**，一张清单多大就返回多大。
export function getShoppingList(listId, options) {
  return request.get(`/shopping-lists/${listId}`, options)
}

// PATCH /shopping-lists/{id} → 单条
// payload 只传要改的字段：{ name } / { note } / { status: 'archived' }。
// 传 { note: null } 是清空备注；传 {} 会被后端 422 拒绝。
export function updateShoppingList(listId, payload, options) {
  return request.patch(`/shopping-lists/${listId}`, payload, options)
}

// DELETE /shopping-lists/{id} → 204 无响应体
// **连带删掉这张清单下的所有物品**，硬删没有回收站。调用前必须二次确认。
export function deleteShoppingList(listId, options) {
  return request.delete(`/shopping-lists/${listId}`, options)
}

// ---------------------------------------------------------------- 物品（挂在清单下）

// GET /shopping-lists/{id}/items → 裸数组（不是分页对象）
// params.purchased：true 只看买过的 / false 只看没买的 / 不传看全部
// params.limit：最多几条（1~200），不传不截断
export function listShoppingItems(listId, params, options) {
  return request.get(`/shopping-lists/${listId}/items`, {
    params: compactParams(params),
    ...options
  })
}

// POST /shopping-lists/{id}/items → 201 + 单条
// payload: { name, quantity?, unit?, category_id?, note? }
// quantity 传数字或字符串都行（后端用 Decimal 接），响应里一律是字符串 "2.00"。
// source 由后端固定写 manual，别传。
export function createShoppingItem(listId, payload, options) {
  return request.post(`/shopping-lists/${listId}/items`, payload, options)
}

// ---------------------------------------------------------------- 物品（独立前缀）

// GET /shopping-items/{id} → 单条
// 物品的 id 全局唯一，所以这四个接口不需要再带 list_id 回来。
export function getShoppingItem(itemId, options) {
  return request.get(`/shopping-items/${itemId}`, options)
}

// PATCH /shopping-items/{id} → 单条
// **不能改 is_purchased**：那个字段不在后端模型里，传了也不会生效。
// 勾选要走下面的 changeItemPurchased。
export function updateShoppingItem(itemId, payload, options) {
  return request.patch(`/shopping-items/${itemId}`, payload, options)
}

// PATCH /shopping-items/{id}/purchased → 单条
// 参数是布尔值，内部包成 { is_purchased }。
// 后端保证：重复勾选不刷新 purchased_at；取消勾选会把 purchased_at 清空。
export function changeItemPurchased(itemId, isPurchased, options) {
  return request.patch(`/shopping-items/${itemId}/purchased`, { is_purchased: isPurchased }, options)
}

// DELETE /shopping-items/{id} → 204 无响应体（硬删，不可恢复）
export function deleteShoppingItem(itemId, options) {
  return request.delete(`/shopping-items/${itemId}`, options)
}