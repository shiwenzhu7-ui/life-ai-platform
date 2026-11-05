<template>
  <div class="page">
    <h2>首页</h2>

    <p v-if="loading" class="page__hint">加载中…</p>
    <p v-else-if="!overview" class="page__hint">数据没取到，稍后再试。</p>

    <!-- F7-1：先把数字摊出来，确认数据通了；F7-2 换成左右两张卡片 -->
    <div v-else class="raw">
      <p class="raw__line">
        未开始 {{ tasks.todo }} · 进行中 {{ tasks.doing }} · 已完成 {{ tasks.done }} ·
        逾期 {{ tasks.overdue }} · 今日到期 {{ tasks.today_due }}
      </p>
      <p class="raw__line">
        本月 ¥{{ money(expense.current_month_total) }} · 上月 ¥{{
          money(expense.last_month_total)
        }}
        · 今日 ¥{{ money(expense.today_total) }}
      </p>
      <p class="raw__line">逾期与今日到期：{{ focusTasks.length }} 条</p>
      <p class="raw__line">近 7 天：{{ recent7d.length }} 个点</p>
    </div>
  </div>
</template>

<script>
import { getOverview } from '@/api/dashboard'
import { formatMoney } from '@/utils/format'

export default {
  name: 'DashboardView',
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
    focusTasks() {
      return (this.overview && this.overview.focus_tasks) || []
    },
    recent7d() {
      return (this.overview && this.overview.recent_7d) || []
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
.page {
  padding: 20px 24px;
}

.page__hint {
  color: #909399;
}

.raw__line {
  margin: 0 0 8px;
  font-size: 15px;
  line-height: 1.6;
}
</style>
