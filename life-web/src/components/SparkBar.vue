<template>
  <div class="spark">
    <div class="spark__bars">
      <div
        v-for="(item, index) in items"
        :key="item.date"
        class="spark__slot"
        :title="item.tip"
      >
        <i
          class="spark__bar"
          :class="{ 'spark__bar--today': index === items.length - 1 }"
          :style="{ height: item.height }"
        />
      </div>
    </div>

    <div class="spark__axis">
      <span class="spark__tick">{{ firstLabel }}</span>
      <span class="spark__tick spark__tick--last">近 7 天</span>
      <span class="spark__tick">{{ lastLabel }}</span>
    </div>
  </div>
</template>

<script>
import { formatMoney } from '@/utils/format'

/**
 * 近 7 天迷你柱状图（纯 CSS）。
 *
 * 只负责「把 7 个数画成 7 根柱子」，不取数、不判断业务口径：
 * 数组有几个点就画几根，最后一个点当作「今天」高亮。
 */
export default {
  name: 'SparkBar',
  props: {
    // [{ date: '2026-11-05', expense: '12.00' }]，金额是字符串
    days: {
      type: Array,
      default: () => []
    }
  },
  computed: {
    items() {
      const values = this.days.map((d) => Number(d.expense) || 0)
      const max = Math.max(...values, 0)

      return this.days.map((d, index) => {
        const value = values[index]
        return {
          date: d.date,
          // 最大那根 100%；全 0 时给 4% 的矮条，避免柱子整体消失
          height: max > 0 ? `${(value / max) * 100}%` : '4%',
          tip: `${this.monthDay(d.date)} ¥${formatMoney(d.expense)}`
        }
      })
    },
    firstLabel() {
      return this.days.length ? this.monthDay(this.days[0].date) : ''
    },
    lastLabel() {
      return this.days.length ? this.monthDay(this.days[this.days.length - 1].date) : ''
    }
  },
  methods: {
    monthDay(date) {
      return typeof date === 'string' && date.length >= 10 ? date.slice(5) : date
    }
  }
}
</script>

<style scoped lang="scss">
.spark {
  margin-top: 18px;
}

.spark__bars {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 48px;
  border-bottom: 1px solid #f0f2f5;
}

.spark__slot {
  display: flex;
  flex: 1;
  align-items: flex-end;
  height: 100%;
}

.spark__bar {
  display: block;
  width: 100%;
  min-height: 3px;
  border-radius: 2px 2px 0 0;
  background: #c6e2ff;
  transition: height 0.3s;

  &--today {
    background: #409eff;
  }
}

.spark__axis {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  font-size: 12px;
  color: #a8abb2;
}

.spark__tick--last {
  color: #909399;
}
</style>
