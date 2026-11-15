<template>
  <div class="forbidden">
    <h2 class="forbidden__title">403</h2>
    <p class="forbidden__hint">
      当前账号不是管理员，进不了 LifeAI 管理后台。
    </p>
    <p v-if="roleLabel" class="forbidden__role">
      当前身份：<el-tag size="mini" type="danger">{{ roleLabel }}</el-tag>
    </p>

    <div class="forbidden__actions">
      <el-button type="primary" size="small" @click="onSwitchAccount">换个账号登录</el-button>
      <el-button size="small" @click="onGoLifeWeb">去生活助手（life-web）</el-button>
    </div>
  </div>
</template>

<script>
import { mapState } from 'vuex'

// 非管理员进后台的落地页。文档（docs/页面设计.md:389）要求「非管理员跳 403 页」。
//
// 两种进得来的人：
//   1. 用普通用户/ VIP 账号登录了这个后台 —— 令牌是真的，只是 /admin/* 全都 403；
//   2. 刚被别的管理员降级，刷新后守卫把他送到这里。
// 所以这里不报错、不自动登出，只给两个出口：换账号、回主站。
const ROLE_LABELS = { user: '普通用户', vip: 'VIP', admin: '管理员' }

// 主站端口。三端同域部署时这里应该是相对路径，V1.x 都是本地 8080/8081/8082 分端口跑。
const LIFE_WEB_URL = process.env.VUE_APP_LIFE_WEB_URL || 'http://127.0.0.1:8080'

export default {
  name: 'ForbiddenView',

  computed: {
    ...mapState('user', ['user']),

    roleLabel() {
      const role = this.user && this.user.role
      return ROLE_LABELS[role] || role || ''
    }
  },

  created() {
    // 刷新在这个页面上时 Vuex 是空的，补一次 /auth/me 才拿得到角色标签
    this.$store.dispatch('user/fetchMe')
  },

  methods: {
    async onSwitchAccount() {
      // 先清令牌再跳：否则守卫看到「有令牌」会把 /login 弹回后台（这正是以前的 bug）
      await this.$store.dispatch('user/logout')
      // 整页跳：SPA 换路由会把 /403 留在历史里，登录后按 ← 又回到无权页
      window.location.replace('/login')
    },

    onGoLifeWeb() {
      window.open(LIFE_WEB_URL, '_blank', 'noopener')
    }
  }
}
</script>

<style lang="scss" scoped>
.forbidden {
  padding: 80px 0;
  text-align: center;

  &__title {
    margin: 0 0 8px;
    font-size: 40px;
    color: #f56c6c;
  }

  &__hint {
    margin: 0 0 8px;
    font-size: 14px;
    color: #606266;
  }

  &__role {
    margin: 0 0 24px;
    font-size: 13px;
    color: #909399;
  }

  &__actions {
    display: flex;
    justify-content: center;
    gap: 12px;
  }
}
</style>