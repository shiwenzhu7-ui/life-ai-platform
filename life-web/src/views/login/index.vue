<template>
  <div class="login">
    <div class="login__card">
      <div class="login__brand">
        <span class="login__brand-text">LifeAI</span>
        <span class="login__brand-sub">智能生活管理</span>
      </div>

      <el-tabs v-model="tab" class="login__tabs" :stretch="true">
        <!-- ---------------- 登录 ---------------- -->
        <el-tab-pane label="登录" name="login">
          <el-form
            ref="loginForm"
            :model="loginForm"
            :rules="loginRules"
            label-position="top"
            @submit.native.prevent="onLogin"
          >
            <el-form-item label="用户名" prop="username">
              <el-input
                v-model="loginForm.username"
                autocomplete="username"
                placeholder="注册时填的用户名"
                prefix-icon="el-icon-user"
              />
            </el-form-item>

            <el-form-item label="密码" prop="password">
              <el-input
                v-model="loginForm.password"
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
            还没有账号？<el-link type="primary" @click="tab = 'register'">去注册</el-link>
          </p>
        </el-tab-pane>

        <!-- ---------------- 注册 ---------------- -->
        <el-tab-pane label="注册" name="register">
          <el-form
            ref="registerForm"
            :model="registerForm"
            :rules="registerRules"
            label-position="top"
            @submit.native.prevent="onRegister"
          >
            <el-form-item label="用户名" prop="username">
              <el-input
                v-model="registerForm.username"
                autocomplete="username"
                placeholder="3~50 位字母、数字、下划线"
                prefix-icon="el-icon-user"
              />
            </el-form-item>

            <el-form-item label="邮箱（可选）" prop="email">
              <!-- 标了可选就必须写清楚，且校验也必须是「空着也算通过」，
                   否则用户会被一个自己没打算填的字段卡住 -->
              <el-input
                v-model="registerForm.email"
                autocomplete="email"
                placeholder="不填也行"
                prefix-icon="el-icon-message"
              />
            </el-form-item>

            <el-form-item label="密码" prop="password">
              <el-input
                v-model="registerForm.password"
                type="password"
                autocomplete="new-password"
                show-password
                placeholder="至少 8 位，最多 72 字节（约 24 个汉字）"
                prefix-icon="el-icon-lock"
              />
            </el-form-item>

            <el-form-item label="确认密码" prop="password2">
              <el-input
                v-model="registerForm.password2"
                type="password"
                autocomplete="new-password"
                show-password
                placeholder="再输一次"
                prefix-icon="el-icon-lock"
                @keyup.enter.native="onRegister"
              />
            </el-form-item>

            <el-button
              type="primary"
              class="login__submit"
              :loading="submitting"
              @click="onRegister"
            >注 册</el-button>
          </el-form>

          <p class="login__hint">
            已有账号？<el-link type="primary" @click="tab = 'login'">去登录</el-link>
          </p>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script>
// 登录页故意不套 BasicLayout：它没有侧边导航，也不需要顶部用户信息。
//
// 两处「不与后端争抢提示」的约定：
//   1. 请求出错后不在页面里再弹一条红条 —— request.js 已经弹过了。
//      页面只在**表单校验**（前端自己拦下的）时用 el-form 的行内红字提示。
//   2. 前端把明显不合法的输入先拦下来，省一次注定 422 的往返；
//      但「用户名是否重复」「密码是否 72 字节以内」仍然交给后端判定，
//      前端不复制后端的规则 —— 复制了就会漂移。

// 与后端 schema.py 的 _USERNAME_RE 一致：出错时看到的规则和真正执行的规则要一样。
const USERNAME_RE = /^[A-Za-z0-9_]{3,50}$/
// bcrypt 只认 72 字节，代码里按 UTF-8 字节算（24 个汉字 = 72 字节）
const PASSWORD_MAX_BYTES = 72

function byteLength(str) {
  return new TextEncoder().encode(str).length
}

export default {
  name: 'LoginView',

  data() {
    const checkPassword2 = (rule, value, callback) => {
      if (!value) return callback(new Error('请再输入一次密码'))
      if (value !== this.registerForm.password) {
        return callback(new Error('两次输入的密码不一致'))
      }
      callback()
    }

    return {
      tab: 'login',
      submitting: false,

      loginForm: {
        username: '',
        password: ''
      },
      loginRules: {
        username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
        password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
      },

      registerForm: {
        username: '',
        email: '',
        password: '',
        password2: ''
      },
      registerRules: {
        username: [
          { required: true, message: '请输入用户名', trigger: 'blur' },
          {
            validator: (rule, value, callback) => {
              if (!USERNAME_RE.test(value || '')) {
                return callback(new Error('用户名只能包含字母、数字、下划线，长度 3~50'))
              }
              callback()
            },
            trigger: 'blur'
          }
        ],
        email: [
          {
            // 空着算通过；填了才校验形状。后端也是这么处理的（空串归一成 null）
            validator: (rule, value, callback) => {
              const v = (value || '').trim()
              if (!v) return callback()
              if (!v.includes('@') || v.startsWith('@') || v.endsWith('@')) {
                return callback(new Error('邮箱格式不正确'))
              }
              callback()
            },
            trigger: 'blur'
          }
        ],
        password: [
          { required: true, message: '请输入密码', trigger: 'blur' },
          {
            validator: (rule, value, callback) => {
              const v = value || ''
              if (v.length < 8) return callback(new Error('密码至少 8 位'))
              if (byteLength(v) > PASSWORD_MAX_BYTES) {
                return callback(new Error('密码过长：最多 72 字节（约 24 个汉字）'))
              }
              callback()
            },
            trigger: 'blur'
          }
        ],
        password2: [{ validator: checkPassword2, trigger: 'blur' }]
      }
    }
  },

  computed: {
    // 登录成功后回哪去：优先 query.redirect（路由守卫塞进来的原地址），
    // 没有就回首页。必须是站内路径，防「?redirect=https://evil.com」这种跳转钓鱼。
    redirectTarget() {
      const target = this.$route.query.redirect
      if (!target || typeof target !== 'string') return '/'
      if (!target.startsWith('/') || target.startsWith('//')) return '/'
      return target
    }
  },

  created() {
    // 带着 ?tab=register 进来时直接落在注册页
    if (this.$route.query.tab === 'register') this.tab = 'register'
  },

  methods: {
    validate(formRef) {
      const form = this.$refs[formRef]
      if (!form) return Promise.resolve(false)
      return new Promise((resolve) => {
        form.validate((valid) => resolve(Boolean(valid)))
      })
    },

    async onLogin() {
      if (this.submitting) return
      const valid = await this.validate('loginForm')
      if (!valid) return

      this.submitting = true
      try {
        await this.$store.dispatch('user/login', {
          username: this.loginForm.username.trim(),
          password: this.loginForm.password
        })
        this.$message.success('登录成功')
        // fetchMe 拉额度信息；不等它 —— 失败也不影响进页面
        this.$store.dispatch('user/fetchMe')
        await this.$router.replace(this.redirectTarget)
      } catch (error) {
        // 失败文案已由 request.js 统一弹出（后端原话「用户名或密码错误」等）
      } finally {
        this.submitting = false
      }
    },

    async onRegister() {
      if (this.submitting) return
      const valid = await this.validate('registerForm')
      if (!valid) return

      const username = this.registerForm.username.trim()
      const email = this.registerForm.email.trim()

      this.submitting = true
      try {
        await this.$store.dispatch('user/register', {
          username,
          // 空串不发：后端会把空串归一成 null，但「不发这个字段」更干净，
          // F12 里看请求体也更容易判断到底填没填邮箱
          ...(email ? { email } : {}),
          password: this.registerForm.password
        })

        // 注册接口不返回令牌（后端设计如此），所以紧接着自动登录一次。
        // 自动登录失败（理论上不该发生）也不能把用户卡住：提示他去登录页。
        try {
          await this.$store.dispatch('user/login', {
            username,
            password: this.registerForm.password
          })
          this.$message.success('注册成功，已自动登录')
          this.$store.dispatch('user/fetchMe')
          await this.$router.replace(this.redirectTarget)
        } catch (loginError) {
          this.$message.warning('注册成功，请手动登录')
          this.loginForm.username = username
          this.loginForm.password = ''
          this.tab = 'login'
        }
      } catch (error) {
        // 失败文案已由 request.js 统一弹出（「用户名已被占用」「邮箱已被注册」等）
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
    margin-bottom: 8px;
  }

  &__brand-text {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: 1px;
    color: #303133;
  }

  &__brand-sub {
    font-size: 12px;
    color: #909399;
  }

  &__tabs {
    margin-top: 4px;
  }

  &__submit {
    width: 100%;
    margin-top: 4px;
  }

  &__hint {
    margin: 14px 0 0;
    font-size: 13px;
    color: #909399;
    text-align: center;
  }
}
</style>