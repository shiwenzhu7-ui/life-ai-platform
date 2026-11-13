import dayjs from 'dayjs'

// ---------------------------------------------------------------------------
// 时间/数字格式化的唯一出口。页面里不允许再出现 dayjs(...) 或 toFixed。
//
// 关于时区：后端 `ai_usage_logs.created_at` 是 `DateTime`（无时区列），
// 序列化出来是 `2026-11-12T08:13:26` 这种**不带偏移**的串；而 users 表的
// 时间同样不带偏移。所以这里一律按**浏览器本地时间**解释，不转时区。
// 前后端和 MySQL 都在同一台机器（Asia/Shanghai），不需要做换算。
// ---------------------------------------------------------------------------

// 完整时间。seconds 为 true 时带秒（调用明细需要秒级分辨）
export function formatDateTime(value, { seconds = false } = {}) {
  if (!value) return ''
  const d = dayjs(value)
  if (!d.isValid()) return String(value)
  return d.format(seconds ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD HH:mm')
}

export function formatDate(value) {
  if (!value) return ''
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DD') : String(value)
}

// 发送给后端时用的序列化：后端收的是无时区的 naive datetime，
// 所以这里也不能带偏移，否则 FastAPI 会当成 UTC 再折算一次
export function toApiDateTime(value) {
  if (!value) return null
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DDTHH:mm:ss') : null
}

// 金额是后端 Decimal 转出的数字/字符串（"0.0421"），这里只做展示，不参与计算
export function formatMoney(value) {
  if (value === null || value === undefined || value === '') return '0.00'
  const text = String(value)
  const negative = text.charAt(0) === '-'
  const body = negative ? text.slice(1) : text
  const parts = body.split('.')
  const integer = (parts[0] || '0').replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const decimal = (parts[1] || '').padEnd(2, '0').slice(0, 2)
  return `${negative ? '-' : ''}${integer}.${decimal}`
}

// 费用估算：单价没配时后端恒给 0，显示「—」比「¥0.00」诚实
export function formatCost(value) {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n === 0) return '—'
  if (n < 0.01) return `¥${n.toFixed(4)}`
  return `¥${n.toFixed(2)}`
}

// token 数量：加分位（12,400 比 12400 好读）
export function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '0'
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

// 大数换算成 K / M，卡片上放得下
export function formatCompact(value) {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n === 0) return '0'
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(n)
}

// 毫秒转可读耗时：12478 → 12.5s，820 → 820ms
export function formatLatency(ms) {
  const n = Number(ms || 0)
  if (!Number.isFinite(n) || n <= 0) return '—'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}s`
  return `${n}ms`
}