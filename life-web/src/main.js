import Vue from 'vue'
import ElementUI from 'element-ui'
import 'element-ui/lib/theme-chalk/index.css'

import App from './App.vue'
import router from './router'
import store from './store'

Vue.config.productionTip = false

// Element UI 全量引入：V1.0 只有三个页面，按需引入的构建配置不值当；
// 等包体积真成问题时再换 babel-plugin-component。
Vue.use(ElementUI, { size: 'small', zIndex: 3000 })

new Vue({
  router,
  store,
  render: h => h(App)
}).$mount('#app')
