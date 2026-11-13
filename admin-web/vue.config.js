const { defineConfig } = require('@vue/cli-service')

module.exports = defineConfig({
  transpileDependencies: false,

  css: {
    loaderOptions: {
      scss: {
        sassOptions: { silenceDeprecations: ['legacy-js-api'] }
      }
    }
  },

  // 后台固定跑 8082，与 life-web 的 8080、analytics-web 的 8081 错开。
  // 两个前端可以同时开着：令牌存在 localStorage，而 localStorage 按**源**隔离
  // （协议+主机+端口），所以 8080 登 life-web 不会影响 8082 的后台登录态。
  devServer: {
    port: 8082,
    historyApiFallback: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true
      }
    }
  }
})