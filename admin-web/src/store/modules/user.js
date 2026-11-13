import { login, logout, me } from '@/api/admin'
import { clearToken, getToken, hasValidToken, setLastRole, setToken } from '@/utils/auth'

// 登录态。三件事：令牌、当前用户、登录/登出动作。
//
// 谁是「真相」：localStorage 里的令牌是唯一真相，store 只是它的内存投影。
// 路由守卫读 localStorage（utils/auth.js）而不是读 state.token ——
// 守卫可能在 Vue 实例挂载之前就跑，那时 store 不一定初始化好了。

// 正在飞的 /auth/me。刷新页面后布局和页面可能同时要用用户信息，不能各发一次。
let pendingMe = null

function toErrorMessage(error, fallback) {
  return (error && error.message) || fallback
}

export default {
  namespaced: true,

  state: () => ({
    token: getToken(),
    user: null
  }),

  getters: {
    isLoggedIn: (state) => Boolean(state.token),

    displayName: (state) => {
      if (!state.user) return ''
      return state.user.nickname || state.user.username
    },

    // ⚠️ 这只是「界面上显示什么」的依据，不是安全边界：
    // 真正能不能读到数据，取决于后端 require_role("admin")。
    // 页面上拿它来隐藏改角色按钮，只是别让人点了才挨一句 403。
    isAdmin: (state) => Boolean(state.user && state.user.role === 'admin'),

    // 第一级：根管理员。只有他能把人设为管理员，且谁都动不了他。
    // 判定在后端（permission.is_root），这里只读它给出的 is_root_admin。
    isRootAdmin: (state) => Boolean(state.user && state.user.is_root_admin)
  },

  mutations: {
    SET_TOKEN(state, token) {
      state.token = token || ''
    },
    SET_USER(state, user) {
      state.user = user || null
    },
    CLEAR(state) {
      state.token = ''
      state.user = null
    }
  },

  actions: {
    async login({ commit }, payload) {
      let data
      try {
        data = await login(payload)
      } catch (error) {
        throw new Error(toErrorMessage(error, '登录失败，请稍后重试'))
      }

      // 关键：**先用登录响应把 user 覆盖掉，再发 /auth/me**。
      // 以前这里是 setToken 之后直接 SET_USER(data.user)，顺序没错，但换了账号登录时
      // `data.user` 里没有 is_root_admin，于是 UI 短暂把旧对象当自己 —— 「我」标签
      // 贴到了别人的行上、编辑别人的抽屉被当成改自己而锁死。
      // 先清空再写，宁可短暂显示空，也不显示上一个账号的身份。
      commit('SET_USER', null)
      setToken(data.access_token, data.expires_in)
      // 记下角色，路由守卫下次刷新时能少发一次注定 403 的请求（见 utils/auth.js）
      setLastRole(data.user && data.user.role)
      commit('SET_TOKEN', data.access_token)
      commit('SET_USER', data.user)
      return data
    },

    // silent：这是后台补数据，用户没主动点什么，失败弹红条只会莫名其妙
    fetchMe({ commit, state }) {
      if (!hasValidToken()) return Promise.resolve(null)
      if (pendingMe) return pendingMe

      pendingMe = me({ silent: true })
        .then((data) => {
          commit('SET_USER', data)
          // 角色可能被别的管理员在这期间改过，以服务端为准
          setLastRole(data && data.role)
          return data
        })
        .catch((error) => {
          // 401 已经由 request.js 清令牌并跳登录页，这里不重复处理
          if (error && error.status === 401) commit('CLEAR')
          return state.user
        })
        .finally(() => {
          pendingMe = null
        })

      return pendingMe
    },

    // 退出：后端挂没挂都得能退出去，所以本地一定先清
    async logout({ commit }) {
      try {
        await logout({ silent: true })
      } catch (error) {
        // 故意忽略
      }
      clearToken()
      commit('CLEAR')
    }
  }
}