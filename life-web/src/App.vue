<template>
  <div id="app">
    <router-view />
  </div>
</template>

<style lang="scss">
// 全局样式在 styles/ 里，F2 再建；这里先只留最小重置。
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

// 表格空态：待办的「没有符合条件的待办」和消费的「还没有记账」共用这一套
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
