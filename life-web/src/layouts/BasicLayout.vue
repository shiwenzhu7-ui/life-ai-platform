<template>
  <el-container class="layout">
    <!-- 左侧导航：V1.0 三页 + V1.1 的 AI 助手；预算/购物等后续再往这里加 item -->
    <el-aside width="200px" class="layout__aside">
      <div class="layout__brand">
        <span class="layout__brand-text">LifeAI</span>
        <span class="layout__brand-sub">智能生活管理</span>
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

        <!-- 真实登录用户（V1.2）。displayName 来自 store/modules/user.js：
             有昵称用昵称，没有用账号名；/auth/me 还没回来时先显示账号名兜底 -->
        <el-dropdown trigger="click" class="layout__user-dropdown" @command="onUserCommand">
          <div class="layout__user">
            <i class="el-icon-user-solid" />
            <span>{{ displayName }}</span>
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
import { mapGetters } from 'vuex'
import { redirectToSsoLogout } from '@/utils/sso'

// 菜单项与 router 里的路径一一对应；改路径要两边一起改。
const MENUS = [
  { path: '/', title: '首页', icon: 'el-icon-s-home' },
  { path: '/tasks', title: '待办', icon: 'el-icon-s-order' },
  { path: '/expenses', title: '消费', icon: 'el-icon-s-finance' },
  { path: '/shopping', title: '购物', icon: 'el-icon-shopping-cart-2' },
  { path: '/knowledge', title: '知识库', icon: 'el-icon-notebook-2' },
  { path: '/ai', title: 'AI 助手', icon: 'el-icon-chat-dot-round' }
]

export default {
  name: 'BasicLayout',
  data() {
    return {
      menus: MENUS
    }
  },
  computed: {
    ...mapGetters('user', ['displayName']),

    // default-active 用 path 精确匹配，/ 这种根路径直接相等
    activeMenu() {
      return this.$route.path
    }
  },
  created() {
    // 刷新页面后 Vuex 里只有令牌、没有用户信息，这里补一次 /auth/me。
    // action 内部有并发去重和失败兜底，重复调用是安全的。
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
      // action 内部已经处理了「后端挂了也要清本地令牌」
      await this.$store.dispatch('user/logout')
      // 这里不是 router.replace 而是整页跳：
      //   本地令牌已经清了，但浏览器里的 Keycloak 会话还在。只换路由的话，
      //   登录页的静默探测会立刻把人再放进来 —— 用户看到的就是「退不出去」。
      //   redirectToSsoLogout 先 302 到 Keycloak 清会话，再 302 回 /login。
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
