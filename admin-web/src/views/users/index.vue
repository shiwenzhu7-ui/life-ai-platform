<template>
  <div class="users">
    <el-alert
      v-if="forbidden"
      type="error"
      :closable="false"
      show-icon
      title="需要管理员权限"
      class="users__alert"
    >
      <p class="users__alert-text">
        当前账号不是管理员，所以读不到用户列表。请用管理员账号登录，
        或在数据库里把 <code>users.role</code> 改成 <code>admin</code>。
      </p>
    </el-alert>

    <!-- 工具栏：筛选条件全部是「点了才查」，不随输入实时请求，
         免得每敲一个字就打一次 /admin/users -->
    <el-form :inline="true" size="small" class="users__toolbar" @submit.native.prevent="onSearch">
      <el-form-item>
        <el-select
          v-model="query.role"
          placeholder="全部角色"
          clearable
          class="users__select"
          @change="onSearch"
        >
          <el-option
            v-for="item in roleOptions"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>

      <el-form-item>
        <el-select
          v-model="query.status"
          placeholder="全部状态"
          clearable
          class="users__select"
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
          v-model="query.keyword"
          placeholder="用户名 / 昵称 / 邮箱"
          clearable
          class="users__keyword"
          @clear="onSearch"
        >
          <el-button slot="append" icon="el-icon-search" @click="onSearch" />
        </el-input>
      </el-form-item>

      <el-form-item>
        <el-button icon="el-icon-refresh" :loading="loading" @click="onRefresh">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-table v-loading="loading" :data="rows" border class="users__table">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="username" label="用户名" min-width="130">
        <template slot-scope="{ row }">
          <span>{{ row.username }}</span>
          <el-tag v-if="isSelf(row)" size="mini" class="users__self">我</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="nickname" label="昵称" min-width="120">
        <template slot-scope="{ row }">{{ row.nickname || '—' }}</template>
      </el-table-column>
      <el-table-column prop="email" label="邮箱" min-width="180">
        <template slot-scope="{ row }">{{ row.email || '—' }}</template>
      </el-table-column>
      <el-table-column label="角色" width="110">
        <template slot-scope="{ row }">
          <el-tag :type="roleTag(row.role)" size="mini">{{ roleLabel(row.role) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template slot-scope="{ row }">
          <span :class="row.status === 'disabled' ? 'users__disabled' : ''">
            {{ statusLabel(row.status) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="AI 调用" width="100" align="right">
        <template slot-scope="{ row }">{{ row.usage_count }}</template>
      </el-table-column>
      <el-table-column label="上次登录" width="150">
        <template slot-scope="{ row }">{{ formatDateTime(row.last_login_at) }}</template>
      </el-table-column>
      <el-table-column label="注册时间" width="150">
        <template slot-scope="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right" align="center">
        <template slot-scope="{ row }">
          <el-button type="text" size="small" :disabled="forbidden" @click="openEdit(row)">
            编辑
          </el-button>
        </template>
      </el-table-column>

      <template slot="empty">
        <div class="empty-state">
          <p class="empty-state__title">{{ emptyText }}</p>
          <p v-if="!forbidden" class="empty-state__hint">用户在 LifeAI 主站注册后就会出现在这里</p>
        </div>
      </template>
    </el-table>

    <div class="users__pager">
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

    <!-- 编辑抽屉。规则都在后端：改自己 / 降级最后一个管理员会 422，
         这里只负责把后端那句话原样弹出来，不在前端猜规则 -->
    <el-drawer
      :visible.sync="drawerVisible"
      :title="drawerTitle"
      size="420px"
      :before-close="closeEdit"
    >
      <div v-if="editing" class="users__drawer">
        <el-form label-width="72px" size="small">
          <el-form-item label="账号">
            <span class="users__drawer-account">
              {{ editing.username }}
              <span v-if="editing.nickname" class="users__drawer-nick">（{{ editing.nickname }}）</span>
            </span>
          </el-form-item>

          <el-form-item label="角色">
            <el-radio-group v-model="form.role" :disabled="isSelf(editing)">
              <el-radio v-for="item in roleOptions" :key="item.value" :label="item.value">
                {{ item.label }}
              </el-radio>
            </el-radio-group>
            <p v-if="isSelf(editing)" class="users__drawer-tip">
              不能修改自己的角色，请让其他管理员操作
            </p>
          </el-form-item>

          <el-form-item label="状态">
            <el-radio-group v-model="form.status" :disabled="isSelf(editing)">
              <el-radio v-for="item in statusOptions" :key="item.value" :label="item.value">
                {{ item.label }}
              </el-radio>
            </el-radio-group>
            <p v-if="isSelf(editing)" class="users__drawer-tip">不能禁用自己</p>
          </el-form-item>
        </el-form>

        <p class="users__drawer-note">
          禁用后该账号的登录请求会被拒绝（403），但历史数据不会删除。
        </p>
      </div>

      <div slot="footer" class="users__drawer-footer">
        <el-button size="small" @click="closeEdit">取消</el-button>
        <el-button
          size="small"
          type="primary"
          :loading="saving"
          :disabled="!canSave"
          @click="onSave"
        >
          保存
        </el-button>
      </div>
    </el-drawer>
  </div>
</template>

<script>
import { listUsers, updateUser } from '@/api/admin'
import { formatDateTime } from '@/utils/format'

// 用户管理页：筛选 + 分页 + 行内编辑（改角色 / 改状态）。
//
// 权限边界：能不能看到、能不能改，全由后端 /admin/* 上的 require_role("admin")
// 决定；这里的禁用态只是「别让人点了才挨一句 403」。

const ROLE_OPTIONS = [
  { value: 'user', label: '普通用户' },
  { value: 'vip', label: 'VIP' },
  { value: 'admin', label: '管理员' }
]
const ROLE_LABELS = { user: '普通用户', vip: 'VIP', admin: '管理员' }
const ROLE_TAGS = { user: 'info', vip: 'warning', admin: 'success' }

const STATUS_OPTIONS = [
  { value: 'active', label: '启用' },
  { value: 'disabled', label: '禁用' }
]
const STATUS_LABELS = { active: '正常', disabled: '已禁用' }

const PAGE_SIZES = [10, 20, 50]

export default {
  name: 'AdminUsersView',

  data() {
    return {
      loading: false,
      saving: false,
      rows: [],
      total: 0,
      // 403 单独存：它不是「加载失败」，是「你没权限」，文案和空表格状态都不一样
      forbidden: false,

      // 输入中的筛选条件：改这里不发请求，点搜索才生效
      query: {
        page: 1,
        size: 20,
        keyword: '',
        role: '',
        status: ''
      },

      drawerVisible: false,
      editing: null,
      form: { role: 'user', status: 'active' },

      roleOptions: ROLE_OPTIONS,
      statusOptions: STATUS_OPTIONS,
      pageSizes: PAGE_SIZES
    }
  },

  computed: {
    emptyText() {
      if (this.forbidden) return '当前账号不是管理员，看不到用户列表'
      return this.hasFilter ? '没有符合筛选条件的用户' : '还没有用户'
    },

    hasFilter() {
      return Boolean(this.query.keyword || this.query.role || this.query.status)
    },

    drawerTitle() {
      if (!this.editing) return '编辑用户'
      return `编辑用户 #${this.editing.id} · ${this.editing.username}`
    },

    // 自己不能改自己：后端会 422，前端先把保存按钮按住
    canSave() {
      if (!this.editing) return false
      if (this.isSelf(this.editing)) return false
      return this.form.role !== this.editing.role || this.form.status !== this.editing.status
    }
  },

  created() {
    this.fetchData()
    // 登录后布局里已经在请求 /auth/me 了，这里再等一次拿的就是同一个 Promise，不会多发
    this.$store.dispatch('user/fetchMe')
  },

  methods: {
    async fetchData() {
      this.loading = true
      this.forbidden = false
      try {
        // silent：失败时下面的 catch 自己决定怎么提示，避免 request.js 和页面弹两条
        const data = await listUsers(this.query, { silent: true })
        this.rows = data.items || []
        this.total = data.total || 0
      } catch (error) {
        this.rows = []
        this.total = 0
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

    // ---------- 编辑 ----------
    openEdit(row) {
      this.editing = row
      this.form = { role: row.role, status: row.status }
      this.drawerVisible = true
    },

    // el-drawer 的 before-close 会传 done 进来，必须调它抽屉才关；
    // footer 的「取消」直接调本方法，没有参数，改 visible 即可
    closeEdit(done) {
      if (this.saving) return
      if (typeof done === 'function') done()
      else this.drawerVisible = false
      this.editing = null
    },

    async onSave() {
      if (!this.editing || !this.canSave) return
      const target = this.editing
      const payload = {}
      if (this.form.role !== target.role) payload.role = this.form.role
      if (this.form.status !== target.status) payload.status = this.form.status

      const changes = []
      if (payload.role) changes.push(`角色改为「${ROLE_LABELS[payload.role]}」`)
      if (payload.status) changes.push(`状态改为「${STATUS_LABELS[payload.status]}」`)
      if (payload.status === 'disabled') changes.push('该账号将无法登录')

      try {
        await this.$confirm(`确定要把 ${target.username} 的${changes.join('，')}吗？`, '确认修改', {
          confirmButtonText: '确定',
          cancelButtonText: '取消',
          type: 'warning'
        })
      } catch (e) {
        return // 用户点了取消
      }

      this.saving = true
      try {
        const updated = await updateUser(target.id, payload, { silent: true })
        // 就地替换这一行，不重拉列表：重拉会丢掉当前页码下的滚动位置，
        // 而且如果筛选条件把身份挡住了，人也没了，看着像操作失败
        const index = this.rows.findIndex((item) => item.id === target.id)
        if (index !== -1) this.$set(this.rows, index, { ...target, ...updated })
        this.$message.success('已保存')
        this.drawerVisible = false
        this.editing = null
        // 如果列表带角色/状态筛选，改完可能已经不符合条件，重拉一次对得上
        if (this.query.role || this.query.status) this.fetchData()
      } catch (error) {
        // 422 的原文（不能改自己的角色 / 最后一个管理员不能降级）原样弹出来
        if (error && error.status !== 401) {
          this.$message.error((error && error.message) || '保存失败')
        }
      } finally {
        this.saving = false
      }
    },

    // ---------- 展示 ----------
    isSelf(row) {
      const me = this.$store.state.user.user
      return Boolean(me && row && me.id === row.id)
    },

    roleLabel(role) {
      return ROLE_LABELS[role] || role || ''
    },

    roleTag(role) {
      return ROLE_TAGS[role] || 'info'
    },

    statusLabel(status) {
      return STATUS_LABELS[status] || status || ''
    },

    formatDateTime(value) {
      return formatDateTime(value) || '—'
    }
  }
}
</script>

<style lang="scss" scoped>
.users {
  &__alert {
    margin-bottom: 12px;
  }

  &__alert-text {
    margin: 4px 0 0;
    font-size: 12px;
    line-height: 1.7;
  }

  &__toolbar {
    margin-bottom: 4px;

    .el-form-item {
      margin-bottom: 10px;
    }
  }

  &__select {
    width: 130px;
  }

  &__keyword {
    width: 240px;
  }

  &__table {
    background: #fff;
  }

  &__self {
    margin-left: 6px;
    transform: scale(0.85);
  }

  &__disabled {
    color: #f56c6c;
  }

  &__pager {
    display: flex;
    justify-content: flex-end;
    margin-top: 12px;
  }

  &__drawer {
    padding: 0 20px;
  }

  &__drawer-account {
    font-size: 13px;
    color: #303133;
  }

  &__drawer-nick {
    color: #909399;
  }

  &__drawer-tip {
    margin: 4px 0 0;
    font-size: 12px;
    color: #e6a23c;
    line-height: 1.6;
  }

  &__drawer-note {
    margin: 16px 0 0;
    font-size: 12px;
    color: #909399;
    line-height: 1.7;
  }

  &__drawer-footer {
    text-align: right;
    padding: 0 20px;
  }
}
</style>