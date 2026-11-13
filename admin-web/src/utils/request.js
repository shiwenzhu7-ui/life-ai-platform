import axios from 'axios'
import { Message } from 'element-ui'

import { clearToken, getToken } from './auth'

// ---------------------------------------------------------------------------
// 后台全项目唯一的请求出口。与 life-web/src/utils/request.js 是同一套规则，
// 唯一的实质差异是 403 的文案：后台的 403 只有一种含义 —— 不是管理员。
// ---------------------------------------------------------------------------

const FIELD_LABELS = {
  username: '用户名',
  nickname: '昵称',
  email: '邮箱',
  role: '角色',
  status: '状态',
  user_id: '用户',
  keyword: '关键词',
  page: '页码',
  size: '每页条数',
  start: '开始时间',
  end: '结束时间'
}

function fieldLabel(loc) {
  if (!Array.isArray(loc) || loc.length === 0) return ''
  const last = loc[loc.length - 1]
  if (last === 'body' || last === 'query' || last === 'path') return ''
  return FIELD_LABELS[last] || last
}

// Pydantic v2 的 ValueError 会被包成 "Value error, xxx"，去掉前缀正好是后端写的中文
function cleanMsg(msg) {
  if (typeof msg !== 'string' || !msg) return '参数不合法'
  return msg.replace(/^Value error,\s*/, '')
}

function buildValidationMessage(body) {
  const errors = body && body.detail && body.detail.errors
  if (!Array.isArray(errors) || errors.length === 0) return ''
  return errors
    .slice(0, 3)
    .map((item) => {
      const label = fieldLabel(item.loc)
      const msg = cleanMsg(item.msg)
      return label ? `${label}：${msg}` : msg
    })
    .join('；')
}

// 422 有两种来源，结构不一样，以前只认第一种，于是业务拒绝全被笼统成「参数校验失败」：
//
//   ① FastAPI/Pydantic 校验失败   body = {detail: {errors: [{loc, msg, type}]}}
//      —— 没有 message 字段，要自己从 errors 拼。
//   ② 后端的业务拒绝（AppError）  body = {code, message, detail}，例如
//      {code: "CANNOT_GRANT_ADMIN", message: "只有根管理员可以把别人设为管理员"}
//      —— 后端已经写好了给人看的中文，直接用，别再盖一层「参数校验失败」。
//
// 所以顺序是：先看 body.message（业务原话），再按 Pydantic 的 errors 拼，
// 两者都没有才退回笼统文案。
function pickMessage(status, body) {
  if (status === 422) {
    if (body && body.message) return body.message
    return buildValidationMessage(body) || '参数校验失败'
  }
  // 登录接口的 401 是「密码错了」，不是「登录过期」—— 这里必须先分
  if (status === 401) {
    if (body && body.message) return body.message
    return '登录已过期，请重新登录'
  }
  // 403 的两种来源：role 不足（require_role）、账号被禁用（USER_DISABLED）
  if (status === 403) return (body && body.message) || '需要管理员权限'
  if (status === 404) return (body && body.message) || '数据不存在'
  if (status === 409) return (body && body.message) || '状态冲突，请刷新后重试'
  if (status >= 500) return `服务器开小差了（${status}），请稍后重试`
  return (body && body.message) || `请求失败（${status}）`
}

const service = axios.create({
  baseURL: process.env.VUE_APP_API_BASE,
  timeout: 10000,
  // FastAPI 的 list 参数要 ?status=a&status=b，关掉方括号
  paramsSerializer: { indexes: null }
})

service.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// ---------- 401 统一处理 ----------
// 登录接口的 401 是「密码错了」，绝不能跳走；业务接口的 401 才是登录态没了。
function redirectToLogin() {
  const current = window.location
  const target = `${current.pathname}${current.search}`
  if (target === '/' || target.startsWith('/login')) {
    window.location.replace('/login')
    return
  }
  window.location.replace(`/login?redirect=${encodeURIComponent(target)}`)
}

let redirecting = false

function handleUnauthorized() {
  clearToken()
  if (redirecting) return
  redirecting = true
  Message.warning('登录已过期，请重新登录')
  window.setTimeout(redirectToLogin, 800)
}

function isAuthEntryUrl(url) {
  const path = String(url || '')
  return path.includes('/auth/login')
}

service.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const config = error.config || {}
    const response = error.response
    let status = 0
    let code = 'NETWORK_ERROR'
    let message = ''

    if (!response) {
      message =
        error.code === 'ECONNABORTED'
          ? '请求超时，请检查后端服务是否在运行'
          : '连接不上后端，请确认 8010 端口的服务已启动'
    } else {
      status = response.status
      const body = response.data || {}
      code = body.code || `HTTP_${status}`
      message = pickMessage(status, body)

      if (status === 401 && !isAuthEntryUrl(config.url)) {
        handleUnauthorized()
        const err = new Error(message)
        err.status = status
        err.code = code
        err.detail = (body && body.detail) || null
        err.response = response
        err.authExpired = true
        return Promise.reject(err)
      }
    }

    if (!config.silent) Message.error(message)

    const err = new Error(message)
    err.status = status
    err.code = code
    err.detail = (response && response.data && response.data.detail) || null
    err.response = response
    return Promise.reject(err)
  }
)

// 去掉 undefined / null / 空串 / 空数组，避免拼出 ?keyword=&page=1 这种脏参数
export function compactParams(params) {
  const out = {}
  Object.keys(params || {}).forEach((key) => {
    const value = params[key]
    if (value === undefined || value === null || value === '') return
    if (Array.isArray(value) && value.length === 0) return
    out[key] = value
  })
  return out
}

export default service