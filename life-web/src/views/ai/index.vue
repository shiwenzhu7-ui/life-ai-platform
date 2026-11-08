<template>
  <div class="ai">
    <!-- 左栏：会话列表 -->
    <aside class="ai__aside">
      <el-button
        class="ai__new"
        icon="el-icon-plus"
        size="small"
        :loading="creating"
        @click="handleCreate"
      >
        新建对话
      </el-button>

      <div v-loading="loadingSessions" class="ai__sessions">
        <div
          v-for="item in sessions"
          :key="item.id"
          class="ai__session"
          :class="{ 'ai__session--active': item.id === activeId }"
          @click="selectSession(item.id)"
        >
          <span class="ai__session-title">{{ item.title }}</span>
          <span class="ai__session-time">{{ formatSessionTime(item) }}</span>
        </div>
        <p v-if="!loadingSessions && !sessions.length" class="ai__placeholder">
          还没有对话
        </p>
      </div>
    </aside>

    <!-- 右栏：消息流 -->
    <section class="ai__main">
      <template v-if="activeId">
        <header class="ai__header">
          <span class="ai__title">{{ activeSession ? activeSession.title : '' }}</span>
        </header>

        <div ref="messagesEl" v-loading="loadingMessages" class="ai__messages">
          <div
            v-for="item in messages"
            :key="item.id"
            class="ai__row"
            :class="'ai__row--' + item.role"
          >
            <div
              v-if="item.content || !item.pending_action"
              class="ai__bubble"
              :class="{ 'ai__bubble--failed': item.status === 'failed' }"
            >
              {{ item.content || '（无内容）' }}
            </div>
            <!-- 卡片挂在产生它的那条助手消息下面，而不是另起一行：
                 用户看到的是「AI 说了一句话 + 一张要他点的卡」这一件事 -->
            <PendingActionCard
              v-if="item.pending_action"
              ref="cards"
              :action="item.pending_action"
              @confirm="handleConfirm"
              @cancel="handleCancel"
            />
            <div v-if="item.status === 'failed'" class="ai__retry">
              <span class="ai__retry-text">
                {{ item.role === 'user' ? '没发出去' : '这条回复失败了' }}
              </span>
              <el-button
                v-if="item.role === 'user'"
                type="text"
                size="mini"
                @click="retry(item)"
              >
                重试
              </el-button>
            </div>
            <span class="ai__time">{{ formatDateTime(item.created_at) }}</span>
          </div>

          <p v-if="!loadingMessages && !messages.length" class="ai__hint">
            这个对话还没有消息
          </p>
        </div>

        <footer class="ai__composer">
          <p v-if="errorText" class="ai__error">{{ errorText }}</p>
          <el-input
            ref="composer"
            v-model="input"
            type="textarea"
            :rows="2"
            resize="none"
            :disabled="sending"
            placeholder="说点什么，Enter 发送，Shift+Enter 换行"
            @keydown.native.enter.exact="handleEnter"
          />
          <div class="ai__composer-bar">
            <span class="ai__composer-hint">{{ sending ? 'AI 正在思考…' : '' }}</span>
            <el-button
              type="primary"
              size="small"
              :loading="sending"
              :disabled="!input.trim()"
              @click="handleSend"
            >
              发送
            </el-button>
          </div>
        </footer>
      </template>

      <div v-else class="ai__empty">
        <i class="el-icon-chat-dot-round ai__empty-icon" />
        <p class="ai__empty-title">AI 生活助手</p>
        <p class="ai__empty-hint">在左侧选择一个对话，或点「新建对话」开始</p>
      </div>
    </section>
  </div>
</template>

<script>
import PendingActionCard from '@/components/PendingActionCard.vue'
import {
  cancelAction,
  chat,
  confirmAction,
  createSession,
  listMessages,
  listSessions
} from '@/api/ai'
import { formatDateTime as fmtDateTime } from '@/utils/format'

export default {
  name: 'AiView',
  components: { PendingActionCard },
  data() {
    return {
      sessions: [],
      activeId: null,
      messages: [],
      input: '',
      loadingSessions: false,
      loadingMessages: false,
      creating: false,
      sending: false,
      errorText: ''
    }
  },
  computed: {
    activeSession() {
      return this.sessions.find((item) => item.id === this.activeId) || null
    }
  },
  created() {
    this.loadSessions()
  },
  // 「确认/取消失败」要显示在卡片自己身上（而不是页面顶部），而卡片是子组件，
  // 父组件只能通过 ref 调它的 finish() —— 这是 Vue 2 里「子组件表现自己、
  // 父组件决定数据」的一种折中写法。409/410 时不用调：紧接着的 loadMessages
  // 会用新数据整体替换消息数组，这张卡连着它的组件一起被销毁。
  methods: {
    // 左栏列表；第一次进入时自动选中第一条，省得页面空着等人的
    async loadSessions() {
      this.loadingSessions = true
      try {
        const page = await listSessions({ page: 1, size: 50 })
        this.sessions = page.items || []
        if (!this.activeId && this.sessions.length) {
          this.selectSession(this.sessions[0].id)
        }
      } finally {
        this.loadingSessions = false
      }
    },

    // order=desc 拿最近 50 条，界面上要按「旧 → 新」显示，所以反转
    async loadMessages(sessionId) {
      this.loadingMessages = true
      try {
        const page = await listMessages(sessionId, {
          page: 1,
          size: 50,
          order: 'desc'
        })
        this.messages = (page.items || []).slice().reverse()
        this.scrollToBottom()
      } catch (err) {
        // 转圈必须停，还得告诉人为什么停了 —— 否则用户只看到永远转的圈
        this.errorText = err.message || '消息加载失败'
      } finally {
        this.loadingMessages = false
      }
    },

    // 只刷新左栏（标题/时间/条数），不动当前选中和消息
    async refreshSessions() {
      try {
        const page = await listSessions({ page: 1, size: 50 }, { silent: true })
        this.sessions = page.items || []
      } catch (e) {
        // 侧栏刷新失败不该盖住主流程的错误提示，静默即可
      }
    },

    selectSession(sessionId) {
      if (this.activeId === sessionId) return
      // 发送中切走会让「正在思考」的结果落到别的会话里，先挡住
      if (this.sending) return
      this.activeId = sessionId
      this.messages = []
      this.errorText = ''
      this.loadMessages(sessionId)
      this.focusComposer()
    },

    // 先建空会话再选中：左栏立刻多一行，用户知道点到了
    async handleCreate() {
      if (this.sending) return
      this.creating = true
      try {
        const session = await createSession({})
        this.sessions.unshift(session)
        this.activeId = session.id
        this.messages = []
        this.errorText = ''
        this.focusComposer()
      } finally {
        this.creating = false
      }
    },

    // 回车发送，但中文输入法选词时的回车不能算发送（isComposing / 229 是 IME 的标志）
    handleEnter(event) {
      if (event.isComposing || event.keyCode === 229) return
      event.preventDefault()
      this.handleSend()
    },

    // 先把自己这条塞进列表，再等模型：不然用户 1~60 秒看不到自己发的话
    async handleSend() {
      const text = this.input.trim()
      if (!text || this.sending || !this.activeId) return
      const sessionId = this.activeId
      const localId = `local-${Date.now()}`
      const now = new Date().toISOString()

      this.messages.push({
        id: localId,
        role: 'user',
        content: text,
        status: 'sending',
        created_at: now
      })
      this.input = ''
      this.errorText = ''
      this.sending = true
      this.scrollToBottom()

      try {
        const res = await chat({ session_id: sessionId, message: text })
        const optimistic = this.messages.find((item) => item.id === localId)
        if (optimistic) optimistic.status = 'ok'
        this.messages.push({
          id: res.message_id,
          role: 'assistant',
          content: res.reply,
          status: 'ok',
          created_at: new Date().toISOString(),
          // 后端说「有动作待确认」时，卡片跟着这条消息渲染；
          // 为 null（纯聊天）时什么都不加，界面和以前一样
          pending_action: res.pending_action || null
        })
        this.scrollToBottom()
        // 首句会把「新对话」改成本句开头，左栏要跟着变
        await this.refreshSessions()
      } catch (err) {
        // 后端其实已存了用户消息和一条 failed 记录，这里只把气泡标红，不伪造回复
        const optimistic = this.messages.find((item) => item.id === localId)
        if (optimistic) optimistic.status = 'failed'
        this.errorText = err.message || '发送失败'
      } finally {
        this.sending = false
        this.focusComposer()
      }
    },

    // 重试就是把它从列表里拿掉再发一次（后端会新落一条用户消息，可接受）
    retry(message) {
      if (this.sending) return
      this.messages = this.messages.filter((item) => item.id !== message.id)
      this.input = message.content
      this.handleSend()
    },

    // 卡片里点，状态在页面里改：卡片只管显示，页面管什么时候动数据
    async handleConfirm(action) {
      const cardRef = this.cardRefOf(action.id)
      try {
        const res = await confirmAction(action.id, null, { silent: true })
        // 用响应里的最终内容更新卡片（用户以后要能改草稿时，这里就是回显改后版本的地方）
        this.patchCard(action.id, {
          status: res.status,
          preview: res.preview,
          created_id: res.created && res.created.id
        })
        // 待办真的建出来了：首页/待办页的数据已经变了，
        // 这张卡片顺势记下新建的 id，好在卡片上给出「查看待办」的入口
        this.errorText = ''
      } catch (err) {
        this.reportCardError(cardRef, err)
      }
    },

    async handleCancel(action) {
      const cardRef = this.cardRefOf(action.id)
      try {
        const res = await cancelAction(action.id, { silent: true })
        this.patchCard(action.id, { status: res.status })
      } catch (err) {
        this.reportCardError(cardRef, err)
      }
    },

    // 按 action_id 找到列表里那条消息的 pending_action，就地改
    patchCard(actionId, patch) {
      this.messages.forEach((item) => {
        if (item.pending_action && item.pending_action.id === actionId) {
          Object.assign(item.pending_action, patch)
        }
      })
    },

    // 卡片已经不在屏幕上（用户切了会话）时 refs 是空的，这一支什么都不做
    cardRefOf(actionId) {
      const cards = this.$refs.cards || []
      return (
        cards.find((card) => card.action && card.action.id === actionId) || null
      )
    },

    // 409 = 这张卡在别处已经处理过（另一个标签页 / 连点）；410 = 过期。
    // 这两种都不是「网络挂了」，而是「这张卡不能用了」：
    // 把消息列表重新拉一遍，卡片就会显示成库里真实的状态，
    // 比让用户对着一个僵住的按钮反复点强。401/5xx 之类则只把原因写在卡片上。
    reportCardError(cardRef, err) {
      const message = err.message || '操作失败'
      if (err.status === 409 || err.status === 410) {
        // 重新拉列表：旧卡片会随 messages 一起被替换掉，不用再 finish
        this.loadMessages(this.activeId)
        return
      }
      if (cardRef) cardRef.finish(message)
      else this.errorText = message
    },

    // 模板里只能调 methods/computed 上的东西，直接 import 的函数挂不上，
    // 所以这里给模板开一个同名入口（内部用别名调，避免自己递归自己）
    formatDateTime(value) {
      return fmtDateTime(value)
    },

    formatSessionTime(session) {
      return fmtDateTime(session.last_message_at || session.created_at)
    },


  //将消息区滚动到最底部：
    scrollToBottom() {
      this.$nextTick(() => {
        const el = this.$refs.messagesEl
        if (el) el.scrollTop = el.scrollHeight
      })
    },

    // 处理完一件事就把焦点放回输入框，省得每次都要点一下
    focusComposer() {
      this.$nextTick(() => {
        const el = this.$refs.composer
        if (el && el.focus) el.focus()
      })
    }
  }
}
</script>

<style lang="scss" scoped>
.ai {
  display: flex;
  height: calc(100vh - 88px);
  background: #fff;
  border: 1px solid #e6e8eb;
  border-radius: 4px;
  overflow: hidden;

  &__aside {
    width: 240px;
    flex: 0 0 240px;
    display: flex;
    flex-direction: column;
    border-right: 1px solid #e6e8eb;
    background: #fafbfc;
  }

  &__new {
    margin: 12px;
  }

  &__sessions {
    flex: 1;
    overflow-y: auto;
    padding: 0 8px 8px;
  }

  &__session {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px;
    margin-bottom: 4px;
    border-radius: 4px;
    cursor: pointer;

    &:hover {
      background: #eef1f5;
    }

    &--active,
    &--active:hover {
      background: #e6f0ff;
    }
  }

  &__session-title {
    font-size: 13px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__session-time {
    font-size: 11px;
    color: #a8abb2;
  }

  &__placeholder {
    margin: 8px 0;
    font-size: 12px;
    color: #a8abb2;
    text-align: center;
  }

  &__main {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  &__header {
    height: 44px;
    flex: 0 0 44px;
    display: flex;
    align-items: center;
    padding: 0 16px;
    border-bottom: 1px solid #f0f2f5;
  }

  &__title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
  }

  &__row {
    display: flex;
    flex-direction: column;
    margin-bottom: 14px;

    &--user {
      align-items: flex-end;
    }

    &--assistant,
    &--system,
    &--tool {
      align-items: flex-start;
    }
  }

  &__bubble {
    max-width: 72%;
    padding: 9px 12px;
    border-radius: 8px;
    font-size: 14px;
    line-height: 1.6;
    color: #303133;
    background: #f4f6f8;
    white-space: pre-wrap;
    word-break: break-word;

    .ai__row--user & {
      background: #dbeaff;
    }

    &--failed {
      background: #fef0f0;
      color: #f56c6c;
    }
  }

  &__time {
    margin-top: 4px;
    font-size: 11px;
    color: #c0c4cc;
  }

  &__hint {
    margin: 24px 0;
    text-align: center;
    font-size: 12px;
    color: #a8abb2;
  }

  &__composer {
    flex: 0 0 auto;
    padding: 12px 16px;
    border-top: 1px solid #f0f2f5;
  }

  &__composer-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 8px;
  }

  &__composer-hint {
    font-size: 12px;
    color: #a8abb2;
  }

  &__error {
    margin: 0 0 8px;
    font-size: 12px;
    color: #f56c6c;
  }

  &__retry {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 2px;
  }

  &__retry-text {
    font-size: 11px;
    color: #f56c6c;
  }

  &__empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #a8abb2;
  }

  &__empty-icon {
    font-size: 42px;
    margin-bottom: 12px;
  }

  &__empty-title {
    margin: 0 0 6px;
    font-size: 16px;
    color: #606266;
  }

  &__empty-hint {
    margin: 0;
    font-size: 13px;
  }
}
</style>
