<template>
  <div class="tasks">
    <div class="tasks__toolbar">
      <span class="tasks__hint">共 {{ total }} 条待办</span>
      <el-button type="primary" size="small" icon="el-icon-plus" @click="openCreate">
        新建待办
      </el-button>
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

      <el-select
        v-model="sortValue"
        class="tasks__filter tasks__filter--sort"
        placeholder="排序"
        @change="handleFilterChange"
      >
        <el-option
          v-for="item in sortOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>

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
      <el-table-column width="46" align="center">
        <!-- 勾选 = 完成。只做 todo ↔ done 二元切换；doing/cancelled 在 V1.0 没有界面入口 -->
        <template slot-scope="{ row }">
          <el-checkbox
            :value="row.status === 'done'"
            :disabled="togglingId === row.id"
            @change="toggleDone(row)"
          />
        </template>
      </el-table-column>

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
          <el-button type="text" size="mini" @click="openEdit(row)">编辑</el-button>
          <el-button type="text" size="mini" class="tasks__danger" @click="remove(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar :page="page" :size="size" :total="total" @change="handlePageChange" />

    <TaskFormDrawer
      :visible.sync="drawerVisible"
      :task-id="editingId"
      @saved="onSaved"
      @failed="fetchData"
    />
  </div>
</template>

<script>
import { listTasks, changeTaskStatus, deleteTask } from '@/api/tasks'
import { formatDateTime } from '@/utils/format'
import PaginationBar from '@/components/PaginationBar.vue'
import TaskFormDrawer from '@/components/TaskFormDrawer.vue'

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

// 排序下拉的六个选项：把「排序键 + 方向」拼成一个值，用户只需选一次
const SORT_OPTIONS = [
  { value: 'due_at:asc', label: '到期（近 → 远）' },
  { value: 'due_at:desc', label: '到期（远 → 近）' },
  { value: 'priority:desc', label: '优先级（高 → 低）' },
  { value: 'priority:asc', label: '优先级（低 → 高）' },
  { value: 'created_at:desc', label: '创建（新 → 旧）' },
  { value: 'created_at:asc', label: '创建（旧 → 新）' }
]

// 筛选条的初值：每次重置都要一份新对象，所以写成函数
const emptyFilters = () => ({
  keyword: '',
  status: [],
  category_id: '',
  priority: '',
  due_range: [],
  // 排序不进「筛选」语义，但它和筛选一样决定列表长什么样，所以放同一份状态里一起重置
  sort: 'due_at',
  order: 'asc'
})

export default {
  name: 'TasksView',
  components: { PaginationBar, TaskFormDrawer },
  data() {
    return {
      loading: false,
      rows: [],
      total: 0,
      page: 1,
      size: 20,
      filters: emptyFilters(),
      // 抽屉：visible 控制显示，editingId 为空 = 新建
      drawerVisible: false,
      editingId: null,
      // 正在切状态的那一行的 id，用来禁掉重复点击
      togglingId: null,
      statusOptions: Object.keys(STATUS_MAP).map((value) => ({
        value,
        label: STATUS_MAP[value].label
      })),
      priorityOptions: Object.keys(PRIORITY_MAP).map((value) => ({
        value,
        label: PRIORITY_MAP[value].label
      })),
      sortOptions: SORT_OPTIONS
    }
  },
  computed: {
    // 分类下拉的选项来自 Vuex 缓存（dispatch 一次，10 分钟内复用）
    taskCategories() {
      return this.$store.getters['categories/listOf']('task')
    },

    // 下拉绑一个字符串，state 里仍分成 sort/order 两项（后端就是两个参数）
    sortValue: {
      get() {
        return `${this.filters.sort}:${this.filters.order}`
      },
      set(value) {
        const [sort, order] = value.split(':')
        this.filters.sort = sort
        this.filters.order = order
      }
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
      const { keyword, status, category_id, priority, due_range, sort, order } = this.filters
      const range = due_range || []
      return {
        page: this.page,
        size: this.size,
        keyword: keyword.trim(),
        status,
        category_id,
        priority,
        due_from: range[0],
        due_to: range[1],
        sort,
        order
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

    // 勾选 = 完成 / 取消完成
    async toggleDone(row) {
      const next = row.status === 'done' ? 'todo' : 'done'
      this.togglingId = row.id
      try {
        await changeTaskStatus(row.id, next)
        // 必须重拉：completed_at、is_overdue、排序都可能变，本地改行会和服务端不一致
        await this.fetchData()
      } catch (e) {
        // 提示已由拦截器弹过；重拉一次让界面回到服务端的真实状态
        await this.fetchData()
      } finally {
        this.togglingId = null
      }
    },

    async remove(row) {
      try {
        await this.$confirm(`删除后不可恢复，确定删除「${row.title}」？`, '删除待办', {
          type: 'warning',
          confirmButtonText: '删除',
          cancelButtonText: '取消'
        })
      } catch (e) {
        return // 用户点了取消
      }
      try {
        await deleteTask(row.id)
        // 删掉的是当前页最后一条时，页码要退一位，否则会停在空页
        if (this.rows.length === 1 && this.page > 1) this.page -= 1
        this.$message.success('已删除')
        await this.fetchData()
      } catch (e) {
        await this.fetchData()
      }
    },

    openCreate() {
      this.editingId = null
      this.drawerVisible = true
    },

    openEdit(row) {
      this.editingId = row.id
      this.drawerVisible = true
    },

    onSaved() {
      this.fetchData()
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

    // 排序的选项文字比「优先级」「分类」长，给宽一点免得截断
    &--sort {
      width: 170px;
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
