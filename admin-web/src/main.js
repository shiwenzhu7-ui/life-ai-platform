import Vue from 'vue'
import ElementUI from 'element-ui'
import 'element-ui/lib/theme-chalk/index.css'
// 全局样式必须放这里（普通 css）：写进 App.vue 的 <style> 会变成
// 「style0 was not found」那一串构建警告，而且页面里的 .empty-state 用不了
import './styles/global.css'

import App from './App.vue'
import router from './router'
import store from './store'

Vue.config.productionTip = false

// 与 life-web 保持一致的全局配置：小尺寸控件、zIndex 抬高一点
// （Element UI 默认 2000，容易被浏览器扩展的浮层压住）
Vue.use(ElementUI, { size: 'small', zIndex: 3000 })

new Vue({
  router,
  store,
  render: (h) => h(App)
}).$mount('#app')