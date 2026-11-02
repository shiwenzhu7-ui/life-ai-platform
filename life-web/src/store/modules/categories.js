import { listCategories } from '@/api/categories'

// 分类字典：41 条、7 个 scope，待办页/消费页/首页都要用。
// 缓存 10 分钟 + 并发去重，避免每次进页面都拉一遍、避免连点两次发两个请求。
const TTL = 10 * 60 * 1000

// 正在飞行的请求放在模块级而不是 state 里：
// Promise 塞进 Vuex state 会被 devtools 序列化、也不参与响应式，放外面更干净。
const pending = {}

export default {
  namespaced: true,

  state: () => ({
    byScope: {}, // { task: [CategoryOut, ...], expense: [...] }
    loadedAt: {} // { task: 时间戳 }
  }),

  getters: {
    // store.getters['categories/listOf']('task')
    listOf: (state) => (scope) => state.byScope[scope] || [],

    // store.getters['categories/nameOf']('task', 18) → '购物'
    nameOf: (state) => (scope, categoryId) => {
      const hit = (state.byScope[scope] || []).find((item) => item.id === categoryId)
      return hit ? hit.name : ''
    }
  },

  mutations: {
    SET_ITEMS(state, { scope, items }) {
      // 整对象替换而不是 state.byScope[scope] = items，
      // 这样 Vue 2 的响应式一定能追踪到新增的 scope 键。
      state.byScope = { ...state.byScope, [scope]: items }
      state.loadedAt = { ...state.loadedAt, [scope]: Date.now() }
    },

    CLEAR(state, scope) {
      // 只把时间戳清成 0，不动数据：下次进页面会先用旧数据显示、同时在后台重新拉，
      // 失败也不会白屏。
      state.loadedAt = { ...state.loadedAt, [scope]: 0 }
    }
  },

  actions: {
    // 幂等：有缓存且没过期直接返回；正在请求就复用同一个 promise
    async load({ state, commit }, scope) {
      const loadedAt = state.loadedAt[scope]
      if (loadedAt && Date.now() - loadedAt < TTL) {
        return state.byScope[scope]
      }
      if (pending[scope]) return pending[scope]

      const request = listCategories(scope, { silent: true })
        .then((items) => {
          commit('SET_ITEMS', { scope, items })
          return items
        })
        .finally(() => {
          delete pending[scope]
        })

      pending[scope] = request
      return request
    },

    // 后台改了分类（V1.4 才有分类管理），用这个主动作废缓存
    invalidate({ commit }, scope) {
      commit('CLEAR', scope)
    }
  }
}
