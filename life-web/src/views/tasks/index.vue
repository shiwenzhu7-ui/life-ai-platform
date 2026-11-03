<template>
  <div class="tasks">
    <div class="tasks__toolbar">
      <span class="tasks__hint">共 {{ total }} 条待办</span>
      <el-tooltip content="新建、编辑、删除在 F5 实现" placement="top-end">
        <el-button type="primary" size="small" icon="el-icon-plus" disabled>
          新建待办
        </el-button>
      </el-tooltip>
    </div>

    <!-- 筛选条：五个维度照 docs/页面设计.md §1.4 的接口映射表，后端参数早已就绪 -->
    <div class="tasks__filters">
      <el-input
        v-model="filters.keyword"
        class="tasks__filter"
        placeholder="搜索标题或备注"
        prefix-icon="el-icon-search"
        clearable
        @keyup.enter.native="handleFilterChange"
        @clear="handleFilterChange"
      />
      <el-button size="small" @click="handleFilterChange">搜索</el-button>

      <el-select
        v-model="filters.status"
        class="tasks__filter"
        multiple
        collapse-tags
        clearable
        placeholder="状态"
        @change="handleFilterChange"
      >
        <el-option
          v-for="item in statusOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>

      <el-select
        v-model="filters.category_id"
        class="tasks__filter"
        clearable
        placeholder="分类"
        @change="handleFilterChange"
      >
        <!-- 0 是后端定义的「未分类」，是有效筛选值，不能被当成「没选」 -->
        <el-option label="未分类" :value="0" />
        <el-option
          v-for="item in taskCategories"
          :key="item.id"
          :label="item.name"
          :value="item.id"
        />
      </el-select>

      <el-select
        v-model="filters.priority"
        class="tasks__filter"
        clearable
        placeholder="优先级"
        @change="handleFilterChange"
      >
        <el-option
          v-for="item in priorityOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>

      <el-date-picker
        v-model="filters.due_range"
        class="tasks__filter tasks__filter--date"
        type="daterange"
        value-format="yyyy-MM-dd"
        range-separator="至"
        start-placeholder="截止起"
        end-placeholder="截止止"
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
      empty-text="还没有待办"
      style="width: 100%"
    >
      <el-table-column label="标题" min-width="240" show-overflow-tooltip>
        <template slot-scope="{ row }">
          <span :class="{ 'is-done': row.status === 'done' }">{{ row.title }}</span>
          <p v-if="row.description" class="tasks__desc">{{ row.description }}</p>
        </template>
      </el-table-column>

      <el-table-column label="分类" width="100">
        <!-- 后端列表已经 join 出 category_name；category_id = 0 就是没有分类 -->
        <template slot-scope="{ row }">{{ row.category_name || '未分类' }}</template>
      </el-table-column>

      <el-table-column label="优先级" width="90" align="center">
        <template slot-scope="{ row }">
          <el-tag size="mini" effect="plain" :type="priorityOf(row.priority).type">
            {{ priorityOf(row.priority).label }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="截止时间" width="150">
        <template slot-scope="{ row }">
          <!-- 逾期与否由后端的 is_overdue 决定，前端不自己比时间：浏览器时钟不可信 -->
          <span v-if="!row.due_at" class="tasks__empty">—</span>
          <span v-else :class="{ 'is-overdue': row.is_overdue }">
            {{ formatDateTime(row.due_at) }}
          </span>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="100" align="center">
        <template slot-scope="{ row }">
          <el-tag size="mini" :type="statusOf(row.status).type">
            {{ statusOf(row.status).label }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="130" align="center">
        <template slot-scope="{ row }">
          <el-button type="text" size="mini" @click="notYet('编辑', row)">编辑</el-button>
          <el-button type="text" size="mini" class="tasks__danger" @click="notYet('删除', row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar :page="page" :size="size" :total="total" @change="handlePageChange" />
  </div>
</template>

<script>
import { listTasks } from '@/api/tasks'
import { formatDateTime } from '@/utils/format'
import PaginationBar from '@/components/PaginationBar.vue'

// 状态/优先级的「英文值 → 中文 + 颜色」映射放在组件外：
// 它们是常量，放进 data 会被 Vue 做成响应式，白白多一层开销。
const STATUS_MAP = {
  todo: { label: '待办', type: '' },
  doing: { label: '进行中', type: 'warning' },
  done: { label: '已完成', type: 'success' },
  cancelled: { label: '已取消', type: 'info' }
}

const PRIORITY_MAP = {
  low: { label: '低', type: 'info' },
  medium: { label: '中', type: '' },
  high: { label: '高', type: 'danger' }
}

// 筛选条的初值：每次重置都要一份新对象，所以写成函数
const emptyFilters = () => ({
  keyword: '',
  status: [],
  category_id: '',
  priority: '',
  due_range: []
})

export default {
  name: 'TasksView',
  components: { PaginationBar },
  data() {
    return {
      loading: false,
      rows: [],
      total: 0,
      page: 1,
      size: 20,
      filters: emptyFilters(),
      statusOptions: Object.keys(STATUS_MAP).map((value) => ({
        value,
        label: STATUS_MAP[value].label
      })),
      priorityOptions: Object.keys(PRIORITY_MAP).map((value) => ({
        value,
        label: PRIORITY_MAP[value].label
      }))
    }
  },
  computed: {
    // 分类下拉的选项来自 Vuex 缓存（dispatch 一次，10 分钟内复用）
    taskCategories() {
      return this.$store.getters['categories/listOf']('task')
    }
  },
  created() {
    this.loadCategories()
    this.fetchData()
  },
  methods: {
    formatDateTime,

    statusOf(status) {
      return STATUS_MAP[status] || { label: status, type: '' }
    },

    priorityOf(priority) {
      return PRIORITY_MAP[priority] || { label: priority, type: '' }
    },

    loadCategories() {
      // 缓存命中 / 并发去重 / 真正发请求，三个分支都在 categories 模块里，页面不用管
      return this.$store.dispatch('categories/load', 'task')
    },

    // 把界面上的筛选值翻成后端的参数名；空值交给 compactParams 剔除
    buildParams() {
      const { keyword, status, category_id, priority, due_range } = this.filters
      const range = due_range || []
      return {
        page: this.page,
        size: this.size,
        keyword: keyword.trim(),
        status,
        category_id,
        priority,
        due_from: range[0],
        due_to: range[1]
      }
    },

    async fetchData() {
      this.loading = true
      try {
        const data = await listTasks(this.buildParams())
        this.rows = data.items
        this.total = data.total
      } catch (e) {
        // 错误提示已经由 request.js 统一弹过，这里只负责把页面清空，别让旧数据留在屏幕上
        this.rows = []
        this.total = 0
      } finally {
        this.loading = false
      }
    },

    // 改筛选必须回第 1 页：第 3 页改完条件后结果可能只剩 1 页，留在第 3 页就是空白
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

    notYet(action, row) {
      this.$message.info(`${action}「${row.title}」在 F5 实现`)
    }
  }
}
</script>

<style lang="scss" scoped>
.tasks {
  padding: 16px;
  background: #fff;
  border-radius: 4px;

  &__toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 12px;
  }

  &__hint {
    font-size: 13px;
    color: #909399;
  }

  &__filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    padding-bottom: 12px;
  }

  &__filter {
    width: 160px;

    &--date {
      width: 300px;
    }
  }

  &__desc {
    margin: 2px 0 0;
    font-size: 12px;
    color: #909399;
  }

  &__empty {
    color: #c0c4cc;
  }

  &__danger {
    color: #f56c6c;
  }

  .is-overdue {
    color: #f56c6c;
    font-weight: 600;
  }

  .is-done {
    color: #909399;
    text-decoration: line-through;
  }
}
</style>
