<template>
  <div id="app">
    <router-view />
  </div>
</template>

<style lang="scss">
*,
*::before,
*::after {
  box-sizing: border-box;
}

html,
body {
  margin: 0;
  padding: 0;
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  color: #303133;
  background: #f5f7fa;
}

#app {
  height: 100%;
}

// 表格空态：各页共用的两行文案
.empty-state {
  padding: 26px 0 10px;
  text-align: center;

  &__title {
    margin: 0 0 6px;
    font-size: 14px;
    color: #606266;
  }

  &__hint {
    margin: 0 0 14px;
    font-size: 12px;
    color: #909399;
  }
}
</style>