<template>
  <div class="expenses">
    <div class="expenses__toolbar">
      <span class="expenses__hint">共 {{ total }} 条记录</span>
      <el-button type="primary" size="small" icon="el-icon-plus" @click="openCreate">
        记一笔
      </el-button>
    </div>

    <ExpenseSummary
      :summary="summary"
      :month="month"
      :loading="summaryLoading"
      @shift="handleMonthShift"
      @current="handleMonthCurrent"
    />

    <!-- 筛选条：四个维度照 docs/页面设计.md §1.5 的接口映射表（type / category_id / keyword / 日期） -->
    <div class="expenses__filters">
      <el-input
        v-model="filters.keyword"
        class="expenses__filter"
        placeholder="搜索备注"
        prefix-icon="el-icon-search"
        clearable
        @keyup.enter.native="handleFilterChange"
        @clear="handleFilterChange"
      />
      <el-button size="small" @click="handleFilterChange">搜索</el-button>

      <el-select
        v-model="filters.type"
        class="expenses__filter"
        clearable
        placeholder="类型"
        @change="handleTypeChange"
      >
        <el-option
          v-for="item in typeOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>

      <el-select
        v-model="filters.category_id"
        class="expenses__filter"
        clearable
        placeholder="分类"
        @change="handleFilterChange"
      >
        <!-- 选项跟着类型走：支出看 expense 域，收入看 income 域（两个域的分类不通用） -->
        <el-option
          v-for="item in filterCategories"
          :key="item.id"
          :label="item.name"
          :value="item.id"
        />
      </el-select>

      <el-date-picker
        v-model="filters.date_range"
        class="expenses__filter expenses__filter--date"
        type="daterange"
        value-format="yyyy-MM-dd"
        range-separator="至"
        start-placeholder="发生起"
        end-placeholder="发生止"
        unlink-panels
        @change="handleFilterChange"
      />

      <el-button type="text" size="small" @click="resetFilters">重置</el-button>
    </div>

    <el-table
      v-loading="loading"
      :data="rows"
      row-key="id"
      border
      stripe
      empty-text="还没有记账"
      style="width: 100%"
    >
      <el-table-column prop="occurred_at" label="时间" width="150">
        <template slot-scope="{ row }">{{ formatDateTime(row.occurred_at) }}</template>
      </el-table-column>

      <el-table-column label="分类" width="110">
        <template slot-scope="{ row }">
          {{ row.category_name || '—' }}
        </template>
      </el-table-column>

      <el-table-column prop="account" label="账户" width="100" />

      <el-table-column label="金额" width="140" align="right">
        <!-- 金额是后端给的字符串，只加千分位和正负号，绝不做加减乘除 -->
        <template slot-scope="{ row }">
          <span :class="['expenses__amount', `expenses__amount--${row.type}`]">
            {{ amountText(row) }}
          </span>
        </template>
      </el-table-column>

      <el-table-column label="备注" min-width="180" show-overflow-tooltip>
        <template slot-scope="{ row }">{{ row.note || '—' }}</template>
      </el-table-column>

      <el-table-column label="操作" width="120" align="center">
        <template slot-scope="{ row }">
          <el-button type="text" size="mini" @click="openEdit(row)">编辑</el-button>
          <el-button type="text" size="mini" class="expenses__danger" @click="remove(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar :page="page" :size="size" :total="total" @change="handlePageChange" />

    <ExpenseFormDialog
      :visible.sync="dialogVisible"
      :expense-id="editingId"
      @saved="onSaved"
      @failed="fetchData"
    />
  </div>
</template>

<script>
import { deleteExpense, getExpenseSummary, listExpenses } from '@/api/expenses'
import { formatDateTime, formatMoney } from '@/utils/format'
import PaginationBar from '@/components/PaginationBar.vue'
import ExpenseFormDialog from '@/components/ExpenseFormDialog.vue'
import ExpenseSummary from '@/components/ExpenseSummary.vue'

// 类型映射放在组件外：常量没必要做成响应式
const TYPE_MAP = {
  expense: { label: '支出', sign: '-' },
  income: { label: '收入', sign: '+' }
}

// 筛选条初值，重置时每次都要一份新对象
const emptyFilters = () => ({
  keyword: '',
  type: '',
  category_id: '',
  date_range: []
})

export default {
  name: 'ExpensesView',
  components: { PaginationBar, ExpenseFormDialog, ExpenseSummary },
  data() {
    return {
      loading: false,
      rows: [],
      total: 0,
      page: 1,
      size: 20,
      filters: emptyFilters(),
      // 汇总区：month 为空表示不传，由后端默认当月
      month: '',
      summary: null,
      summaryLoading: false,
      // 请求序号：只认最新一次 summary 请求的结果
      summarySeq: 0,
      // 记一笔 / 编辑 弹窗（null = 新建）
      dialogVisible: false,
      editingId: null,
      typeOptions: Object.keys(TYPE_MAP).map((value) => ({
        value,
        label: TYPE_MAP[value].label
      }))
    }
  },
  computed: {
    // 分类下拉的选项来自 Vuex 缓存；没选类型时按「支出」显示（默认证号场景）
    filterCategories() {
      const scope = this.filters.type || 'expense'
      return this.$store.getters['categories/listOf'](scope)
    }
  },
  created() {
    // 两个域都预热：切类型时下拉能立刻出选项，不用等请求
    this.loadCategories('expense')
    this.loadCategories('income')
    this.fetchData()
    this.fetchSummary()
  },
  methods: {
    formatDateTime,

    amountText(row) {
      const sign = TYPE_MAP[row.type] ? TYPE_MAP[row.type].sign : ''
      return sign + formatMoney(row.amount)
    },

    loadCategories(scope) {
      return this.$store.dispatch('categories/load', scope)
    },

    // 换类型时清掉已选分类：两个域的分类 id 不通用，留着必然查不到数据
    handleTypeChange() {
      this.filters.category_id = ''
      this.handleFilterChange()
    },

    buildParams() {
      const { keyword, type, category_id, date_range } = this.filters
      const range = date_range || []
      return {
        page: this.page,
        size: this.size,
        keyword: keyword.trim(),
        type,
        category_id,
        date_from: range[0],
        date_to: range[1]
      }
    },

    async fetchData() {
      this.loading = true
      try {
        const data = await listExpenses(this.buildParams())
        this.rows = data.items
        this.total = data.total
      } catch (e) {
        // 提示已由 request.js 统一弹过，这里只负责清屏，别让旧数据留着
        this.rows = []
        this.total = 0
      } finally {
        this.loading = false
      }
    },

    // 改筛选回第 1 页：否则可能停在一个已经空了的页码上
    handleFilterChange() {
      this.page = 1
      this.fetchData()
    },

    resetFilters() {
      this.filters = emptyFilters()
      this.handleFilterChange()
    },

    handlePageChange({ page, size }) {
      this.page = page
      this.size = size
      this.fetchData()
    },

    openCreate() {
      this.editingId = null
      this.dialogVisible = true
    },

    // 只传 id：整行数据可能已经旧了，让弹窗自己去拉最新的
    openEdit(row) {
      this.editingId = row.id
      this.dialogVisible = true
    },

    // 记完一笔：新记录可能落在当前页任意位置，还可能换了日期，所以回第 1 页；
    // 编辑则留在当前页，避免「只改了个备注却跳页」
    onSaved() {
      if (!this.editingId) this.page = 1
      this.fetchData()
      // 金额变了 → 汇总必须跟着刷新，否则顶部数字还是旧的
      this.fetchSummary()
    },

    // 汇总区：只跟月份走，与列表的筛选条件互不影响
    // seq 用来丢弃过期响应：连点两下箭头时，后发的请求可能先回来，
    // 不加这层会看到「月份是 2026-08、数字却是 2026-09 的」
    async fetchSummary() {
      const seq = ++this.summarySeq
      this.summaryLoading = true
      try {
        const data = await getExpenseSummary(this.month || undefined)
        if (seq !== this.summarySeq) return
        this.summary = data
      } catch (e) {
        // 提示已由 request.js 统一弹过；置空让卡片显示 0.00，别留着上个月的数字
        if (seq === this.summarySeq) this.summary = null
      } finally {
        if (seq === this.summarySeq) this.summaryLoading = false
      }
    },

    // 翻月：用 Date 的「溢出归一」处理跨年（2026-01 往前一个月会得到 2025-12）
    handleMonthShift(delta) {
      const base = this.month
        ? new Date(`${this.month}-01T00:00:00`)
        : new Date()
      const target = new Date(base.getFullYear(), base.getMonth() + delta, 1)
      const month = String(target.getMonth() + 1).padStart(2, '0')
      this.month = `${target.getFullYear()}-${month}`
      this.fetchSummary()
    },

    // 回到当月：清空 month，让后端用默认值（省得前端自己算今天几号）
    handleMonthCurrent() {
      this.month = ''
      this.fetchSummary()
    },

    // 删除：后端是软删（只写 deleted_at），界面上表现为「消失」
    async remove(row) {
      try {
        await this.$confirm(
          `确定删除这笔 ${this.amountText(row)}（${row.category_name || '未分类'}）？删除后列表与汇总都不再统计。`,
          '删除记录',
          { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
        )
      } catch (e) {
        return // 点了取消 / 关闭，什么都不做
      }
      try {
        await deleteExpense(row.id)
        this.$message.success('已删除')
        // 删掉的是当前页最后一条时回退一页，否则会停在一个空页上
        if (this.rows.length === 1 && this.page > 1) {
          this.page -= 1
        }
        this.fetchData()
        this.fetchSummary()
      } catch (e) {
        // 提示已由 request.js 统一弹过（例如并发删除导致的 404），这里不重复弹
      }
    }
  }
}
</script>

<style lang="scss" scoped>
.expenses {
  padding: 16px;
  background: #fff;
  border-radius: 4px;

  &__toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }

  &__hint {
    font-size: 13px;
    color: #606266;
  }

  &__filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
  }

  &__filter {
    width: 160px;

    &--date {
      width: 300px;
    }
  }

  &__amount {
    font-variant-numeric: tabular-nums;
    font-weight: 600;

    &--expense {
      color: #f56c6c;
    }

    &--income {
      color: #67c23a;
    }
  }

  &__danger {
    color: #f56c6c;
  }
}
</style>

