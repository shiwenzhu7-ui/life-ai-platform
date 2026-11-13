<template>
  <div class="login">
    <div class="login__card">
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

      <p class="login__hint">
        这里没有注册入口。普通用户请去
        <a href="http://localhost:8080/login" class="login__link">LifeAI 主站</a>。
      </p>
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

export default {
  name: 'AdminLoginView',

  data() {
    return {
      submitting: false,
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
    // 优先 query.redirect，没有就进用户管理。必须是站内路径，防跳转钓鱼
    redirectTarget() {
      const target = this.$route.query.redirect
      if (!target || typeof target !== 'string') return '/users'
      if (!target.startsWith('/') || target.startsWith('//')) return '/users'
      return target
    }
  },

  methods: {
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