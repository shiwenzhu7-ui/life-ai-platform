const { defineConfig } = require('@vue/cli-service')

module.exports = defineConfig({
  // 关掉「把 node_modules 也过 babel」：开着会让 element-ui 的 1MB 大文件进 babel，
  // 每次启动都打 [BABEL] deoptimised 警告、编译还慢。它俩本来就发 ES5 产物。
  transpileDependencies: false,

  css: {
    loaderOptions: {
      scss: {
        // sass 1.83 + sass-loader 13 还在走 legacy JS API，每次编译都打 Deprecation。
        // 官方给的过渡开关就是 silenceDeprecations，等 sass-loader 升到 14 再删。
        sassOptions: { silenceDeprecations: ['legacy-js-api'] }
      }
    }
  },

  // 开发服务器：固定 8080，并把 /api 代理到后端。
  // 走代理的好处：浏览器看到的是同源请求，既不触发 CORS，
  // 也不用在前端写死后端端口（后端实际跑在 8010，文档里写的是 8000）。
  devServer: {
    port: 8080,
    // history 模式下直接访问 /ai、/tasks 也要能打开（否则只能从首页点进来）
    historyApiFallback: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true
      }
    }
  }
})
