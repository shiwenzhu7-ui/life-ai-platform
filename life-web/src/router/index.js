import Vue from 'vue'
import VueRouter from 'vue-router'

Vue.use(VueRouter)

// V1.0 只有三条业务路由 + 404，其余页面等对应版本再加。
const routes = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('../views/dashboard/index.vue'),
    meta: { title: '首页' }
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('../views/tasks/index.vue'),
    meta: { title: '待办' }
  },
  {
    path: '/expenses',
    name: 'expenses',
    component: () => import('../views/expenses/index.vue'),
    meta: { title: '消费' }
  },
  {
    path: '*',
    name: 'not-found',
    component: () => import('../views/NotFound.vue'),
    meta: { title: '页面不存在' }
  }
]

const router = new VueRouter({
  mode: 'history',
  base: process.env.BASE_URL,
  routes
})

export default router
