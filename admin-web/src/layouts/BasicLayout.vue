<template>
  <el-container class="layout">
    <el-aside width="200px" class="layout__aside">
      <div class="layout__brand">
        <span class="layout__brand-text">LifeAI</span>
        <span class="layout__brand-sub">管理后台</span>
      </div>

      <el-menu
        :default-active="activeMenu"
        router
        class="layout__menu"
        background-color="#304156"
        text-color="#bfcbd9"
        active-text-color="#ffffff"
      >
        <el-menu-item v-for="item in menus" :key="item.path" :index="item.path">
          <i :class="item.icon" />
          <span slot="title">{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header height="56px" class="layout__header">
        <span class="layout__title">{{ $route.meta.title || '' }}</span>

        <el-dropdown trigger="click" class="layout__user-dropdown" @command="onUserCommand">
          <div class="layout__user">
            <i class="el-icon-user-solid" />
            <span>{{ displayName }}</span>
            <!-- 不是管理员还停在后台页面上，说明是令牌有效但权限不够，得让人一眼看出来 -->
            <el-tag v-if="user && !isAdmin" size="mini" type="danger" class="layout__tag">
              {{ roleLabel }}
            </el-tag>
            <!-- 管理员分两级，界面上要说清自己是哪一级：只有根管理员能授予管理员 -->
            <el-tag v-else-if="isRootAdmin" size="mini" type="danger" class="layout__tag">
              根管理员
            </el-tag>
            <el-tag v-else-if="user" size="mini" type="info" class="layout__tag">
              管理员
            </el-tag>
            <i class="el-icon-arrow-down layout__user-arrow" />
          </div>
          <el-dropdown-menu slot="dropdown">
            <el-dropdown-item command="logout">退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </el-dropdown>
      </el-header>

      <el-main class="layout__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script>
import { mapGetters, mapState } from 'vuex'
import { redirectToSsoLogout } from '@/utils/sso'

// 菜单项与 router 里的路径一一对应；改路径要两边一起改。
const MENUS = [
  { path: '/users', title: '用户管理', icon: 'el-icon-user' },
  { path: '/usage', title: 'AI 调用明细', icon: 'el-icon-data-line' },
  { path: '/roles', title: '角色说明', icon: 'el-icon-s-custom' }
]

const ROLE_LABELS = { user: '普通用户', vip: 'VIP', admin: '管理员' }

export default {
  name: 'BasicLayout',
  data() {
    return {
      menus: MENUS
    }
  },
  computed: {
    ...mapState('user', ['user']),
    ...mapGetters('user', ['displayName', 'isAdmin', 'isRootAdmin']),

    activeMenu() {
      return this.$route.path
    },

    roleLabel() {
      const role = this.user && this.user.role
      return ROLE_LABELS[role] || role || ''
    }
  },
  created() {
    // 刷新后 Vuex 里只有令牌、没有用户信息，这里补一次 /auth/me（action 内部有去重）
    this.$store.dispatch('user/fetchMe')
  },
  methods: {
    async onUserCommand(command) {
      if (command !== 'logout') return
      try {
        await this.$confirm('确定退出登录吗？', '提示', {
          confirmButtonText: '退出',
          cancelButtonText: '取消',
          type: 'warning'
        })
      } catch (e) {
        return // 用户点了取消
      }
      await this.$store.dispatch('user/logout')
      // 这里不是 router.replace 而是整页跳：
      //   本地令牌已经清了，但浏览器里的 Keycloak 会话还在。只换路由的话，
      //   下一次打开任何一个前端都会静默探测成功，用户会觉得「退不出去」。
      //   redirectToSsoLogout 会先 302 到 Keycloak 清会话，再 302 回 /login，
      //   整页加载 = Vuex/localStorage 里残留的东西也一并被扔掉。
      redirectToSsoLogout({ redirect: '/login' })
    }
  }
}
</script>

<style lang="scss" scoped>
.layout {
  height: 100%;

  &__aside {
    background: #304156;
    display: flex;
    flex-direction: column;
  }

  &__brand {
    height: 56px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    color: #fff;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }

  &__brand-text {
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 1px;
  }

  &__brand-sub {
    font-size: 11px;
    color: #8a97a8;
  }

  &__menu {
    flex: 1;
    border-right: none;
  }

  &__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #fff;
    border-bottom: 1px solid #e6e8eb;
  }

  &__title {
    font-size: 15px;
    font-weight: 600;
    color: #303133;
  }

  &__user-dropdown {
    cursor: pointer;
  }

  &__user {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: #606266;

    &:hover {
      color: #409eff;
    }
  }

  &__tag {
    transform: scale(0.9);
  }

  &__user-arrow {
    color: #c0c4cc;
    font-size: 12px;
  }

  &__main {
    padding: 16px;
    background: #f5f7fa;
    overflow: auto;
  }
}
</style>