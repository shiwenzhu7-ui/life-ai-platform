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
</style>
