<template>
  <div class="usage">
    <el-alert
      v-if="forbidden"
      type="error"
      :closable="false"
      show-icon
      title="需要管理员权限"
      class="usage__alert"
    >
      <p class="usage__alert-text">当前账号不是管理员，所以读不到调用明细。</p>
    </el-alert>

    <!-- 合计卡：数字来自响应的 summary，是整个筛选范围的合计，
         翻页不会变，所以它得画在表格外面 -->
    <el-row :gutter="12" class="usage__cards">
      <el-col :xs="12" :sm="12" :md="6">
        <div class="usage__card">
          <p class="usage__card-label">调用次数</p>
          <p class="usage__card-value">{{ formatInt(summary.calls) }}</p>
          <p class="usage__card-hint">
            失败 <span :class="summary.failed ? 'usage__bad' : ''">{{ formatInt(summary.failed) }}</span> 次
          </p>
        </div>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <div class="usage__card">
          <p class="usage__card-label">输入 token</p>
          <p class="usage__card-value">{{ formatInt(summary.token_in) }}</p>
          <p class="usage__card-hint">≈ {{ formatTokens(summary.token_in) }}</p>
        </div>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <div class="usage__card">
          <p class="usage__card-label">输出 token</p>
          <p class="usage__card-value">{{ formatInt(summary.token_out) }}</p>
          <p class="usage__card-hint">≈ {{ formatTokens(summary.token_out) }}</p>
        </div>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <div class="usage__card">
          <p class="usage__card-label">估算费用</p>
          <p class="usage__card-value">{{ formatCost(summary.cost_estimate) }}</p>
          <p class="usage__card-hint">{{ summary.cost_estimate ? '按配置单价估算' : '未配置单价，只记 token' }}</p>
        </div>
      </el-col>
    </el-row>

    <div class="usage__panel">
      <!-- 时间范围用快捷标签而不是两个日期选择器：看日志 90% 是「今天/昨天/近七天」，
           剩下 10% 再手动填。点标签会重置页码 -->
      <el-radio-group v-model="range" size="small" class="usage__ranges" @change="onRangeChange">
        <el-radio-button v-for="item in rangeOptions" :key="item.value" :label="item.value">
          {{ item.label }}
        </el-radio-button>
      </el-radio-group>

      <el-date-picker
        v-model="customRange"
        type="datetimerange"
        size="small"
        value-format="yyyy-MM-dd HH:mm:ss"
        range-separator="至"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        class="usage__daterange"
        @change="onCustomRangeChange"
      />

      <!-- 筛选条件「点了才查」，不随输入实时请求 -->
      <el-form :inline="true" size="small" class="usage__toolbar" @submit.native.prevent="onSearch">
        <el-form-item>
          <el-select
            v-model="query.status"
            placeholder="全部状态"
            clearable
            class="usage__select"
            @change="onSearch"
          >
            <el-option
              v-for="item in statusOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item>
          <el-input
            v-model="filters.keyword"
            placeholder="用户名 / 用户 ID"
            clearable
            class="usage__keyword"
            @clear="onSearch"
          >
            <el-button slot="append" icon="el-icon-search" @click="onSearch" />
          </el-input>
        </el-form-item>

        <el-form-item>
          <el-button icon="el-icon-refresh" :loading="loading" @click="onRefresh">刷新</el-button>
        </el-form-item>
      </el-form>
    </div>

    <el-table v-loading="loading" :data="rows" border class="usage__table">
      <el-table-column label="时间" width="150">
        <template slot-scope="{ row }">{{ showTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="用户" min-width="130">
        <template slot-scope="{ row }">
          <span>{{ row.username || '（已注销）' }}</span>
          <span class="usage__uid">#{{ row.user_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="用途" width="100">
        <template slot-scope="{ row }">{{ purposeLabel(row.purpose) }}</template>
      </el-table-column>
      <el-table-column prop="model" label="模型" min-width="140" show-overflow-tooltip />
      <el-table-column label="输入" width="90" align="right">
        <template slot-scope="{ row }">{{ formatInt(row.token_in) }}</template>
      </el-table-column>
      <el-table-column label="输出" width="90" align="right">
        <template slot-scope="{ row }">{{ formatInt(row.token_out) }}</template>
      </el-table-column>
      <el-table-column label="费用" width="100" align="right">
        <template slot-scope="{ row }">{{ formatCost(row.cost_estimate) }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="90" align="right">
        <template slot-scope="{ row }">{{ formatLatency(row.latency_ms) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template slot-scope="{ row }">
          <el-tag :type="row.status === 'ok' ? 'success' : 'danger'" size="mini">
            {{ statusLabel(row.status) }}
          </el-tag>
          <el-tooltip v-if="row.error_code" :content="row.error_code" placement="top">
            <i class="el-icon-warning-outline usage__err-icon" />
          </el-tooltip>
        </template>
      </el-table-column>

      <template slot="empty">
        <div class="empty-state">
          <p class="empty-state__title">{{ emptyText }}</p>
          <p v-if="!forbidden" class="empty-state__hint">调用 AI 助手后这里会出现每一次模型调用</p>
        </div>
      </template>
    </el-table>

    <div class="usage__pager">
      <el-pagination
        background
        layout="total, sizes, prev, pager, next, jumper"
        :total="total"
        :current-page="query.page"
        :page-size="query.size"
        :page-sizes="pageSizes"
        :disabled="forbidden"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </div>
  </div>
</template>

<script>
import { listUsage } from '@/api/admin'
import { formatCompact, formatCost, formatDateTime, formatLatency, formatNumber } from '@/utils/format'

// AI 调用明细页：合计卡 + 时间/状态/用户筛选 + 分页表格。
//
// 合计与明细在同一个响应里（后端 UsagePageOut.summary），所以无论翻到哪一页，
// 顶部四张卡都是**整个筛选范围**的合计，不会变成「本页合计」。
//
// 权限：看不到就是 403（router 挂的 require_role("admin")），这里只把 403 翻成一条红条，
// 不当成「加载失败」弹 message。

const PAGE_SIZES = [10, 20, 50, 100]

const STATUS_OPTIONS = [
  { value: 'ok', label: '成功' },
  { value: 'failed', label: '失败' }
]
const STATUS_LABELS = { ok: '成功', failed: '失败' }

const PURPOSE_LABELS = { chat: '对话', embedding: '向量化', report: '报告' }

// 快捷区间。`end` 故意往后挪一格：后端是左闭右开，不挪的话「今天」会漏掉现在这一秒。
const RANGE_OPTIONS = [
  { value: 'today', label: '今天' },
  { value: 'yesterday', label: '昨天' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: 'all', label: '全部' }
]

function pad(n) {
  return n < 10 ? `0${n}` : String(n)
}

// 后端 datetime 没有时区（naive），按浏览器本地时间理解即可
function fmtDate(d, withTime) {
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  if (!withTime) return day
  return `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function startOfToday() {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

// 算出一个时间区间，返回 [start, end] 两个字符串；'all' 返回 []
function resolveRange(key) {
  const today = startOfToday()
  if (key === 'today') {
    const end = new Date(today.getTime() + 24 * 3600 * 1000)
    return [fmtDate(today, true), fmtDate(end, true)]
  }
  if (key === 'yesterday') {
    const start = new Date(today.getTime() - 24 * 3600 * 1000)
    return [fmtDate(start, true), fmtDate(today, true)]
  }
  if (key === '7d') {
    const start = new Date(today.getTime() - 6 * 24 * 3600 * 1000)
    const end = new Date(today.getTime() + 24 * 3600 * 1000)
    return [fmtDate(start, true), fmtDate(end, true)]
  }
  if (key === '30d') {
    const start = new Date(today.getTime() - 29 * 24 * 3600 * 1000)
    const end = new Date(today.getTime() + 24 * 3600 * 1000)
    return [fmtDate(start, true), fmtDate(end, true)]
  }
  return []
}

const EMPTY_SUMMARY = { token_in: 0, token_out: 0, cost_estimate: 0, calls: 0, failed: 0 }

export default {
  name: 'AdminUsageView',

  data() {
    return {
      loading: false,
      rows: [],
      total: 0,
      summary: { ...EMPTY_SUMMARY },
      forbidden: false,

      range: '7d',
      customRange: null,

      // 输入中的筛选条件：改这里不发请求，点搜索才生效
      filters: { keyword: '' },

      // 真正发出去的参数
      query: {
        page: 1,
        size: 20,
        status: '',
        keyword: '',
        start: '',
        end: ''
      },

      rangeOptions: RANGE_OPTIONS,
      statusOptions: STATUS_OPTIONS,
      pageSizes: PAGE_SIZES
    }
  },

  computed: {
    emptyText() {
      if (this.forbidden) return '当前账号不是管理员，看不到调用明细'
      return this.hasFilter ? '这个区间内没有调用记录' : '还没有任何 AI 调用'
    },

    hasFilter() {
      return Boolean(this.query.status || this.query.keyword || this.query.start)
    }
  },

  created() {
    this.applyRange('7d')
    this.fetchData()
  },

  methods: {
    // 快捷标签 → query.start/end；同时把自定义区间清掉，免得两套条件打架
    applyRange(key) {
      const [start, end] = resolveRange(key)
      this.query.start = start || ''
      this.query.end = end || ''
    },

    onRangeChange(key) {
      this.customRange = null
      this.applyRange(key)
      this.onSearch()
    },

    onCustomRangeChange(value) {
      if (!Array.isArray(value) || value.length !== 2) return
      // 选了自定义区间就退出快捷标签的选中态（高亮都灭掉），
      // 否则「近 7 天」还亮着，用户会以为两个条件叠加
      this.range = ''
      this.query.start = value[0]
      this.query.end = value[1]
      this.onSearch()
    },

    async fetchData() {
      this.loading = true
      this.forbidden = false
      try {
        // silent：失败自己处理，避免 request.js 和页面各弹一条
        const data = await listUsage(this.query, { silent: true })
        this.rows = data.items || []
        this.total = data.total || 0
        this.summary = data.summary || { ...EMPTY_SUMMARY }
      } catch (error) {
        this.rows = []
        this.total = 0
        this.summary = { ...EMPTY_SUMMARY }
        if (error && error.status === 403) {
          this.forbidden = true
        } else if (error && error.status !== 401) {
          // 401 已由 request.js 跳登录页
          this.$message.error(error.message || '加载失败')
        }
      } finally {
        this.loading = false
      }
    },

    onSearch() {
      // 搜索时把输入框的值搬进 query
      this.query.keyword = this.filters.keyword
      this.query.page = 1
      this.fetchData()
    },

    onRefresh() {
      this.fetchData()
    },

    onPageChange(page) {
      this.query.page = page
      this.fetchData()
    },

    onSizeChange(size) {
      this.query.size = size
      this.query.page = 1
      this.fetchData()
    },

    // ---------- 展示 ----------
    // 格式化全走 @/utils/format，页面里不出现 dayjs / toFixed
    formatInt: formatNumber,
    formatTokens: formatCompact,
    formatCost,
    formatLatency,

    // 调用明细要秒级分辨：同一分钟里可能有几十次调用
    showTime(value) {
      return formatDateTime(value, { seconds: true }) || '—'
    },

    purposeLabel(purpose) {
      return PURPOSE_LABELS[purpose] || purpose || ''
    },

    statusLabel(status) {
      return STATUS_LABELS[status] || status || ''
    }
  }
}
</script>

<style lang="scss" scoped>
.usage {
  &__alert {
    margin-bottom: 12px;
  }

  &__alert-text {
    margin: 4px 0 0;
    font-size: 12px;
    line-height: 1.7;
  }

  &__cards {
    margin-bottom: 12px;
  }

  &__card {
    background: #fff;
    border-radius: 4px;
    padding: 14px 16px;
    margin-bottom: 12px;

    &-label {
      margin: 0;
      font-size: 12px;
      color: #909399;
    }

    &-value {
      margin: 6px 0 0;
      font-size: 22px;
      font-weight: 600;
      color: #303133;
      line-height: 1.2;
    }

    &-hint {
      margin: 4px 0 0;
      font-size: 12px;
      color: #c0c4cc;
    }
  }

  &__bad {
    color: #f56c6c;
  }

  &__panel {
    background: #fff;
    border-radius: 4px;
    padding: 12px 16px 2px;
    margin-bottom: 12px;
  }

  &__ranges {
    margin-right: 8px;
  }

  &__daterange {
    width: 360px;
    vertical-align: middle;
  }

  &__toolbar {
    display: inline-block;
    margin-left: 8px;
    vertical-align: middle;

    .el-form-item {
      margin-bottom: 10px;
    }
  }

  &__select {
    width: 130px;
  }

  &__keyword {
    width: 220px;
  }

  &__table {
    background: #fff;
  }

  &__uid {
    margin-left: 6px;
    font-size: 12px;
    color: #c0c4cc;
  }

  &__err-icon {
    margin-left: 4px;
    color: #e6a23c;
    cursor: help;
  }

  &__pager {
    display: flex;
    justify-content: flex-end;
    margin-top: 12px;
  }
}
</style>