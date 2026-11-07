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

        <!-- V1.0 没有登录，用户固定是后端 core/deps.py 里的 DEMO_USER_ID = 1 -->
        <div class="layout__user">
          <i class="el-icon-user-solid" />
          <span>演示用户 #1</span>
          <el-tooltip content="V1.2 接入登录后这里换成真实用户" placement="bottom-end">
            <i class="el-icon-question layout__user-help" />
          </el-tooltip>
        </div>
      </el-header>

      <el-main class="layout__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script>
// 菜单项与 router 里的路径一一对应；改路径要两边一起改。
const MENUS = [
  { path: '/', title: '首页', icon: 'el-icon-s-home' },
  { path: '/tasks', title: '待办', icon: 'el-icon-s-order' },
  { path: '/expenses', title: '消费', icon: 'el-icon-s-finance' },
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
    // default-active 用 path 精确匹配，/ 这种根路径直接相等
    activeMenu() {
      return this.$route.path
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

  &__user {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: #606266;
  }

  &__user-help {
    color: #c0c4cc;
    cursor: help;
  }

  &__main {
    padding: 16px;
    background: #f5f7fa;
    overflow: auto;
  }
}
</style>
