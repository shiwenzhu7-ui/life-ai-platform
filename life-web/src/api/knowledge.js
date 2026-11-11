import request, { compactParams } from '@/utils/request'

// 知识库 4 个接口，一一对应后端 modules/lifeweb/knowledge/router.py。
// 页面只调这里的函数，不拼 url。
//
// 和 tasks.js 一样，第二个参数是 axios 配置（如 { silent: true } 关掉自动报错提示），
// 全部用 ...options 透传给 request。每个函数的参数顺序固定为
// 「路径参数 → query/payload → options」，别自己加花样。

// POST /knowledge/documents → 202 + 单条（此时 status 是 pending）
// 参数是原生 File 对象（el-upload 的 file 或 input.files[0]），内部自己拼 FormData。
//
// 千万不要手动设 Content-Type：axios 认出 FormData 后会自己写
// multipart/form-data; boundary=xxx，手写会漏掉 boundary，
// 后端拿到的就是「缺 file 字段」的 422。
//
// timeout 单独放大到 120 秒：全局那 10 秒是给 JSON 接口的，
// 20MB 走代理传起来可能要好几十秒。options 里传 timeout 可以覆盖。
export function uploadDocument(file, options) {
  const form = new FormData()
  // 第三个参数是文件名，必须给：不写的话浏览器会把 file 转成字符串
  form.append('file', file, file.name)
  return request.post('/knowledge/documents', form, {
    timeout: 120000,
    ...options
  })
}

// GET /knowledge/documents → { items, total, page, size, pages }
// params: page, size, status（pending/parsing/chunking/embedding/ready/failed）, keyword
export function listDocuments(params, options) {
  return request.get('/knowledge/documents', {
    params: compactParams(params),
    ...options
  })
}

// GET /knowledge/documents/{id} → { document, chunks }
// 注意返回的是包了一层的结果：document 是这个文档，
// chunks 是切块预览（每块 { id, chunk_index, page_no, content, token_count }）。
//
// params.chunk_limit 控制带多少条切块（后端默认 50，0~200）。
// 轮询状态时传 { chunk_limit: 0 }，省掉一次切块查询。
export function getDocument(documentId, params, options) {
  return request.get(`/knowledge/documents/${documentId}`, {
    params: compactParams(params),
    ...options
  })
}

// DELETE /knowledge/documents/{id} → 204 无响应体（硬删，切块、向量、磁盘文件一起清掉，不可恢复）
export function deleteDocument(documentId, options) {
  return request.delete(`/knowledge/documents/${documentId}`, options)
}