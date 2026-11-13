<template>
  <div class="not-found">
    <h2 class="not-found__title">404</h2>
    <p class="not-found__hint">这个地址没有对应的后台页面。</p>
    <router-link to="/users">回到用户管理</router-link>
  </div>
</template>

<script>
export default {
  name: 'NotFoundView'
}
</script>

<style lang="scss" scoped>
.not-found {
  padding: 80px 0;
  text-align: center;

  &__title {
    margin: 0 0 8px;
    font-size: 40px;
    color: #c0c4cc;
  }

  &__hint {
    margin: 0 0 16px;
    font-size: 13px;
    color: #909399;
  }
}
</style>