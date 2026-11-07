<template>
  <div class="ai">
    <!-- 左栏：会话列表（第 2 步接数据） -->
    <aside class="ai__aside">
      <el-button class="ai__new" icon="el-icon-plus" size="small" @click="handleCreate">
        新建对话
      </el-button>
      <div class="ai__sessions">
        <p class="ai__placeholder">还没有对话</p>
      </div>
    </aside>

    <!-- 右栏：消息流（第 3 步接数据） -->
    <section class="ai__main">
      <div class="ai__empty">
        <i class="el-icon-chat-dot-round ai__empty-icon" />
        <p class="ai__empty-title">AI 生活助手</p>
        <p class="ai__empty-hint">在左侧选择一个对话，或点「新建对话」开始</p>
      </div>
    </section>
  </div>
</template>

<script>
export default {
  name: 'AiView',
  data() {
    return {
      sessions: [],
      activeId: null,
      messages: [],
      sending: false
    }
  },
  methods: {
    handleCreate() {
      // 第 2 步实现：调 createSession 后放进 sessions 并选中
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
