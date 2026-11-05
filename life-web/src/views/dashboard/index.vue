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
</style>
