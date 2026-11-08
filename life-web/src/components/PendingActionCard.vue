<template>
  <div class="pa-card" :class="'pa-card--' + status">
    <header class="pa-card__head">
      <i class="pa-card__icon" :class="iconClass" />
      <span class="pa-card__title">{{ cardTitle }}</span>
      <span class="pa-card__badge">{{ statusText }}</span>
    </header>

    <dl class="pa-card__fields">
      <template v-for="field in fields">
        <dt :key="field.key + '-k'" class="pa-card__label">{{ field.label }}</dt>
        <dd :key="field.key + '-v'" class="pa-card__value">{{ field.value }}</dd>
      </template>
    </dl>

    <footer class="pa-card__foot">
      <!-- 只在还能操作时出现按钮：已确认 / 已取消 / 已过期的卡不该让人再点 -->
      <template v-if="actionable">
        <el-button
          type="primary"
          size="small"
          :loading="busy === 'confirm'"
          :disabled="busy !== ''"
          @click="onConfirm"
        >
          确认创建
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
        create_expense: '记一笔支出',
        add_shopping_item: '加入购物清单'
      }[this.action.action_type] || '待确认操作'
    },
    iconClass() {
      return {
        create_task: 'el-icon-document-add',
        create_expense: 'el-icon-money',
        add_shopping_item: 'el-icon-shopping-cart-2'
      }[this.action.action_type] || 'el-icon-warning-outline'
    },
    statusText() {
      return {
        pending: '待确认',
        confirmed: '已创建',
        cancelled: '已取消',
        expired: '已过期',
        confirming: '提交中…',
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
    expireText() {
      const raw = this.action.expires_at
      if (!raw) return ''
      // 后端给的是 +08:00 的 ISO 串，只截到分钟显示，不做时区换算
      return `${String(raw).slice(0, 10)} ${String(raw).slice(11, 16)} 前有效`
    },
    resultText() {
      return {
        confirmed: '已按这张卡创建完成',
        cancelled: '已取消，没有写入任何数据',
        expired: '超过 30 分钟未处理，已失效'
      }[this.status] || ''
    },
    // V1.0 的待办只有列表页（没有 /tasks/:id 详情路由），所以链到列表。
    // `created_id` 由页面在确认成功后回填（接口只在确认那一次返回它）。
    createdLink() {
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