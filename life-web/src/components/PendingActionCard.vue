<template>
  <div class="pa-card" :class="'pa-card--' + status">
    <header class="pa-card__head">
      <i class="pa-card__icon" :class="iconClass" />
      <span class="pa-card__title">{{ cardTitle }}</span>
      <span class="pa-card__badge">{{ statusText }}</span>
    </header>

    <dl class="pa-card__fields">
      <template v-for="field in shownFields">
        <dt :key="field.key + '-k'" class="pa-card__label">{{ field.label }}</dt>
        <dd :key="field.key + '-v'" class="pa-card__value">{{ field.value }}</dd>
      </template>
    </dl>

    <!--
      改账卡只说「哪一项从什么变成什么」。只列真的变了的字段 ——
      把整笔账重列一遍会让用户去找「到底改了哪儿」，而这正是他要核对的东西。
    -->
    <dl v-if="changedFields.length" class="pa-card__fields pa-card__fields--diff">
      <template v-for="row in changedFields">
        <dt :key="row.key + '-k'" class="pa-card__label">{{ row.label }}</dt>
        <dd :key="row.key + '-v'" class="pa-card__value">
          <span class="pa-card__old">{{ row.before }}</span>
          <i class="el-icon-right pa-card__arrow" />
          <span class="pa-card__new">{{ row.after }}</span>
        </dd>
      </template>
    </dl>

    <footer class="pa-card__foot">
      <!-- 只在还能操作时出现按钮：已确认 / 已取消 / 已过期的卡不该让人再点 -->
      <template v-if="actionable">
        <el-button
          :type="confirmButtonType"
          size="small"
          :loading="busy === 'confirm'"
          :disabled="busy !== ''"
          @click="onConfirm"
        >
          {{ confirmText }}
        </el-button>
        <el-button
          size="small"
          :loading="busy === 'cancel'"
          :disabled="busy !== ''"
          @click="onCancel"
        >
          不用了
        </el-button>
        <span class="pa-card__expire">{{ expireText }}</span>
      </template>

      <template v-else>
        <span class="pa-card__result">{{ resultText }}</span>
        <router-link
          v-if="createdLink"
          :to="createdLink"
          class="pa-card__link"
        >
          查看待办 ▸
        </router-link>
      </template>

      <span v-if="errorText" class="pa-card__error">{{ errorText }}</span>
    </footer>
  </div>
</template>

<script>
/**
 * 待确认卡片 —— 模型说「我帮你记一条待办」之后，拦在用户面前的那道闸。
 *
 * 它自己**不发请求**：点确认 / 取消只往上抛事件，由页面调接口并把新状态传回来。
 * 这样卡片只有「怎么显示」一件事，页面只有「什么时候改数据」一件事，
 * 两件事分开写，各自都好测（也方便以后在别处复用这张卡）。
 *
 * 四种后端状态对应四种长相，只有 pending 是可操作的：
 *   pending   蓝边，两个按钮
 *   confirmed 灰掉，「已创建」
 *   cancelled 灰掉，「已取消」
 *   expired   灰掉，「已过期」
 * 另外有一个前端独有的 busy（confirming / cancelling）：防连点，
 * 用户手快连按两下时第二次请求不该发出去 —— 后端会回 409，用户还以为是出错。
 */
export default {
  name: 'PendingActionCard',
  props: {
    // MessageOut.pending_action 的原样对象：
    // { id, action_type, status, expires_at, preview }
    action: {
      type: Object,
      required: true
    }
  },
  data() {
    return {
      // '' | 'confirm' | 'cancel'，只在本组件内有效，不写回 action
      busy: '',
      errorText: ''
    }
  },
  computed: {
    status() {
      return this.action.status || 'pending'
    },
    actionable() {
      return this.status === 'pending' && !this.busy
    },
    preview() {
      return this.action.preview || {}
    },
    cardTitle() {
      return {
        create_task: '新建待办',
        update_task: '修改待办',
        delete_task: '删除待办',
        create_expense: '记一笔支出',
        update_expense: '修改账目',
        delete_expense: '删除账目',
        add_shopping_item: '加入购物清单'
      }[this.action.action_type] || '待确认操作'
    },
    iconClass() {
      return {
        create_task: 'el-icon-document-add',
        update_task: 'el-icon-edit-outline',
        delete_task: 'el-icon-delete',
        create_expense: 'el-icon-money',
        update_expense: 'el-icon-edit-outline',
        delete_expense: 'el-icon-delete',
        add_shopping_item: 'el-icon-shopping-cart-2'
      }[this.action.action_type] || 'el-icon-warning-outline'
    },
    // 删除是不可逆的：按钮做成红色，文案说「删除」而不是「确认创建」。
    // 卡片上因此不能只写「确认」—— 用户点之前得知道这一下会发生什么。
    isDelete() {
      return (
        this.action.action_type === 'delete_task' ||
        this.action.action_type === 'delete_expense'
      )
    },
    // 账目类动作的文案与待办分开：账本里「创建」叫「记一笔」，改账不叫「创建」
    isExpense() {
      return (
        this.action.action_type === 'create_expense' ||
        this.action.action_type === 'update_expense' ||
        this.action.action_type === 'delete_expense'
      )
    },
    confirmButtonType() {
      return this.isDelete ? 'danger' : 'primary'
    },
    confirmText() {
      if (this.isDelete) return '删除'
      return this.action.action_type === 'update_task' ||
        this.action.action_type === 'update_expense'
        ? '确认修改'
        : '确认创建'
    },
    statusText() {
      const saved = this.isExpense ? '已记账' : '已创建'
      return {
        pending: '待确认',
        confirmed: this.isDelete ? '已删除' : saved,
        cancelled: '已取消',
        expired: '已过期',
        confirming: this.isDelete ? '删除中…' : '提交中…',
        cancelling: '处理中…'
      }[this.busy || this.status] || this.status
    },
    // 分类 id 为 0 表示未分类，此时不显示「分类：未分类」——
    // 后端返回的 category_name 也可能为空，两种都跳过
    fields() {
      const p = this.preview
      const out = []
      if (p.title) out.push({ key: 'title', label: '标题', value: p.title })
      if (p.due_at_text) out.push({ key: 'due', label: '时间', value: p.due_at_text })
      if (p.priority_text) {
        out.push({ key: 'priority', label: '优先级', value: p.priority_text })
      }
      if (p.category_name) {
        out.push({ key: 'category', label: '分类', value: p.category_name })
      }
      return out
    },
    // 账目的字段集。金额/收支方向用后端算好的 *_text，
    // 时间用 occurred_at_text（后端把 UTC 转成北京时间并补了星期）。
    expenseFields(source) {
      const out = []
      if (source.amount_text) out.push({ key: 'amount', label: '金额', value: source.amount_text })
      if (source.type_text) out.push({ key: 'type', label: '类型', value: source.type_text })
      if (source.occurred_at_text) {
        out.push({ key: 'occurred', label: '时间', value: source.occurred_at_text })
      }
      if (source.account) out.push({ key: 'account', label: '支付方式', value: source.account })
      // 分类名只有在顶层 payload 里才有（before 快照里存的是 id）
      if (source.category_name) {
        out.push({ key: 'category', label: '分类', value: source.category_name })
      }
      if (source.note) out.push({ key: 'note', label: '备注', value: source.note })
      return out
    },
    // 删除卡显示的是「要删的那条长什么样」（后端 payload.gone 快照）。
    // 不能只显示理由：「你说删买菜」和「真的要删的是买菜」是两件事，
    // 模型抄错 id 时，这一栏会露出另一条待办的名字 —— 那是用户唯一的逃生机会。
    deleteFields() {
      const gone = this.preview.gone || {}
      const out = []
      if (this.isExpense) {
        out.push(...this.expenseFields(gone))
      } else {
        if (gone.title) out.push({ key: 'gone-title', label: '要删除', value: gone.title })
        if (gone.due_at_text) {
          out.push({ key: 'gone-due', label: '时间', value: gone.due_at_text })
        }
        if (gone.priority_text) {
          out.push({ key: 'gone-priority', label: '优先级', value: gone.priority_text })
        }
      }
      if (this.preview.reason) {
        out.push({ key: 'reason', label: '原因', value: this.preview.reason })
      }
      return out
    },
    // 改账卡的核心：只列**真的变了**的那几项，每项一行「改前 → 改后」。
    // 后端把「哪些字段变了」算好放在 preview.changed 里，
    // 前端不重算 —— 规则散两处早晚会出现「卡片说没变、库里变了」。
    changedFields() {
      const p = this.preview
      const before = p.before || {}
      const after = {}
      if (p.amount_text) after.amount = p.amount_text
      if (p.type_text) after.type = p.type_text
      if (p.occurred_at_text) after.occurred_at = p.occurred_at_text
      if (p.account) after.account = p.account
      if (p.category_id !== undefined) after.category_id = p.category_id
      if (p.note !== undefined) after.note = p.note

      const labels = {
        amount: '金额',
        type: '类型',
        occurred_at: '时间',
        account: '支付方式',
        category_id: '分类',
        note: '备注'
      }
      const shown = (value) => (value === null || value === undefined || value === '' ? '—' : value)
      const changed = Array.isArray(p.changed) ? p.changed : []
      return changed.map((key) => {
        let beforeValue = before[key]
        if (key === 'occurred_at') beforeValue = before.occurred_at_text
        if (key === 'amount') beforeValue = before.amount_text
        if (key === 'type') beforeValue = before.type_text
        if (key === 'category_id') beforeValue = before.category_name
        return {
          key: 'chg-' + key,
          label: labels[key] || key,
          before: shown(beforeValue),
          after: shown(after[key])
        }
      })
    },
    shownFields() {
      if (this.isDelete) return this.deleteFields
      if (this.action.action_type === 'update_expense') return []
      if (this.isExpense) return this.expenseFields(this.preview)
      return this.fields
    },
    expireText() {
      const raw = this.action.expires_at
      if (!raw) return ''
      // 后端给的是 +08:00 的 ISO 串，只截到分钟显示，不做时区换算
      return `${String(raw).slice(0, 10)} ${String(raw).slice(11, 16)} 前有效`
    },
    resultText() {
      if (this.isDelete) {
        const what = this.isExpense ? '这笔账' : '这条待办'
        return {
          confirmed: `已删除，${what}不会再出现在列表里`,
          cancelled: `已取消，${what}还在`,
          expired: '超过 30 分钟未处理，已失效'
        }[this.status] || ''
      }
      if (this.action.action_type === 'update_expense') {
        return {
          confirmed: '已按这张卡改好',
          cancelled: '已取消，账目没有变动',
          expired: '超过 30 分钟未处理，已失效'
        }[this.status] || ''
      }
      return {
        confirmed: this.isExpense ? '已按这张卡记好' : '已按这张卡创建完成',
        cancelled: '已取消，没有写入任何数据',
        expired: '超过 30 分钟未处理，已失效'
      }[this.status] || ''
    },
    // V1.0 的待办只有列表页（没有 /tasks/:id 详情路由），所以链到列表。
    // `created_id` 由页面在确认成功后回填（接口只在确认那一次返回它）。
    // 删除卡不显示这个链接：那条待办刚被删掉，点进去只会看到「列表里没有它」。
    createdLink() {
      if (this.isDelete) return null
      if (this.action.action_type === 'update_expense') {
        return this.status === 'confirmed' ? '/expenses' : null
      }
      return this.status === 'confirmed' && this.action.created_id ? '/tasks' : null
    }
  },
  methods: {
    onConfirm() {
      if (!this.actionable) return
      this.errorText = ''
      this.busy = 'confirm'
      this.$emit('confirm', this.action)
    },
    onCancel() {
      if (!this.actionable) return
      this.errorText = ''
      this.busy = 'cancel'
      this.$emit('cancel', this.action)
    },
    // 页面在请求回来后调它：失败要把按钮放出来（否则用户永远卡在「提交中」）
    finish(errorText) {
      this.busy = ''
      this.errorText = errorText || ''
    }
  }
}
</script>

<style scoped lang="scss">
.pa-card {
  max-width: 72%;
  border: 1px solid #c6e2ff;
  border-radius: 8px;
  background: #fbfdff;
  padding: 12px 14px;

  // 已处理 / 已过期的卡：整张灰掉，但仍然留在对话里当记录
  &--confirmed,
  &--cancelled,
  &--expired {
    border-color: #e4e7ed;
    background: #fafafa;
    color: #909399;
  }
}

.pa-card__head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}

.pa-card__icon {
  font-size: 15px;
  color: #409eff;

  .pa-card--confirmed &,
  .pa-card--cancelled &,
  .pa-card--expired & {
    color: #c0c4cc;
  }
}

.pa-card__title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;

  .pa-card--confirmed &,
  .pa-card--cancelled &,
  .pa-card--expired & {
    color: #909399;
  }
}

.pa-card__badge {
  margin-left: auto;
  padding: 0 6px;
  height: 18px;
  line-height: 18px;
  font-size: 11px;
  color: #409eff;
  background: #ecf5ff;
  border-radius: 3px;

  .pa-card--confirmed & {
    color: #67c23a;
    background: #f0f9eb;
  }

  .pa-card--cancelled &,
  .pa-card--expired & {
    color: #909399;
    background: #f4f4f5;
  }
}

.pa-card__fields {
  display: grid;
  grid-template-columns: 52px 1fr;
  gap: 4px 8px;
  margin: 0 0 10px;
  font-size: 13px;
}

.pa-card__label {
  margin: 0;
  color: #909399;
}

.pa-card__value {
  margin: 0;
  color: #303133;
  word-break: break-all;

  .pa-card--confirmed &,
  .pa-card--cancelled &,
  .pa-card--expired & {
    color: #909399;
  }
}

// 「改前 → 改后」：旧值划线、新值加粗。
// 用颜色区分是不行的 —— 色弱用户分不出这两个色块，加上删除线和字重才稳。
.pa-card__old {
  color: #909399;
  text-decoration: line-through;
}

.pa-card__arrow {
  margin: 0 6px;
  color: #c0c4cc;
}

.pa-card__new {
  color: #303133;
  font-weight: 600;

  .pa-card--confirmed &,
  .pa-card--cancelled &,
  .pa-card--expired & {
    color: #909399;
  }
}

.pa-card__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.pa-card__expire {
  font-size: 11px;
  color: #c0c4cc;
}

.pa-card__result {
  font-size: 12px;
  color: #909399;
}

.pa-card__link {
  font-size: 12px;
  color: #409eff;
  text-decoration: none;

  &:hover {
    text-decoration: underline;
  }
}

.pa-card__error {
  font-size: 12px;
  color: #f56c6c;
}
</style>