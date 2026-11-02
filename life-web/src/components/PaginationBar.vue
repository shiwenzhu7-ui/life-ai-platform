<template>
  <div class="pagination-bar">
    <el-pagination
      background
      :current-page="page"
      :page-size="size"
      :page-sizes="[10, 20, 50]"
      :total="total"
      layout="total, sizes, prev, pager, next, jumper"
      @current-change="handleCurrentChange"
      @size-change="handleSizeChange"
    />
  </div>
</template>

<script>
// 只有分页条：待办页和消费页共用同一份，所以抽成组件而不是各写一遍。
// 父组件只关心「现在要看第几页、每页几条」，拿到就用这个去请求。
export default {
  name: 'PaginationBar',
  props: {
    page: { type: Number, default: 1 },
    size: { type: Number, default: 20 },
    total: { type: Number, default: 0 }
  },
  methods: {
    handleCurrentChange(page) {
      this.$emit('change', { page, size: this.size })
    },
    // 改每页条数时回到第 1 页：否则可能停在一个不存在的页码上（比如原来第 3 页、改成每页 50 条后只有 1 页）
    handleSizeChange(size) {
      this.$emit('change', { page: 1, size })
    }
  }
}
</script>

<style lang="scss" scoped>
.pagination-bar {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}
</style>
