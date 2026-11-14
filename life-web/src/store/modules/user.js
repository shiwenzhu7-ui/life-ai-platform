import * as authApi from '@/api/auth'
import { clearToken, getToken, hasValidToken, setToken } from '@/utils/auth'
import { clearSilentTried } from '@/utils/sso'

// 登录态。三件事：令牌、当前用户、登录/登出动作。
//
// 谁是「真相」：localStorage 里的令牌是唯一真相，store 只是它的内存投影。
// 路由守卫读 localStorage（utils/auth.js）而不是读 state.token ——
// 守卫可能在 Vue 实例挂载之前就跑，那时 store 不一定初始化好了。

// 正在飞的 /auth/me 请求。刷新页面后布局和页面可能同时要用户信息，
// 不能各发一次；放在模块级而不是 state 里，理由同 categories.js。
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

    // 顶部条显示什么：有昵称优先昵称，没有就用账号名
    displayName: (state) => {
      if (!state.user) return ''
      return state.user.nickname || state.user.username
    }
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
    // 登录。成功返回后端原始响应，失败时把后端的中文提示放进 message 再抛出去，
    // 页面只管 catch 后显示，不用自己拼文案。
    async login({ commit }, payload) {
      let data
      try {
        data = await authApi.login(payload)
      } catch (error) {
        throw new Error(toErrorMessage(error, '登录失败，请稍后重试'))
      }

      setToken(data.access_token, data.expires_in)
      commit('SET_TOKEN', data.access_token)
      commit('SET_USER', data.user)
      return data
    },

    // 注册。后端成功只回 UserBrief，不含令牌 —— 注册完还要再登录一次。
    async register(ctx, payload) {
      try {
        return await authApi.register(payload)
      } catch (error) {
        throw new Error(toErrorMessage(error, '注册失败，请稍后重试'))
      }
    },

    // 拉当前用户（显示名/角色）。silent：这个请求失败不该在界面上弹红条 ——
    // 它通常是后台补数据，用户没主动点什么，弹出来只会莫名其妙。
    // 并发调用共用同一个 Promise，避免刷新页面时发两次 /auth/me。
    fetchMe({ commit, state }) {
      if (!hasValidToken()) return Promise.resolve(null)
      if (pendingMe) return pendingMe

      pendingMe = authApi
        .me({ silent: true })
        .then((data) => {
          commit('SET_USER', data)
          return data
        })
        .catch((error) => {
          // 401 已经由 request.js 清令牌并跳登录页了，这里不再重复处理；
          // 其它错误（比如后端挂了）保留 state.user 不动，界面继续显示旧昵称。
          if (error && error.status === 401) commit('CLEAR')
          return state.user
        })
        .finally(() => {
          pendingMe = null
        })

      return pendingMe
    },

    // 退出。后端 /auth/logout 是空实现（本地 JWT 无状态），
    // 所以本地一定要先把令牌清掉：后端挂了也得能退出，不能把用户卡在里面。
    //
    // 但只清本地不够 —— 浏览器里还留着 Keycloak 的会话 Cookie，登录页的静默
    // 探测会立刻把人再放进来。清完之后由调用方发起 redirectToSsoLogout，
    // 走一趟 Keycloak 的 logout 把那份会话也清掉。
    async logout({ commit }) {
      try {
        await authApi.logout({ silent: true })
      } catch (error) {
        // 故意忽略：退出是本地行为，服务端的失败不该阻止用户离开
      }
      clearToken()
      commit('CLEAR')
      // 退出后允许登录页再静默探一次账号中心（见 utils/sso.js）：
      // 不给自己留后路的「已探过」标记会让换账号登录的人被挡在 SSO 外面。
      clearSilentTried()
    }
  }
}