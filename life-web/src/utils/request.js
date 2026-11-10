import axios from 'axios'
import { Message } from 'element-ui'

import { clearToken, getToken } from './auth'

// ---------------------------------------------------------------------------
// 全项目唯一的请求出口。
// 页面/组件不允许直接 import axios，也不允许自己拼 url 和弹错误提示。
// ---------------------------------------------------------------------------

// 422 的 detail.errors[].loc 形如 ["body", "title"]、["query", "size"]、["path", "task_id"]，
// 最后一项是字段名，翻成中文读起来才不费劲。没收录的直接显示原字段名。
const FIELD_LABELS = {
  title: '标题',
  description: '描述',
  priority: '优先级',
  due_at: '截止时间',
  category_id: '分类',
  type: '类型',
  amount: '金额',
  account: '账户',
  occurred_at: '发生时间',
  tags: '标签',
  note: '备注',
  status: '状态',
  month: '月份',
  keyword: '关键词',
  page: '页码',
  size: '每页条数',
  date_from: '开始日期',
  date_to: '结束日期'
}

function fieldLabel(loc) {
  if (!Array.isArray(loc) || loc.length === 0) return ''
  const last = loc[loc.length - 1]
  if (last === 'body' || last === 'query' || last === 'path') return ''
  return FIELD_LABELS[last] || last
}

// Pydantic v2 自定义 validator 抛的 ValueError 会被包成 "Value error, 标题不能为空"，
// 去掉前缀后正好是后端写的中文提示。
function cleanMsg(msg) {
  if (typeof msg !== 'string' || !msg) return '参数不合法'
  return msg.replace(/^Value error,\s*/, '')
}

function buildValidationMessage(body) {
  const errors = body && body.detail && body.detail.errors
  if (!Array.isArray(errors) || errors.length === 0) return ''
  return errors
    .slice(0, 3) // 最多拼三条，再多用户也看不下去
    .map((item) => {
      const label = fieldLabel(item.loc)
      const msg = cleanMsg(item.msg)
      return label ? `${label}：${msg}` : msg
    })
    .join('；')
}

// 按状态码决定给用户看什么。后端错误体统一是
// { code, message, request_id, detail }，所以 4xx 优先用它自己的 message。
function pickMessage(status, body) {
  if (status === 422) {
    return buildValidationMessage(body) || '参数校验失败'
  }
  if (status === 401) return '登录已过期，请重新登录'
  if (status === 403) return (body && body.message) || '没有权限'
  if (status === 404) return (body && body.message) || '数据不存在'
  if (status === 409) return (body && body.message) || '状态冲突，请刷新后重试'
  if (status >= 500) return `服务器开小差了（${status}），请稍后重试`
  return (body && body.message) || `请求失败（${status}）`
}

const service = axios.create({
  // 开发环境走 vue.config.js 的 proxy（/api → 127.0.0.1:8010），生产环境靠同域反代
  baseURL: process.env.VUE_APP_API_BASE,
  timeout: 10000,
  // FastAPI 的 list[TaskStatus] 要的是 ?status=todo&status=done，
  // axios 默认会序列化成 status[]=todo，所以关掉方括号。
  paramsSerializer: { indexes: null }
})

service.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ---------- 鉴权：登录失效时统一处理 ----------
//
// 两个必须区分的场景：
//   1. 业务接口 401 —— 令牌没了/坏了/过期了 → 清令牌、跳登录、记住原来在哪
//   2. 登录接口 401 —— 用户只是**输错了密码** → 绝不能跳走
//      否则会出现「在登录页输错一次，页面重载、表单被清空」的诡异体验。
//
// 这里不用 router 而用 window.location.replace：request.js 被 router 的模块依赖链
// 外面引用着，import router 容易绕成循环依赖；而且换页时整份内存状态本来也该重置。

function redirectToLogin() {
  const current = window.location
  const target = `${current.pathname}${current.search}`
  if (target === '/' || target.startsWith('/login')) {
    window.location.replace('/login')
    return
  }
  // 用 encodeURIComponent 而不是 URLSearchParams：后者会把 '/' 编码成 %2F，
  // 虽然合法，但地址栏里一团乱，排查时读不出来。
  window.location.replace(`/login?redirect=${encodeURIComponent(target)}`)
}

// 防止并发请求同时 401 时弹出一排红条、跳转被触发好几次
let redirecting = false

function handleUnauthorized() {
  clearToken()
  if (redirecting) return
  redirecting = true
  Message.warning('登录已过期，请重新登录')
  // 稍等一下再跳，让用户看清提示；否则页面一闪就换了，反而莫名其妙
  window.setTimeout(redirectToLogin, 800)
}

function isAuthEntryUrl(url) {
  const path = String(url || '')
  return path.includes('/auth/login') || path.includes('/auth/register')
}

service.interceptors.response.use(
  // 后端成功时直接返回业务对象，没有 { code, data } 包装，所以这里直接解一层
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

      // 401 且不是登录/注册接口 → 令牌失效，清本地状态并回登录页。
      // /auth/me 的 silent 只影响「弹不弹红条」，不影响这里 ——
      // 它同样说明登录态已经没了。
      if (status === 401 && !isAuthEntryUrl(config.url)) {
        handleUnauthorized()
        // 这条路径上不再弹 error 红条，handleUnauthorized 的 warning 已经说明了原因
        const err = new Error(message)
        err.status = status
        err.code = code
        err.detail = (body && body.detail) || null
        err.response = response
        err.authExpired = true
        return Promise.reject(err)
      }
    }

    // config.silent === true 时不弹提示，交给调用方自己处理
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
