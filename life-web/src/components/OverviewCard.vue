<template>
  <section class="ov-card">
    <header class="ov-card__head">
      <h3 class="ov-card__title">{{ title }}</h3>
      <router-link v-if="to" :to="to" class="ov-card__link">
        {{ linkText }} <span class="ov-card__arrow">▸</span>
      </router-link>
    </header>

    <div class="ov-card__body">
      <slot />
    </div>

    <footer v-if="$slots.footer" class="ov-card__foot">
      <slot name="footer" />
    </footer>
  </section>
</template>

<script>
/**
 * 首页卡片壳：标题 + 内容 + 右上角「查看」链接。
 *
 * 两张卡（待办 / 消费）结构完全一样，只有内容不同，所以壳抽出来，
 * 卡片内容由调用方用默认插槽填。
 */
export default {
  name: 'OverviewCard',
  props: {
    title: {
      type: String,
      required: true
    },
    // 有 to 才显示链接；没有就不显示（比如以后某张卡不需要跳转）
    to: {
      type: [String, Object],
      default: null
    },
    linkText: {
      type: String,
      default: '查看'
    }
  }
}
</script>

<style scoped lang="scss">
.ov-card {
  display: flex;
  flex-direction: column;
  background: #fff;
  border-radius: 6px;
  box-shadow: 0 1px 4px rgba(0, 21, 41, 0.08);
  padding: 18px 20px 20px;
}

.ov-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.ov-card__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

.ov-card__link {
  font-size: 13px;
  color: #409eff;
  text-decoration: none;

  &:hover {
    text-decoration: underline;
  }
}

.ov-card__arrow {
  font-size: 12px;
}

.ov-card__body {
  flex: 1;
}

.ov-card__foot {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid #f0f2f5;
}
</style>
