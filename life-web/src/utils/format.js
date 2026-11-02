import dayjs from 'dayjs'

// ---------------------------------------------------------------------------
// 时间/金额的唯一格式化出口。页面里不允许再出现 dayjs(...) 或 toFixed。
// ---------------------------------------------------------------------------

// 后端所有时间都是「已经转到北京时间」的 ISO 串，例如 2026-10-31T13:56:49+08:00。
// 展示只做截断，绝对不要用 dayjs 再转一次时区——后端 to_beijing_iso 已经做对了。
export function formatDateTime(value) {
  if (!value) return ''
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm') : String(value)
}

export function formatDate(value) {
  if (!value) return ''
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DD') : String(value)
}

export function formatMonth(value) {
  if (!value) return ''
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM') : String(value)
}

// 发给后端时用的序列化：必须带 +08:00 偏移（形如 2026-11-02T13:30:00+08:00）。
// 踩过的坑：发裸 UTC 串会被后端当成北京时间再推 8 小时，直接错一天。
export function toApiDateTime(value) {
  if (!value) return null
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DDTHH:mm:ssZ') : null
}

// 金额在后端是 Decimal 转出来的字符串（"1280.50"），前端只加千分位、不做任何算术：
// 浮点相加会出现 0.1 + 0.2 = 0.30000000000000004，金额计算一律交给后端 summary。
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
