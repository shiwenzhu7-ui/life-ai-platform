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
// Keycloak 登录完回落到这里：?token=<本地JWT>&next=<站内路径>
//
// 三个必须记住的细节：
//   1. 地址栏里的令牌是**一次性**的。读完立刻 router.replace 把 query 洗掉，
//      否则用户一截图、一复制地址，令牌就外泄了。
//   2. 这一页必须是 public（路由守卫放行）：新登录落地时令牌还在地址栏里、
//      没进 localStorage，守卫要是拿 hasValidToken() 判断，会把刚登录的人
//      直接踢回 /login —— 死循环。
//   3. 令牌存在 localStorage 里（utils/auth.js），和账号密码登录完全同一条路，
//      所以后面的 request.js 请求拦截、路由守卫一个字都不用改。
//
// 但「已经登录的人退回到这一页」是另一回事（见 created 里第一段）：
// 那一格被浏览器记在历史栈里删不掉，人可能按 ← 退回来，
// 这时手里已经有令牌，必须直接送回应用，而不是显示「登录没有完成」。
import { setToken, hasValidToken } from '@/utils/auth'
import { saveIdToken, safeNextPath, ssoErrorText } from '@/utils/sso'

// created 里排的自动跳转，销毁时要取消，否则页面都换了还往旧地址跳
let leaveTimer = null

export default {
  name: 'SsoCallbackView',

  data() {
    return {
      phase: 'working', // working | done | failed
      nextPath: '/',
      errorText: ''
    }
  },

  created() {
    const query = this.$route.query
    const token = query.token
    const ssoError = query.sso_error

    // ⚠️ 最重要的一条：**已经登录的人，不该再看到「登录没有完成」**。
    //
    // 场景：这一页会被浏览器记进历史栈（登录路上的 302 链各占一格，页面删不掉），
    // 用户登录成功后连按几次 ← 就会退回到这里。上一版这里无条件显示失败页，
    // 于是「明明已经登进去了，按 ← 看到登录失败」，就是用户反复报的那个问题。
    //
    // 判定条件只有「手里有没有一张还能用的令牌」：
    //   - 有 → 他已经是登录状态，这个失败页对他没有任何信息量，直接送回应用。
    //   - 没有 → 确实没登上，老老实实显示错误和「回登录页」。
    // 用 hasValidToken（本地看令牌 + 过期时间），不看 sso_error 是什么，
    // 因为无论错误码是 sso_silent 还是 state_expired，已登录时都不该拦人。
    if (hasValidToken()) {
      this.phase = 'done'
      this.nextPath = safeNextPath(query.next)
      // 整页跳，丢掉这一格的错误参数；replace 不新增历史
      window.location.replace(this.nextPath)
      return
    }

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
    this.$store.commit('user/SET_TOKEN', token)
    // id_token 只留给退出用（sessionStorage，不进 localStorage）：
    // 后端无状态，退出时得由浏览器把「退哪一次登录」带回去。
    saveIdToken(query.id_token)
    this.phase = 'done'

    // 不等 fetchMe：它失败也不该卡住人，布局挂载后会自己再拉一次
    this.$store.dispatch('user/fetchMe')

    // 把 ?token=… 从地址栏和浏览器历史里抹掉（replace 不新增历史记录）
    this.$router.replace({ path: '/sso/callback', query: {} })

    leaveTimer = window.setTimeout(() => this.goNext(), 300)
  },

  beforeDestroy() {
    if (leaveTimer) {
      window.clearTimeout(leaveTimer)
      leaveTimer = null
    }
  },

  methods: {
    fail(text) {
      this.phase = 'failed'
      this.errorText = text
    },

    goNext() {
      if (this.$route.path !== '/sso/callback') return
      // 整页跳，不走 this.$router.replace。
      // 理由和登录页的一样（见 views/login/index.vue 的 leaveTo）：
      // SPA 换路由会让 /login 和 /sso/callback 这两格都留在历史里，
      // 登录之后按 ← 就退回登录页（守卫再弹回来，等于「按了没反应」）。
      // 整页 replace 把当前这一格换成目标页，历史栈里从此没有登录流程的格子。
      window.location.replace(this.nextPath)
    },

    // 整页跳而不是 router.replace：
    //   1. 地址栏里还挂着 ?sso_error=…，replace 成 /login 后再点一次同一个错误码
    //      会算出「同一条路由」，Vue Router 直接拒绝——按钮看着就是没反应；
    //   2. 整页刷新等于把这页的状态彻底清零，登录页的静默探测逻辑从干净状态开始。
    backToLogin() {
      window.location.replace('/login')
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