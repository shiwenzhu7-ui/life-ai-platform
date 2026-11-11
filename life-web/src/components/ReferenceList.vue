<template>
  <div class="ref-list">
    <p class="ref-list__head">
      <i class="el-icon-document ref-list__icon" />
      答案来自你的知识库（{{ items.length }} 处）
    </p>

    <ul class="ref-list__body">
      <li v-for="(item, index) in items" :key="keyOf(item, index)" class="ref-list__item">
        <div class="ref-list__line" @click="toggle(index)">
          <span class="ref-list__no">{{ index + 1 }}</span>
          <span class="ref-list__file" :title="item.filename">{{ item.filename }}</span>
          <span v-if="locationOf(item)" class="ref-list__where">{{ locationOf(item) }}</span>
          <i
            class="ref-list__toggle"
            :class="expanded[index] ? 'el-icon-arrow-up' : 'el-icon-arrow-down'"
          />
        </div>

        <!--
          收起时只给两行摘要，展开才给全文：
          引用区的存在是为了「能核对」，不是把整份文档摊在这里。
        -->
        <p
          class="ref-list__snippet"
          :class="{ 'ref-list__snippet--open': expanded[index] }"
        >
          {{ item.snippet }}
        </p>
      </li>
    </ul>
  </div>
</template>

<script>
/**
 * 引用来源卡片 —— 回答下面那几行「这句话出自哪份文件的哪一段」。
 *
 * 为什么要有它：RAG 的答案和模型自己编的答案，在屏幕上看不出区别。
 * 只有把出处摆出来，用户才能判断「这是从我上传的文件里读来的」还是「它在猜」。
 * 所以这个组件只在**这一轮真的检索命中**时出现（父组件用 v-if 控制），
 * 空数组时连壳都不该画出来 —— 画一个「暂无引用」只会增加噪音。
 *
 * 它自己**不发请求**：数据随消息一起来（`item.references`），
 * 后端已经把文件名字、页码、段落号、摘要片段都算好了。
 * 展开看全文目前只是把摘要放开 —— 摘要本身就是后端截的
 * `_SNIPPET_CHARS = 120` 字，够核对一个数字/一句话；真要读全文走知识库页。
 */
export default {
  name: 'ReferenceList',
  props: {
    // 后端 ReferenceOut 的数组：
    // { document_id, chunk_id, filename, page_no, chunk_index, snippet, score }
    references: {
      type: Array,
      required: true
    }
  },
  data() {
    return {
      // 按序号记展开状态。用下标而不是 key：列表是渲染后就固定的，
      // 而且同一份文件的两段可能共用 document_id，拿它当键会连在一起展开。
      expanded: {}
    }
  },
  computed: {
    items() {
      return this.references || []
    }
  },
  methods: {
    keyOf(item, index) {
      // chunk_id 同一会话内唯一；老数据缺失时退回到下标
      return item.chunk_id != null ? `c-${item.chunk_id}` : `i-${index}`
    },
    // 页码只有 PDF 才有；DOCX/TXT 整篇算一篇，只能给「第 N 段」。
    // 段落号从 0 开始（后端 chunk_index），展示时 +1 更符合人的数法。
    locationOf(item) {
      const parts = []
      if (item.page_no != null) parts.push(`第 ${item.page_no} 页`)
      if (item.chunk_index != null) parts.push(`第 ${item.chunk_index + 1} 段`)
      return parts.join(' · ')
    },
    toggle(index) {
      // Vue 2 不能直接 obj[key] = x 触发更新，得用 $set
      this.$set(this.expanded, index, !this.expanded[index])
    }
  }
}
</script>

<style scoped lang="scss">
.ref-list {
  // 与气泡同宽，缩进一点表示「这是对上面那句话的补充说明」
  max-width: 72%;
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  background: #fcfcfd;
  font-size: 12px;
}

.ref-list__head {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0 0 6px;
  color: #909399;
}

.ref-list__icon {
  color: #409eff;
}

.ref-list__body {
  margin: 0;
  padding: 0;
  list-style: none;
}

.ref-list__item + .ref-list__item {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed #ebeef5;
}

.ref-list__line {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;

  &:hover .ref-list__file {
    color: #409eff;
  }
}

.ref-list__no {
  flex: 0 0 16px;
  height: 16px;
  line-height: 16px;
  text-align: center;
  border-radius: 3px;
  background: #ecf5ff;
  color: #409eff;
  font-size: 11px;
}

.ref-list__file {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #303133;
  font-weight: 600;
}

.ref-list__where {
  flex: 0 0 auto;
  color: #a8abb2;
}

.ref-list__toggle {
  flex: 0 0 auto;
  margin-left: auto;
  color: #c0c4cc;
}

.ref-list__snippet {
  margin: 4px 0 0 22px;
  color: #606266;
  line-height: 1.5;
  // 收起时两行封顶 —— 超出部分用省略号，展开时全部放开
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  word-break: break-word;

  &--open {
    display: block;
    -webkit-line-clamp: unset;
    overflow: visible;
  }
}
</style>