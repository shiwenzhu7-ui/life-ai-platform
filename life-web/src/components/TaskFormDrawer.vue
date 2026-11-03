<template>
  <el-drawer
    :visible="visible"
    :title="isEdit ? '编辑待办' : '新建待办'"
    direction="rtl"
    size="480px"
    :before-close="handleClose"
    @close="$emit('update:visible', false)"
  >
    <div v-loading="loadingTask" class="task-form">
      <el-form :model="form" label-width="72px" size="small">
        <el-form-item label="标题" required>
          <el-input
            v-model="form.title"
            maxlength="120"
            show-word-limit
            placeholder="要做的事"
          />
        </el-form-item>

        <el-form-item label="备注">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="可选"
          />
        </el-form-item>

        <el-form-item label="分类">
          <el-select v-model="form.category_id" class="task-form__control">
            <!-- 0 = 未分类，是后端认可的值，不是「空」 -->
            <el-option label="未分类" :value="0" />
            <el-option
              v-for="item in taskCategories"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="优先级">
          <el-select v-model="form.priority" class="task-form__control">
            <el-option
              v-for="item in priorityOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="截止时间">
          <!-- 不设 value-format：v-model 拿到的是 Date 对象，提交时再统一转 +08:00 -->
          <el-date-picker
            v-model="form.due_at"
            type="datetime"
            class="task-form__control"
            placeholder="不填表示没有截止时间"
          />
        </el-form-item>
      </el-form>

      <div class="task-form__footer">
        <el-button size="small" @click="handleClose">取消</el-button>
        <el-button type="primary" size="small" :loading="saving" @click="submit">
          保存
        </el-button>
      </div>

      <p class="task-form__hint">
        提示：编辑时只提交改动过的字段；清空备注或截止时间会真的清空（发 null）。
      </p>
    </div>
  </el-drawer>
</template>

<script>
import { createTask, getTask, updateTask } from '@/api/tasks'
import { toApiDateTime } from '@/utils/format'

const PRIORITY_OPTIONS = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' }
]

// 新建时的初值：每次打开抽屉都要一份新对象
const emptyForm = () => ({
  title: '',
  description: '',
  category_id: 0,
  priority: 'medium',
  due_at: null
})

export default {
  name: 'TaskFormDrawer',
  props: {
    visible: { type: Boolean, default: false },
    // 有 id = 编辑；没有 = 新建
    taskId: { type: Number, default: null }
  },
  data() {
    return {
      form: emptyForm(),
      // 打开时把表单拍个快照，保存时用它算差异
      snapshot: null,
      loadingTask: false,
      saving: false,
      priorityOptions: PRIORITY_OPTIONS
    }
  },
  computed: {
    isEdit() {
      return this.taskId !== null && this.taskId !== undefined
    },
    taskCategories() {
      return this.$store.getters['categories/listOf']('task')
    }
  },
  watch: {
    // 抽屉打开时才初始化，避免隐藏状态下也发请求
    visible(value) {
      if (value) this.init()
    }
  },
  methods: {
    init() {
      this.form = emptyForm()
      this.snapshot = this.serialize()
      if (this.isEdit) this.loadTask()
    },

    // 用来比较「有没有改过」：Date 转成时间戳，备注的空串归一成 null
    serialize() {
      const { title, description, category_id, priority, due_at } = this.form
      return {
        title,
        description: description && description.trim() ? description : null,
        category_id,
        priority,
        due_at: due_at ? new Date(due_at).getTime() : null
      }
    },

    async loadTask() {
      this.loadingTask = true
      try {
        // 不用列表里那行：列表可能是几分钟前拉的，先取一次最新再回填
        const task = await getTask(this.taskId)
        this.form = {
          title: task.title,
          description: task.description || '',
          category_id: task.category_id || 0,
          priority: task.priority,
          due_at: task.due_at ? new Date(task.due_at) : null
        }
        this.snapshot = this.serialize()
      } catch (e) {
        // 404（已被别人删除）等：关掉抽屉并让父组件刷新，别卡在转圈
        this.$emit('update:visible', false)
        this.$emit('failed')
      } finally {
        this.loadingTask = false
      }
    },

    // 只发真的改过的字段：没动的键不出现，后端才分得清「没传」和「传了 null」
    buildDiff() {
      const current = this.serialize()
      const payload = {}
      Object.keys(current).forEach((key) => {
        if (current[key] !== this.snapshot[key]) payload[key] = current[key]
      })
      return payload
    },

    async submit() {
      const title = this.form.title.trim()
      if (!title) {
        this.$message.error('标题不能为空')
        return
      }

      this.saving = true
      try {
        if (this.isEdit) {
          const payload = this.buildDiff()
          if (Object.keys(payload).length === 0) {
            this.$message.info('没有改动')
            this.$emit('update:visible', false)
            return
          }
          // due_at 在 payload 里是时间戳，转回带 +08:00 的串
          if ('due_at' in payload) {
            payload.due_at = payload.due_at === null ? null : toApiDateTime(payload.due_at)
          }
          await updateTask(this.taskId, payload)
        } else {
          await createTask({
            title,
            description: this.form.description.trim() || null,
            category_id: this.form.category_id,
            priority: this.form.priority,
            due_at: toApiDateTime(this.form.due_at)
          })
        }
        this.$message.success(this.isEdit ? '已保存' : '已创建')
        this.$emit('saved')
        this.$emit('update:visible', false)
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
.task-form {
  padding: 0 20px 20px;

  &__control {
    width: 100%;
  }

  &__footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 8px;
  }

  &__hint {
    margin: 16px 0 0;
    font-size: 12px;
    line-height: 1.6;
    color: #909399;
  }
}
</style>
