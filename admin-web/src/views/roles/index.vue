<template>
  <div class="roles">
    <el-alert
      class="roles__tip"
      type="info"
      :closable="false"
      show-icon
      title="角色由管理员在「用户管理」里调整；这里只是说明，改不了任何东西。"
    />

    <el-row :gutter="16" v-loading="loading">
      <el-col v-for="item in roles" :key="item.role" :xs="24" :sm="24" :md="8">
        <el-card shadow="never" class="roles__card">
          <div slot="header" class="roles__header">
            <span class="roles__label">{{ item.label }}</span>
            <code class="roles__code">{{ item.role }}</code>
          </div>

          <p class="roles__summary">{{ item.summary }}</p>

          <ul class="roles__abilities">
            <li v-for="(line, i) in item.abilities" :key="i">{{ line }}</li>
          </ul>
        </el-card>
      </el-col>
    </el-row>

    <el-empty
      v-if="!loading && roles.length === 0"
      description="没有拿到角色说明，点一下刷新"
    >
      <el-button size="small" @click="fetchRoles">刷新</el-button>
    </el-empty>
  </div>
</template>

<script>
import { listRoles } from '@/api/admin'

// 角色说明（只读）。文案全部由后端返回：改文案只改 backend 的 ROLE_CATALOG，
// 前端一个常量都不留，免得两边写着写着对不上。
export default {
  name: 'AdminRolesView',

  data() {
    return {
      loading: false,
      roles: []
    }
  },

  created() {
    this.fetchRoles()
  },

  methods: {
    async fetchRoles() {
      this.loading = true
      try {
        const data = await listRoles({ silent: true })
        this.roles = Array.isArray(data) ? data : []
      } catch (error) {
        this.roles = []
        // 401 由 request.js 统一跳登录页，这里只处理其余情况
        if (error && error.status !== 401) {
          this.$message.error(error.message || '加载失败')
        }
      } finally {
        this.loading = false
      }
    }
  }
}
</script>

<style lang="scss" scoped>
.roles {
  &__tip {
    margin-bottom: 16px;
  }

  &__card {
    margin-bottom: 16px;
  }

  &__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__label {
    font-weight: 600;
    color: #303133;
  }

  &__code {
    font-size: 12px;
    color: #909399;
    background: #f5f7fa;
    padding: 1px 6px;
    border-radius: 3px;
  }

  &__summary {
    margin: 0 0 12px;
    font-size: 13px;
    color: #606266;
  }

  &__abilities {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
    color: #606266;
    line-height: 1.9;
  }
}
</style>