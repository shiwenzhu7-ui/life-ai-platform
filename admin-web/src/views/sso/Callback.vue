<template>
  <div class="sso-callback">
    <div class="sso-callback__box">
      <template v-if="phase === 'done'">
        <i class="el-icon-circle-check sso-callback__icon sso-callback__icon--ok" />
        <p class="sso-callback__title">登录成功</p>
        <p class="sso-callback__hint">正在进入……没反应就点<el-link type="primary" @click="goNext">这里</el-link></p>
      </template>

      <template v-else>
        <i class="el-icon-warning sso-callback__icon sso-callback__icon--fail" />
        <p class="sso-callback__title">登录没有完成</p>
        <p class="sso-callback__hint">{{ errorText }}</p>
        <el-button type="primary" size="small" @click="backToLogin">回登录页</el-button>
      </template>
    </div>
  </div>
</template>

<script>
// 账号中心登录完回落到这里：?token=<本地JWT>&next=<站内路径>
//
// 和 life-web 的 Callback.vue 是同一套思路，多两条后台特有的顾虑：
//   1. 令牌存在 localStorage 里（utils/auth.js），和账号密码登录完全同一条路，
//      所以 request.js 的请求拦截、路由守卫一个字都不用改。
//   2. **不能无条件跳 /users**：通过账号中心登进来的可能是普通用户，
//      写死 /users 就会让他看一屏 403 红条。这里先等 /auth/me 返回角色，
//      是管理员才去 /users，否则直接送 /403 并说明原因。
//   3. 本页必须是 public（路由守卫放行）：此刻令牌还没进 localStorage，
//      守卫要是拿 hasValidToken() 判断，会把刚登录的人踢回 /login —— 死循环。
import { setToken } from '@/utils/auth'
import { saveIdToken, safeNextPath, ssoErrorText } from '@/utils/sso'

// 等 /auth/me 的最长时间。它挂了也不能让人卡在「正在进入……」上。
const ME_TIMEOUT_MS = 3000

// created 里排的定时器，销毁时要取消，否则页面都换了还往旧地址跳
let leaveTimer = null

export default {
  name: 'SsoCallbackView',

  data() {
    return {
      phase: 'working', // working | done | failed
      nextPath: '/users',
      errorText: ''
    }
  },

  async created() {
    const query = this.$route.query
    const token = query.token
    const ssoError = query.sso_error

    if (ssoError) {
      this.fail(ssoErrorText(ssoError))
      return
    }
    if (!token || typeof token !== 'string') {
      // 直接手敲这个地址的人会走到这里
      this.fail('没有收到登录凭证，请重新登录')
      return
    }

    this.nextPath = safeNextPath(query.next)
    setToken(token) // 不传 expiresIn：不写过期时间戳，由后端 401 兜底
    // id_token 也留一份：退出时要拿它当 id_token_hint，后端才知道「退谁的
    // Keycloak 会话」。放 sessionStorage 而不是 localStorage —— 关掉标签页就没了，
    // 而且它只对「退出」有用，不值得长期留存。
    saveIdToken(query.id_token)
    this.$store.commit('user/SET_TOKEN', token)
    this.phase = 'done'

    // 把 ?token=… 从地址栏和浏览器历史里抹掉（replace 不新增历史记录）
    this.$router.replace({ path: '/sso/callback', query: {} })

    const user = await this.withTimeout(this.$store.dispatch('user/fetchMe'))
    if (user && user.role && user.role !== 'admin') {
      this.nextPath = '/403'
    }

    leaveTimer = window.setTimeout(() => this.goNext(), 300)
  },

  beforeDestroy() {
    if (leaveTimer) {
      window.clearTimeout(leaveTimer)
      leaveTimer = null
    }
  },

  methods: {
    // 拿不到结果（超时/失败）就当未知角色处理：路由守卫和 afterMe 那一刀还在，
    // 不会把普通用户长期留在后台页里。
    withTimeout(promise) {
      return Promise.race([
        Promise.resolve(promise).catch(() => null),
        new Promise((resolve) => window.setTimeout(() => resolve(null), ME_TIMEOUT_MS))
      ])
    },

    fail(text) {
      this.phase = 'failed'
      this.errorText = text
    },

    goNext() {
      if (this.$route.path !== '/sso/callback') return
      this.$router.replace(this.nextPath)
    },

    backToLogin() {
      this.$router.replace('/login')
    }
  }
}
</script>

<style lang="scss" scoped>
.sso-callback {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #304156 0%, #1f2d3d 100%);

  &__box {
    width: 340px;
    padding: 32px 28px;
    background: #fff;
    border-radius: 6px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.24);
    text-align: center;
  }

  &__icon {
    font-size: 42px;

    &--ok {
      color: #67c23a;
    }

    &--fail {
      color: #f56c6c;
    }
  }

  &__title {
    margin: 12px 0 6px;
    font-size: 16px;
    color: #303133;
  }

  &__hint {
    margin: 0 0 16px;
    font-size: 13px;
    line-height: 1.6;
    color: #909399;
  }
}
</style>