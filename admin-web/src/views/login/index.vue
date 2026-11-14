<template>
  <div class="login">
    <div class="login__card">
      <!-- 静默探测账号中心的那一两秒：不闪表单，给个「正在进入」 -->
      <div v-if="autoChecking" class="login__auto">
        <i class="el-icon-loading login__auto-icon" />
        <p class="login__auto-text">正在通过账号中心登录…</p>
        <el-button type="text" @click="stopAutoCheck">改用账号密码登录</el-button>
      </div>

      <template v-else>
        <div class="login__brand">
          <span class="login__brand-text">LifeAI 管理后台</span>
          <span class="login__brand-sub">仅限管理员账号</span>
        </div>

        <el-form
          ref="loginForm"
          :model="form"
          :rules="rules"
          label-position="top"
          @submit.native.prevent="onLogin"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="form.username"
              autocomplete="username"
              placeholder="管理员账号"
              prefix-icon="el-icon-user"
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              autocomplete="current-password"
              show-password
              placeholder="密码"
              prefix-icon="el-icon-lock"
              @keyup.enter.native="onLogin"
            />
          </el-form-item>

          <el-button
            type="primary"
            class="login__submit"
            :loading="submitting"
            @click="onLogin"
          >登 录</el-button>
        </el-form>

        <el-divider class="login__divider">或者</el-divider>

        <el-button
          class="login__sso"
          icon="el-icon-key"
          @click="onSsoLogin"
        >用账号中心登录</el-button>

        <p class="login__hint">
          这里没有注册入口。普通用户请去
          <a href="http://localhost:8080/login" class="login__link">LifeAI 主站</a>。
        </p>
      </template>
    </div>
  </div>
</template>

<script>
// 后台登录页故意不套 BasicLayout：它没有导航，也不需要用户信息。
//
// 与 life-web 的登录页相比少了两块：
//   1. 没有注册 tab —— 后台不该成为「随便注册个账号」的入口
//   2. 没有自己判断「是不是管理员」—— 登录接口对所有账号一视同仁，
//      是不是管理员由 /admin/users 这类接口返回 403 来判定（在 BasicLayout 里处理）
//
// 这里登录成功后**不检查角色**，只把令牌存下来就进 /users：
// 如果账号不是管理员，进去会看到 403 提示 + 顶部一个红色角色标签，
// 这比在登录页拦下更容易解释「你的账号权限不足」，也省一次前端猜测。
//
// 账号中心登录（第二按钮）走的是另一条路：整页跳 /api/v1/auth/sso/login?app=admin-web，
// Keycloak 那边验完人，后端签一个**和账号密码登录一模一样的本地 JWT**，
// 浏览器被送回 /sso/callback，由 views/sso/Callback.vue 落令牌。
// 所以本文件不碰令牌、不碰 axios —— 只负责把浏览器交出去。
//
// **进页面先静默问一次账号中心**（prompt=none）：
//   账号中心那边已经登着（比如刚在 life-web 登过）→ 不弹密码框、不弹红字，
//   直接换令牌进后台，用户感觉是「打开就进去了」。这就是单点登录的效果。
//   没登着 → 回 sso_error=sso_silent，安静地显示本地表单，不打扰任何人。
// 只试一次（sessionStorage 记着），否则「没登着 → 弹回 /login → 又试 → …」会死循环。
import {
  clearSilentTried,
  isQuietSsoError,
  markSilentTried,
  redirectToSsoLogin,
  silentAlreadyTried,
  ssoErrorText
} from '@/utils/sso'

export default {
  name: 'AdminLoginView',

  data() {
    return {
      submitting: false,
      // 正在等静默探测 / 正在换令牌，这段时间显示「正在进入…」而不是表单
      autoChecking: false,
      // 账号中心跳回来失败时带回的 next，优先于 query.redirect
      redirectFallback: '',
      form: {
        username: '',
        password: ''
      },
      rules: {
        username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
        password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
      }
    }
  },

  computed: {
    // 优先 query.redirect（守卫塞的），其次 next（账号中心失败跳回来带的），
    // 都没有就进用户管理。两个都必须是站内路径，防跳转钓鱼
    redirectTarget() {
      return this.pickTarget(this.$route.query.redirect) || this.pickTarget(this.redirectFallback) || '/users'
    }
  },

  created() {
    const { sso_error: ssoError, next } = this.$route.query
    if (typeof next === 'string' && this.pickTarget(next)) this.redirectFallback = next

    if (ssoError) {
      // 账号中心的静默探测已经试过了，这次显示表单等用户自己动手
      clearSilentTried()
      // 静默探测没登着、或后端没配 SSO，都不该弹红字
      if (!isQuietSsoError(ssoError)) this.$message.error(ssoErrorText(ssoError))
      return
    }

    // 刚点「用账号中心登录」回来失败（比如 state 过期）也走上面那条分支；
    // 只有「第一次打开这一页」才自动探测。
    if (!silentAlreadyTried()) {
      this.autoChecking = true
      markSilentTried()
      redirectToSsoLogin(this.redirectTarget, true)
    }
  },

  methods: {
    pickTarget(target) {
      if (!target || typeof target !== 'string') return ''
      if (!target.startsWith('/') || target.startsWith('//')) return ''
      return target
    },

    validate() {
      const form = this.$refs.loginForm
      if (!form) return Promise.resolve(false)
      return new Promise((resolve) => {
        form.validate((valid) => resolve(Boolean(valid)))
      })
    },

    async onLogin() {
      if (this.submitting) return
      const valid = await this.validate()
      if (!valid) return

      this.submitting = true
      try {
        await this.$store.dispatch('user/login', {
          username: this.form.username.trim(),
          password: this.form.password
        })
        this.$message.success('登录成功')
        this.$store.dispatch('user/fetchMe')
        await this.$router.replace(this.redirectTarget)
      } catch (error) {
        // 失败文案已由 request.js 统一弹出（后端原话）
      } finally {
        this.submitting = false
      }
    },

    // 整页跳走，不走 axios：Keycloak 要接管整个浏览器
    onSsoLogin() {
      redirectToSsoLogin(this.redirectTarget)
    },

    // 静默探测时用户等不及，点「改用账号密码登录」：停掉转圈直接给表单
    stopAutoCheck() {
      this.autoChecking = false
    }
  }
}
</script>

<style lang="scss" scoped>
.login {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #304156 0%, #1f2d3d 100%);

  &__card {
    width: 380px;
    padding: 28px 32px 20px;
    background: #fff;
    border-radius: 6px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.24);
  }

  &__brand {
    display: flex;
    flex-direction: column;
    align-items: center;
    margin-bottom: 18px;
  }

  &__brand-text {
    font-size: 20px;
    font-weight: 600;
    letter-spacing: 1px;
    color: #303133;
  }

  &__brand-sub {
    margin-top: 2px;
    font-size: 12px;
    color: #909399;
  }

  &__submit {
    width: 100%;
    margin-top: 4px;
  }

  &__divider {
    margin: 18px 0;

    ::v-deep .el-divider__text {
      background: #fff;
      color: #c0c4cc;
      font-size: 12px;
    }
  }

  &__sso {
    width: 100%;
  }

  &__auto {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 36px 0 32px;
  }

  &__auto-icon {
    font-size: 30px;
    color: #409eff;
  }

  &__auto-text {
    margin: 14px 0 6px;
    font-size: 14px;
    color: #606266;
  }

  &__hint {
    margin: 14px 0 0;
    font-size: 12px;
    color: #909399;
    text-align: center;
  }

  &__link {
    color: #409eff;
    text-decoration: none;
  }
}
</style>