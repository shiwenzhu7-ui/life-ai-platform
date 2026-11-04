<template>
  <el-dialog
    :visible="visible"
    :title="title"
    width="520px"
    :close-on-click-modal="false"
    :show-close="true"
    @close="handleClose"
  >
    <div class="expense-form">
      <!-- 类型：切到收入时，下面的分类九宫格整体换成 income 域 -->
      <el-radio-group v-model="form.type" class="expense-form__type" @change="handleTypeChange">
        <el-radio-button label="expense">支出</el-radio-button>
        <el-radio-button label="income">收入</el-radio-button>
      </el-radio-group>

      <div class="expense-form__row">
        <label class="expense-form__label">金额<span class="expense-form__req">*</span></label>
        <!-- 用字符串输入，不用 el-input-number：number 会引入浮点误差 -->
        <el-input
          v-model="form.amount"
          class="expense-form__amount"
          placeholder="0.00"
          inputmode="decimal"
          clearable
        >
          <template slot="prepend">¥</template>
        </el-input>
      </div>

      <div class="expense-form__row expense-form__row--block">
        <label class="expense-form__label">分类<span class="expense-form__req">*</span></label>
        <div class="expense-form__grid">
          <button
            v-for="item in categories"
            :key="item.id"
            type="button"
            :class="['expense-form__cat', { 'is-active': form.category_id === item.id }]"
            @click="form.category_id = item.id"
          >
            {{ item.name }}
          </button>
        </div>
        <p v-if="!categories.length" class="expense-form__hint">分类还没加载出来，稍等一下</p>
      </div>

      <div class="expense-form__row expense-form__row--inline">
        <div class="expense-form__field">
          <label class="expense-form__label">账户</label>
          <el-select
            v-model="form.account"
            class="expense-form__control"
            filterable
            allow-create
            default-first-option
            placeholder="现金"
          >
            <el-option v-for="item in accountOptions" :key="item" :label="item" :value="item" />
          </el-select>
        </div>

        <div class="expense-form__field">
          <label class="expense-form__label">时间<span class="expense-form__req">*</span></label>
          <el-date-picker
            v-model="form.occurred_at"
            class="expense-form__control"
            type="datetime"
            placeholder="发生时间"
            :picker-options="pickerOptions"
          />
        </div>
      </div>

      <div class="expense-form__row">
        <label class="expense-form__label">备注</label>
        <el-input
          v-model="form.note"
          maxlength="255"
          show-word-limit
          placeholder="可选"
        />
      </div>
    </div>

    <div slot="footer">
      <el-button size="small" @click="handleClose">取消</el-button>
      <el-button type="primary" size="small" :loading="saving" @click="submit">保存</el-button>
    </div>
  </el-dialog>
</template>

<script>
import { createExpense, getExpense, updateExpense } from '@/api/expenses'
import { toApiDateTime } from '@/utils/format'

const ACCOUNT_OPTIONS = ['现金', '支付宝', '微信', '银行卡']

// 后端金额上限（Decimal(12,2)）：前端也拦一道，省一次 422
const MAX_AMOUNT = 9999999.99
const AMOUNT_PATTERN = /^\d+(\.\d{1,2})?$/

// "1280.5" 和 "1280.50" 是同一个金额，比较"改没改"之前必须先归一，
// 否则打开编辑框什么都不动，也会被判成金额变了
function normalizeAmount(value) {
  const text = String(value || '').trim()
  if (!AMOUNT_PATTERN.test(text)) return text
  const [int, dec = ''] = text.split('.')
  return `${Number(int)}.${dec.padEnd(2, '0')}`
}

// 新建时每次都要一份新对象；时间默认「现在」
const emptyForm = () => ({
  type: 'expense',
  amount: '',
  category_id: null,
  account: '现金',
  occurred_at: new Date(),
  note: ''
})

export default {
  name: 'ExpenseFormDialog',
  props: {
    visible: { type: Boolean, default: false },
    // null = 新建；数字 = 编辑这一条
    expenseId: { type: Number, default: null }
  },
  data() {
    return {
      form: emptyForm(),
      saving: false,
      // 打开时的快照，用来算「改了哪些字段」
      snapshot: null,
      accountOptions: ACCOUNT_OPTIONS,
      // 后端只接受「不晚于现在 1 天」，所以后天及以后直接禁选
      pickerOptions: {
        disabledDate(time) {
          return time.getTime() > Date.now() + 24 * 60 * 60 * 1000
        }
      }
    }
  },
  computed: {
    isEdit() {
      return this.expenseId !== null && this.expenseId !== undefined
    },
    title() {
      const prefix = this.isEdit ? '编辑' : '记一笔'
      return prefix + (this.form.type === 'income' ? '收入' : '支出')
    },
    // 分类数据源跟着类型走：expense 域 9 个 / income 域 5 个
    categories() {
      return this.$store.getters['categories/listOf'](this.form.type)
    }
  },
  watch: {
    // 只有打开时才初始化，避免隐藏状态下残留上一次的输入
    visible(value) {
      if (value) this.init()
    }
  },
  methods: {
    init() {
      this.form = emptyForm()
      this.snapshot = null
      if (this.isEdit) {
        this.loadExpense()
      } else {
        // 新建：空表单本身就是「改动前」的状态
        this.snapshot = this.serialize()
      }
    },

    // 编辑：先拉最新的（列表里那行可能是几分钟前取的），再回填
    async loadExpense() {
      try {
        const expense = await getExpense(this.expenseId)
        this.form = {
          type: expense.type,
          amount: expense.amount, // 后端返回的就是两位小数字符串
          category_id: expense.category_id,
          account: expense.account || '现金',
          occurred_at: new Date(expense.occurred_at),
          note: expense.note || ''
        }
        this.snapshot = this.serialize()
      } catch (e) {
        // 这条已被删 / 不属于你 → 404。提示已由 request.js 弹过，
        // 这里关掉弹窗并让父组件刷新，别留一张空表单在屏幕上
        this.$emit('update:visible', false)
        this.$emit('failed')
      }
    },

    // 把表单压成「可比较的快照」：Date → 毫秒、空串 → null、金额 → 两位小数
    serialize() {
      return {
        type: this.form.type,
        amount: normalizeAmount(this.form.amount),
        category_id: this.form.category_id,
        account: (this.form.account || '').trim() || '现金',
        occurred_at: this.form.occurred_at ? this.form.occurred_at.getTime() : null,
        note: this.form.note.trim() || null
      }
    },

    // 只挑与快照不同的字段：只改备注时，请求体就只有 {"note": "..."}
    buildDiff() {
      const current = this.serialize()
      const diff = {}
      Object.keys(current).forEach((key) => {
        if (current[key] !== this.snapshot[key]) diff[key] = current[key]
      })
      return diff
    },

    // 换类型必须清空分类：两个域的分类 id 不通用，留着就会被后端判成「分类不存在」
    handleTypeChange() {
      this.form.category_id = null
    },

    validate() {
      const amount = this.form.amount.trim()
      if (!amount) return '请填写金额'
      if (!AMOUNT_PATTERN.test(amount)) return '金额最多两位小数'
      const value = Number(amount)
      if (value <= 0) return '金额必须大于 0'
      if (value > MAX_AMOUNT) return '金额不能超过 9999999.99'
      if (!this.form.category_id) return '请选择分类'
      if (!this.form.occurred_at) return '请选择时间'
      return ''
    },

    async submit() {
      const error = this.validate()
      if (error) {
        this.$message.error(error)
        return
      }

      this.saving = true
      try {
        if (this.isEdit) {
          const diff = this.buildDiff()
          if (!Object.keys(diff).length) {
            this.$message.info('没有改动')
            return
          }
          // 快照里时间是毫秒（为了能比较），发给后端要转回 +08:00 字符串
          if ('occurred_at' in diff) {
            diff.occurred_at = toApiDateTime(new Date(diff.occurred_at))
          }
          await updateExpense(this.expenseId, diff)
          this.$message.success('已保存')
        } else {
          const payload = this.serialize()
          payload.occurred_at = toApiDateTime(this.form.occurred_at)
          await createExpense(payload)
          this.$message.success('已记一笔')
        }
        this.$emit('saved')
        this.$emit('update:visible', false)
      } catch (e) {
        // 失败（422/404/断网）：提示已由 request.js 统一弹过，
        // 这里什么都不做——弹窗和已填的内容都要留着，别让用户重填
      } finally {
        this.saving = false
      }
    },

    handleClose() {
      this.$emit('update:visible', false)
    }
  }
}
</script>

<style lang="scss" scoped>
.expense-form {
  &__type {
    display: flex;
    margin-bottom: 16px;
  }

  &__row {
    display: flex;
    align-items: center;
    margin-bottom: 14px;

    &--block {
      display: block;
    }

    &--inline {
      gap: 16px;
    }
  }

  &__field {
    flex: 1;
  }

  &__label {
    display: block;
    width: 56px;
    flex: 0 0 56px;
    font-size: 13px;
    color: #606266;
    margin-bottom: 4px;
  }

  &__req {
    color: #f56c6c;
    margin-left: 2px;
  }

  &__amount {
    flex: 1;

    ::v-deep input {
      height: 44px;
      font-size: 24px;
      font-variant-numeric: tabular-nums;
    }
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }

  &__cat {
    height: 44px;
    border: 1px solid #dcdfe6;
    border-radius: 4px;
    background: #fff;
    font-size: 13px;
    color: #606266;
    cursor: pointer;
    transition: all 0.15s;

    &:hover {
      border-color: #409eff;
      color: #409eff;
    }

    &.is-active {
      border-color: #409eff;
      background: #ecf5ff;
      color: #409eff;
      font-weight: 600;
    }
  }

  &__control {
    width: 100%;
  }

  &__hint {
    margin: 8px 0 0;
    font-size: 12px;
    color: #909399;
  }
}
</style>
