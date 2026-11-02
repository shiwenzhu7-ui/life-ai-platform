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

export default {
  name: 'TasksView',
  components: { PaginationBar },
  data() {
    return {
      loading: false,
      rows: [],
      total: 0,
      page: 1,
      size: 20
    }
  },
  created() {
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

    // 只读版：只有 page/size 两个参数，筛选在 4b 里加
    async fetchData() {
      this.loading = true
      try {
        const data = await listTasks({ page: this.page, size: this.size })
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
