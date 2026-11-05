<template>
  <div class="dash">
    <p v-if="loading" class="dash__hint">加载中…</p>
    <p v-else-if="!overview" class="dash__hint">数据没取到，稍后再试。</p>

    <template v-else>
      <p class="dash__greeting">{{ greeting }}，演示用户</p>

      <div class="dash__cards">
        <!-- 左：待办概览 -->
        <OverviewCard title="待办概览" to="/tasks" link-text="查看待办">
          <ul class="stats">
            <li class="stats__item">
              <span class="stats__label">未开始</span>
              <span class="stats__value">{{ tasks.todo }}</span>
            </li>
            <li class="stats__item">
              <span class="stats__label">进行中</span>
              <span class="stats__value">{{ tasks.doing }}</span>
            </li>
            <li class="stats__item">
              <span class="stats__label">已完成</span>
              <span class="stats__value">{{ tasks.done }}</span>
            </li>
            <li class="stats__item">
              <span class="stats__label">逾期</span>
              <span class="stats__value" :class="{ 'stats__value--danger': tasks.overdue > 0 }">
                {{ tasks.overdue }}
              </span>
            </li>
            <li class="stats__item stats__item--wide">
              <span class="stats__label">今日到期</span>
              <span class="stats__value">{{ tasks.today_due }}</span>
            </li>
          </ul>
        </OverviewCard>

        <!-- 右：本月消费 -->
        <OverviewCard title="本月消费" to="/expenses" link-text="查看消费">
          <p class="amount">
            <span class="amount__unit">¥</span>
            <span class="amount__value">{{ money(expense.current_month_total) }}</span>
          </p>
          <p class="amount__sub">
            上月 <b>¥{{ money(expense.last_month_total) }}</b>
            <span class="amount__gap">今日 <b>¥{{ money(expense.today_total) }}</b></span>
          </p>
          <SparkBar :days="recent7d" />
        </OverviewCard>
      </div>

      <OverviewCard
        class="dash__focus"
        title="逾期与今日到期（最多 5 条）"
        to="/tasks"
        link-text="查看全部"
      >
        <p v-if="!focusTasks.length" class="focus__empty">今天没有待办，休息一下</p>

        <ul v-else class="focus">
          <li v-for="row in focusTasks" :key="row.id" class="focus__row">
            <span class="focus__flag" :class="{ 'focus__flag--on': row.is_overdue }">
              {{ row.is_overdue ? '!' : '' }}
            </span>
            <span class="focus__title" :title="row.title">{{ row.title }}</span>
            <span class="focus__time">{{ whenLabel(row.due_at) }}</span>
            <span class="focus__due" :class="{ 'focus__due--overdue': row.is_overdue }">
              {{ dueLabel(row) }}
            </span>
          </li>
        </ul>
      </OverviewCard>

      <div class="dash__actions">
        <el-button type="primary" size="small" icon="el-icon-plus" @click="go('/tasks')">
          新建待办
        </el-button>
        <el-button size="small" icon="el-icon-plus" @click="go('/expenses')">记一笔</el-button>
      </div>
    </template>
  </div>
</template>

<script>
import { getOverview } from '@/api/dashboard'
import { formatMoney } from '@/utils/format'
import OverviewCard from '@/components/OverviewCard'
import SparkBar from '@/components/SparkBar'

// 按北京时间判断问候语；只影响文案，不影响任何数据口径
function beijingHour() {
  const now = new Date()
  const utc = now.getTime() + now.getTimezoneOffset() * 60000
  return new Date(utc + 8 * 3600000).getHours()
}

// 今天（北京时间）的 YYYY-MM-DD
function beijingToday() {
  const now = new Date()
  const utc = now.getTime() + now.getTimezoneOffset() * 60000
  const bj = new Date(utc + 8 * 3600000)
  const mm = String(bj.getMonth() + 1).padStart(2, '0')
  const dd = String(bj.getDate()).padStart(2, '0')
  return `${bj.getFullYear()}-${mm}-${dd}`
}

// 两个 YYYY-MM-DD 相差几天（用 UTC 零点算，避免夏令时/时区干扰）
function dayGap(from, to) {
  const a = Date.parse(`${from}T00:00:00Z`)
  const b = Date.parse(`${to}T00:00:00Z`)
  if (Number.isNaN(a) || Number.isNaN(b)) return 0
  return Math.round((b - a) / 86400000)
}

export default {
  name: 'DashboardView',
  components: { OverviewCard, SparkBar },
  data() {
    return {
      loading: false,
      overview: null
    }
  },
  computed: {
    tasks() {
      return (this.overview && this.overview.tasks) || {}
    },
    expense() {
      return (this.overview && this.overview.expense) || {}
    },
    recent7d() {
      return (this.overview && this.overview.recent_7d) || []
    },
    focusTasks() {
      return (this.overview && this.overview.focus_tasks) || []
    },
    greeting() {
      const h = beijingHour()
      if (h < 6) return '凌晨好'
      if (h < 11) return '早上好'
      if (h < 14) return '中午好'
      if (h < 18) return '下午好'
      return '晚上好'
    }
  },
  created() {
    this.fetchOverview()
  },
  methods: {
    money(value) {
      return formatMoney(value)
    },
    go(path) {
      this.$router.push(path)
    },
    // 后端返回的 due_at 固定带 +08:00，所以前 10 位就是北京日期、11-16 位就是北京时间
    beijingDate(iso) {
      return typeof iso === 'string' && iso.length >= 10 ? iso.slice(0, 10) : ''
    },
    clock(iso) {
      return typeof iso === 'string' && iso.length >= 16 ? iso.slice(11, 16) : ''
    },
    // 时间列：今天/明天用相对词，其余用 MM-DD
    whenLabel(iso) {
      if (!iso) return '—'
      const gap = dayGap(beijingToday(), this.beijingDate(iso))
      const time = this.clock(iso)
      if (gap === 0) return `今天 ${time}`
      if (gap === 1) return `明天 ${time}`
      return `${this.beijingDate(iso).slice(5)} ${time}`
    },
    // 状态列：逾期几天 / 今天到期 / 明日到期 / 还有几天
    dueLabel(row) {
      if (!row.due_at) return '无期限'
      const gap = dayGap(beijingToday(), this.beijingDate(row.due_at))
      if (row.is_overdue) {
        return gap < 0 ? `逾期 ${-gap} 天` : '今天已过期'
      }
      if (gap === 0) return '今天到期'
      if (gap === 1) return '明日到期'
      return `还有 ${gap} 天`
    },
    async fetchOverview() {
      this.loading = true
      try {
        this.overview = await getOverview()
      } catch (e) {
        // 提示由 request.js 统一弹，这里只保证页面有可读状态
        this.overview = null
      } finally {
        this.loading = false
      }
    }
  }
}
</script>

<style scoped lang="scss">
.dash {
  padding: 20px 24px;
}

.dash__hint {
  color: #909399;
}

.dash__greeting {
  margin: 0 0 16px;
  font-size: 18px;
  font-weight: 500;
  color: #303133;
}

.dash__cards {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
}

/* 待办卡里的五格数字 */
.stats {
  display: grid;
  margin: 0;
  padding: 0;
  list-style: none;
  gap: 12px 8px;
  grid-template-columns: repeat(2, 1fr);
}

.stats__item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding-right: 12px;
}

.stats__item--wide {
  grid-column: 1 / -1;
  padding-right: 0;
}

.stats__label {
  font-size: 13px;
  color: #909399;
}

.stats__value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
  line-height: 1.2;
}

.stats__value--danger {
  color: #f56c6c;
}

/* 消费卡 */
.amount {
  margin: 0;
  color: #303133;
  line-height: 1.1;
}

.amount__unit {
  font-size: 16px;
  font-weight: 500;
  margin-right: 2px;
}

.amount__value {
  font-size: 30px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.amount__sub {
  margin: 10px 0 0;
  font-size: 13px;
  color: #909399;

  b {
    color: #606266;
    font-weight: 500;
  }
}

.amount__gap {
  margin-left: 16px;
}

/* 逾期与今日到期 */
.dash__focus {
  margin-top: 16px;
}

.focus {
  margin: 0;
  padding: 0;
  list-style: none;
}

.focus__row {
  display: grid;
  align-items: center;
  padding: 9px 6px;
  border-bottom: 1px solid #f7f8fa;
  font-size: 14px;
  grid-template-columns: 16px 1fr 140px 96px;

  &:last-child {
    border-bottom: none;
  }

  &:hover {
    background: #fafbfc;
  }
}

.focus__flag {
  font-weight: 700;
  color: #f56c6c;
}

.focus__title {
  overflow: hidden;
  color: #303133;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.focus__time {
  color: #909399;
  font-size: 13px;
}

.focus__due {
  text-align: right;
  color: #909399;
  font-size: 13px;
}

.focus__due--overdue {
  color: #f56c6c;
}

.focus__empty {
  margin: 2px 0;
  color: #909399;
  font-size: 13px;
}

.dash__actions {
  margin-top: 16px;
}
</style>
