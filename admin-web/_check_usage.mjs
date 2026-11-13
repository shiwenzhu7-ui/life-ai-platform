// 格式化与时间区间的回归自检：`npm run lint:usage`
//
// 这两块最容易悄悄错、又最难在界面上看出来：
//   1. 后端给的是**不带时区偏移**的 naive 串（2026-11-12T08:13:26），
//      被当 UTC 解析就会整体差 8 小时；
//   2. 后端时间范围是**左闭右开**，快捷区间的 end 必须是「明天 00:00:00」，
//      写成「今天 23:59:59」会漏掉最后一秒。

import { createRequire } from 'module'

const require_ = createRequire(import.meta.url)
const dayjs = require_('dayjs')

// 与 src/utils/format.js 保持同一实现（该文件是 ESM + 别名，这里不直接 import）
const formatDateTime = (value, opts = {}) => {
  if (!value) return ''
  const d = dayjs(value)
  if (!d.isValid()) return String(value)
  return d.format(opts.seconds ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD HH:mm')
}
const formatCost = (value) => {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n === 0) return '—'
  if (n < 0.01) return `¥${n.toFixed(4)}`
  return `¥${n.toFixed(2)}`
}
const formatNumber = (value) =>
  value === null || value === undefined || value === '' ? '0' : String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
const formatCompact = (value) => {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n === 0) return '0'
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(n)
}
const formatLatency = (ms) => {
  const n = Number(ms || 0)
  if (!Number.isFinite(n) || n <= 0) return '—'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}s`
  return `${n}ms`
}

const DAY = 86400000
const pad = (n) => (n < 10 ? `0${n}` : String(n))
const fmtD = (d, withTime) => {
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  return withTime ? `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : day
}
const startOfToday = () => {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}
// 与 views/usage/index.vue 的 resolveRange 同一套算法
const resolveRange = (key) => {
  const today = startOfToday()
  if (key === 'today') return [fmtD(today, true), fmtD(new Date(today.getTime() + DAY), true)]
  if (key === 'yesterday') return [fmtD(new Date(today.getTime() - DAY), true), fmtD(today, true)]
  if (key === '7d') {
    return [fmtD(new Date(today.getTime() - 6 * DAY), true), fmtD(new Date(today.getTime() + DAY), true)]
  }
  if (key === '30d') {
    return [fmtD(new Date(today.getTime() - 29 * DAY), true), fmtD(new Date(today.getTime() + DAY), true)]
  }
  return []
}

let ok = 0
let bad = 0
const check = (name, cond, extra) => {
  if (cond) {
    ok += 1
  } else {
    bad += 1
    console.log('FAIL', name, extra === undefined ? '' : JSON.stringify(extra))
  }
}

// --- 时间：naive 串必须按本地解析，不能被当成 UTC 减 8 小时
check('naive 串日期不变', formatDateTime('2026-11-12T08:13:26').startsWith('2026-11-12'), formatDateTime('2026-11-12T08:13:26'))
check('naive 串时刻不变', formatDateTime('2026-11-12T08:13:26') === '2026-11-12 08:13', formatDateTime('2026-11-12T08:13:26'))
check('带秒模式', formatDateTime('2026-11-12T08:13:26', { seconds: true }) === '2026-11-12 08:13:26')
check('空值给空串', formatDateTime(null) === '')
check('非法串原样返回', formatDateTime('not-a-date') === 'not-a-date')

// --- 数字 / 费用 / 耗时
check('千分位', formatNumber(1162692) === '1,162,692', formatNumber(1162692))
check('空值给 0', formatNumber(null) === '0')
check('M 换算', formatCompact(1162692) === '1.16M', formatCompact(1162692))
check('K 换算', formatCompact(9540) === '9.5K', formatCompact(9540))
check('0 费用给破折号', formatCost(0) === '—')
check('小额费用 4 位', formatCost(0.0012) === '¥0.0012', formatCost(0.0012))
check('常规费用 2 位', formatCost(1.5) === '¥1.50', formatCost(1.5))
check('毫秒转秒', formatLatency(12478) === '12.5s')
check('不足 1 秒', formatLatency(820) === '820ms')

// --- 区间（左闭右开）
const today = startOfToday()
const tomorrow = new Date(today.getTime() + DAY)
const ymd = (d) => fmtD(d, false)

const [ts, te] = resolveRange('today')
check('today 起点是今天 00:00:00', ts === `${ymd(today)} 00:00:00`, ts)
check('today 终点是明天 00:00:00', te === `${ymd(tomorrow)} 00:00:00`, te)

const [ys, ye] = resolveRange('yesterday')
check('yesterday 起点', ys === `${ymd(new Date(today.getTime() - DAY))} 00:00:00`, ys)
check('yesterday 终点是今天 00:00:00', ye === `${ymd(today)} 00:00:00`, ye)

const [s7, e7] = resolveRange('7d')
check('7d 起点是 6 天前（含今天共 7 天）', s7 === `${ymd(new Date(today.getTime() - 6 * DAY))} 00:00:00`, s7)
check('7d 终点是明天', e7 === `${ymd(tomorrow)} 00:00:00`, e7)

const [s30] = resolveRange('30d')
check('30d 起点是 29 天前', s30 === `${ymd(new Date(today.getTime() - 29 * DAY))} 00:00:00`, s30)

check('all 不带时间条件', resolveRange('all').length === 0)

// 边界语义：今天最后一毫秒必须落在区间内，明天零点必须落在区间外
const endTs = new Date(te.replace(' ', 'T')).getTime()
check('今天最后一毫秒 < end', today.getTime() + DAY - 1 < endTs)
check('end 恰为明天零点', endTs === tomorrow.getTime())

console.log(`${ok} passed, ${bad} failed`)
process.exit(bad ? 1 : 0)