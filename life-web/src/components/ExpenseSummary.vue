<template>
  <div v-loading="loading" class="expense-summary">
    <!-- 月份条：箭头翻月、右侧「本月」快捷回到当月 -->
    <div class="expense-summary__head">
      <el-button
        class="expense-summary__arrow"
        icon="el-icon-arrow-left"
        circle
        size="mini"
        @click="$emit('shift', -1)"
      />
      <span class="expense-summary__period">{{ periodText }}</span>
      <el-button
        class="expense-summary__arrow"
        icon="el-icon-arrow-right"
        circle
        size="mini"
        @click="$emit('shift', 1)"
      />
      <el-button type="text" size="mini" @click="$emit('current')">本月</el-button>
    </div>

    <!-- 三个数字：金额是后端给的两位小数字符串，这里只加千分位，不做任何运算 -->
    <div class="expense-summary__cards">
      <div class="expense-summary__card">
        <span class="expense-summary__label">支出</span>
        <span class="expense-summary__value expense-summary__value--expense">
          ¥{{ money(totals.expense_total) }}
        </span>
      </div>
      <div class="expense-summary__card">
        <span class="expense-summary__label">收入</span>
        <span class="expense-summary__value expense-summary__value--income">
          ¥{{ money(totals.income_total) }}
        </span>
      </div>
      <div class="expense-summary__card">
        <span class="expense-summary__label">结余</span>
        <span
          :class="[
            'expense-summary__value',
            Number(totals.balance) < 0
              ? 'expense-summary__value--expense'
              : 'expense-summary__value--balance'
          ]"
        >
          ¥{{ money(totals.balance) }}
        </span>
      </div>
    </div>

    <!-- 类别占比：V1.0 用横向条，V1.4 换 ECharts 饼图（见 docs/页面设计.md §1.5） -->
    <div class="expense-summary__cats">
      <p class="expense-summary__cats-title">类别占比</p>
      <p v-if="!categories.length" class="expense-summary__empty">本月还没有支出记录</p>
      <div v-for="item in categories" :key="item.category_id" class="expense-summary__cat">
        <span class="expense-summary__cat-name">{{ item.category_name }}</span>
        <span class="expense-summary__cat-bar">
          <i :style="{ width: barWidth(item.ratio) }" />
        </span>
        <span class="expense-summary__cat-total">{{ money(item.total) }}</span>
        <span class="expense-summary__cat-ratio">{{ percent(item.ratio) }}</span>
      </div>
    </div>
  </div>
</template>

<script>
import { formatMoney } from '@/utils/format'

// 汇总区只负责显示：数据由页面取（页面要在记一笔/删除之后统一刷新）
export default {
  name: 'ExpenseSummary',
  props: {
    // 接口原样返回的对象：{ period, expense_total, income_total, balance, by_category }
    summary: { type: Object, default: null },
    // 页面请求的月份（'' 表示没传，后端默认当月）；summary 还没回来时用它在标题上兜底
    month: { type: String, default: '' },
    loading: { type: Boolean, default: false }
  },
  computed: {
    totals() {
      const s = this.summary || {}
      return {
        expense_total: s.expense_total || '0.00',
        income_total: s.income_total || '0.00',
        balance: s.balance || '0.00'
      }
    },
    categories() {
      return (this.summary && this.summary.by_category) || []
    },
    // "2026-11" → "2026 年 11 月"
    periodText() {
      const period = (this.summary && this.summary.period) || this.month
      if (!period) return '本月'
      const [year, month] = period.split('-')
      return `${year} 年 ${Number(month)} 月`
    }
  },
  methods: {
    money(value) {
      return formatMoney(value)
    },
    // ratio 是 0~1 的三位小数；横条宽度按百分比
    barWidth(ratio) {
      const value = Number(ratio) || 0
      return `${Math.min(Math.max(value * 100, 0), 100).toFixed(1)}%`
    },
    percent(ratio) {
      return `${(Number(ratio) * 100 || 0).toFixed(1)}%`
    }
  }
}
</script>

<style lang="scss" scoped>
.expense-summary {
  margin-bottom: 16px;

  &__head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 14px;
  }

  &__period {
    min-width: 110px;
    text-align: center;
    font-size: 14px;
    font-weight: 600;
    color: #303133;
  }

  &__cards {
    display: flex;
    gap: 16px;
    margin-bottom: 18px;
  }

  &__card {
    flex: 1;
    padding: 12px 16px;
    border: 1px solid #ebeef5;
    border-radius: 6px;
    background: #fafbfc;
  }

  &__label {
    display: block;
    font-size: 12px;
    color: #909399;
    margin-bottom: 6px;
  }

  &__value {
    font-size: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;

    &--expense {
      color: #f56c6c;
    }

    &--income {
      color: #67c23a;
    }

    &--balance {
      color: #303133;
    }
  }

  &__cats-title {
    margin: 0 0 10px;
    font-size: 13px;
    color: #606266;
  }

  &__empty {
    margin: 0;
    font-size: 13px;
    color: #c0c4cc;
  }

  &__cat {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
    font-size: 13px;
    color: #606266;
  }

  &__cat-name {
    width: 56px;
    flex: 0 0 56px;
  }

  &__cat-bar {
    flex: 1;
    height: 10px;
    border-radius: 5px;
    background: #f0f2f5;
    overflow: hidden;

    i {
      display: block;
      height: 100%;
      border-radius: 5px;
      background: #409eff;
      transition: width 0.3s;
    }
  }

  &__cat-total {
    width: 96px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  &__cat-ratio {
    width: 56px;
    text-align: right;
    color: #909399;
  }
}
</style>
