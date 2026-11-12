<template>
  <div class="shopping">
    <!-- 左栏：清单列表。窄、固定宽、可独立滚动；右栏才是内容区 -->
    <aside class="shopping__side">
      <div class="shopping__side-head">
        <span class="shopping__side-title">我的清单</span>
        <el-button
          type="text"
          size="small"
          icon="el-icon-plus"
          @click="openCreate"
        >
          新建
        </el-button>
      </div>

      <div v-loading="listsLoading" class="shopping__lists">
        <!-- 没有清单：这是整个页面的空态，右栏也一起空着 -->
        <div v-if="!listsLoading && !lists.length" class="empty-state">
          <p class="empty-state__title">还没有购物清单</p>
          <p class="empty-state__hint">建第一张，比如「周末采购」「露营装备」</p>
          <el-button type="primary" size="small" @click="openCreate">新建清单</el-button>
        </div>

        <template v-else>
          <div
            v-for="row in lists"
            :key="row.id"
            :title="row.note || row.name"
            :class="['shopping__list', { 'is-active': row.id === activeId }]"
            @click="selectList(row.id)"
          >
            <span class="shopping__list-name">{{ row.name }}</span>
            <!-- 计数用「未购/总数」：一眼看出还剩几件要买 -->
            <span v-if="row.item_count" class="shopping__list-count">
              <span :class="{ 'shopping__list-count--done': !row.unpurchased_count }">
                {{ row.unpurchased_count }}
              </span>/{{ row.item_count }}
            </span>
            <span v-else class="shopping__list-count shopping__list-count--blank">空</span>
          </div>

          <!-- 归档区折叠起来：平时不想看见它们，但还得能翻回去 -->
          <div v-if="archived.length" class="shopping__archived">
            <div class="shopping__archived-head" @click="archivedOpen = !archivedOpen">
              <i :class="archivedOpen ? 'el-icon-arrow-down' : 'el-icon-arrow-right'" />
              <span>已归档（{{ archived.length }}）</span>
            </div>
            <div v-show="archivedOpen" class="shopping__archived-body">
              <div
                v-for="row in archived"
                :key="row.id"
                :title="row.note || row.name"
                :class="['shopping__list shopping__list--archived', { 'is-active': row.id === activeId }]"
                @click="selectList(row.id)"
              >
                <span class="shopping__list-name">{{ row.name }}</span>
                <span v-if="row.item_count" class="shopping__list-count">{{ row.item_count }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </aside>

    <!-- 右栏：物品明细 -->
    <section class="shopping__main">
      <!-- 一张清单都没选：左栏空、这里也空，不显示任何按钮 -->
      <div v-if="!activeList" class="empty-state">
        <p class="empty-state__title">从左边选一张清单</p>
        <p class="empty-state__hint">选中后在右边添加要买的东西</p>
      </div>

      <template v-else>
        <div class="shopping__main-head">
          <div class="shopping__main-title">
            <h2 class="shopping__title">{{ activeList.name }}</h2>
            <!-- 归档的清单只读：能看、不能勾、不能加，先恢复再改 -->
            <el-tag v-if="activeList.status === 'archived'" size="mini" type="info">已归档</el-tag>
            <el-tag v-else-if="activeList.item_count" size="mini">{{ progressText }}</el-tag>
          </div>

          <div class="shopping__main-actions">
            <el-switch
              v-if="!isArchived"
              :value="hidePurchased"
              active-text="隐藏已买"
              @change="handleHidePurchased"
            />
            <el-button
              v-if="!isArchived"
              size="small"
              icon="el-icon-folder-opened"
              @click="toggleArchive(true)"
            >
              归档
            </el-button>
            <el-button
              v-else
              size="small"
              icon="el-icon-refresh-left"
              @click="toggleArchive(false)"
            >
              恢复
            </el-button>
            <el-button size="small" type="danger" plain icon="el-icon-delete" @click="removeList">
              删除清单
            </el-button>
          </div>
        </div>

        <p v-if="activeList.note" class="shopping__note">{{ activeList.note }}</p>

        <!-- 行内添加：回车就加，加完清空、光标留在输入框里，连着加几样不用点 -->
        <div v-if="!isArchived" class="shopping__add">
          <el-input
            ref="addName"
            v-model="draft.name"
            class="shopping__add-name"
            placeholder="要加的东西，回车添加"
            maxlength="120"
            @keyup.enter.native="addItem"
          />
          <el-input
            v-model="draft.quantity"
            class="shopping__add-qty"
            placeholder="数量"
            @keyup.enter.native="addItem"
          />
          <el-input
            v-model="draft.unit"
            class="shopping__add-unit"
            placeholder="单位"
            maxlength="16"
            @keyup.enter.native="addItem"
          />
          <el-select
            v-model="draft.category_id"
            class="shopping__add-category"
            placeholder="分类（可不填）"
            clearable
          >
            <el-option
              v-for="item in categoryOptions"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
          <el-button type="primary" size="small" :loading="adding" @click="addItem">添加</el-button>
        </div>

        <div v-loading="itemsLoading" class="shopping__items">
          <div v-if="!items.length && !itemsLoading" class="empty-state">
            <template v-if="hidePurchased && activeList.item_count">
              <p class="empty-state__title">都买完了，已买的被藏起来了</p>
              <p class="empty-state__hint">关掉「隐藏已买」就能看到</p>
              <el-button size="small" @click="handleHidePurchased(false)">显示已买</el-button>
            </template>
            <template v-else-if="activeList.status === 'archived'">
              <p class="empty-state__title">这张清单是空的</p>
              <p class="empty-state__hint">归档的清单不能再添加物品，点「恢复」后再加</p>
            </template>
            <template v-else>
              <p class="empty-state__title">还没有要买的东西</p>
              <p class="empty-state__hint">在上面输入框里写一样，回车就加进来了</p>
            </template>
          </div>

          <div v-for="row in visibleItems" v-else :key="row.id" :class="['shopping__item', { 'is-bought': row.is_purchased }]">
            <el-checkbox
              :value="row.is_purchased"
              :disabled="isArchived || pendingIds.includes(row.id)"
              @change="(checked) => setPurchased(row, checked)"
            />
            <span class="shopping__item-name">{{ row.name }}</span>
            <span v-if="row.quantity" class="shopping__item-qty">
              {{ quantityText(row.quantity) }}{{ row.unit === '个' ? '' : row.unit }}
            </span>
            <el-tag v-if="row.category_name" size="mini" type="info">{{ row.category_name }}</el-tag>
            <span v-if="row.note" class="shopping__item-note" :title="row.note">{{ row.note }}</span>
            <span class="shopping__item-spacer" />
            <span v-if="row.is_purchased" class="shopping__item-time">{{ formatDay(row.purchased_at) }}</span>
            <el-button
              v-if="!isArchived"
              type="text"
              size="small"
              class="shopping__item-del"
              @click="removeItem(row)"
            >
              删
            </el-button>
          </div>
        </div>
      </template>
    </section>
  </div>
</template>

<script>
import {
  listShoppingLists,
  createShoppingList,
  getShoppingList,
  updateShoppingList,
  deleteShoppingList,
  createShoppingItem,
  changeItemPurchased,
  deleteShoppingItem
} from '@/api/shopping'
import { listCategories } from '@/api/categories'

// 左栏一屏能装多少：清单一般不超过几十张，不做分页，一次拉 100 张够用。
// 真超过了再谈分页——现在没有证据表明需要。
const LIST_PAGE_SIZE = 100

// 「隐藏已买」记在 localStorage：翻页回来还是你上次的选择，不用每次重新拨。
const HIDE_KEY = 'lifeai:shopping:hidePurchased'

// 与后端 ListCreate.name / ItemCreate.name 的 max_length 对齐，别让用户写完才被 422
const MAX_LIST_NAME = 64
const MAX_ITEM_NAME = 120

export default {
  name: 'ShoppingView',

  data() {
    return {
      listsLoading: false,
      lists: [],
      archived: [],
      archivedOpen: false,
      activeId: null,

      // ---- 右栏 ----
      items: [],
      itemsLoading: false,
      // 正在等勾选结果的物品 id：勾上/取消都是先落库再改状态，
      // 这期间禁用它自己，防止连点两下发出两个相反的请求
      pendingIds: [],
      adding: false,
      draft: emptyDraft(),
      hidePurchased: readHidePurchased(),
      categoryOptions: []
    }
  },

  computed: {
    // 当前选中的清单对象。存在于 lists 或 archived 里都算
    activeList() {
      if (!this.activeId) return null
      const hit = this.lists.find((row) => row.id === this.activeId)
      return hit || this.archived.find((row) => row.id === this.activeId) || null
    },

    isArchived() {
      return Boolean(this.activeList && this.activeList.status === 'archived')
    },

    // 隐藏已买只是不渲染，不删数据：关掉开关原位就回来，
    // 而且位置不变（排序是后端给的「未购在前」，一勾选它就该沉底，
    // 这里不重排——重排会让用户的手指找不到刚才点的是哪一行）
    visibleItems() {
      if (!this.hidePurchased) return this.items
      return this.items.filter((row) => !row.is_purchased)
    },

    // 进度一律从 items 现算，不读 activeList.unpurchased_count：
    // 那个计数来自列表接口，勾选完不重拉列表就是旧的，页面上会出现
    // 「卖完的绿色 0/2」和右栏还没勾的状态并存。items 才是眼前这份真相。
    progressText() {
      const total = this.items.length
      const left = this.items.filter((row) => !row.is_purchased).length
      return left ? `还剩 ${left} / 共 ${total}` : `全部买齐（${total}）`
    }
  },

  watch: {
    // 换清单就重新拉物品；同一张清单不重复拉
    activeId(id, oldId) {
      if (id === oldId) return
      this.items = []
      if (id) this.fetchItems()
    }
  },

  created() {
    this.fetchLists()
    this.fetchCategories()
  },

  methods: {
    // 两个状态各拉一次：active 撑起左栏主体，archived 进折叠区。
    // 并发而不是串行——两个请求互不依赖，串行只会让左栏白等一次往返。
    async fetchLists() {
      this.listsLoading = true
      try {
        const [active, archived] = await Promise.all([
          listShoppingLists({ size: LIST_PAGE_SIZE, status: 'active' }, { silent: true }),
          listShoppingLists({ size: LIST_PAGE_SIZE, status: 'archived' }, { silent: true })
        ])
        this.lists = active.items
        this.archived = archived.items
        this.ensureSelection()
      } catch (e) {
        // 提示已由 request.js 弹过；这里清屏，别留着上一次的清单
        this.lists = []
        this.archived = []
        this.activeId = null
      } finally {
        this.listsLoading = false
      }
    },

    // 选中项失效时自动落到第一张：首次进入、刚删掉当前清单都会走到这里。
    // 优先落在 active 清单上，全归档了才落到归档第一张。
    ensureSelection() {
      const all = [...this.lists, ...this.archived]
      if (!all.length) {
        this.activeId = null
        return
      }
      const stillThere = all.some((row) => row.id === this.activeId)
      if (!stillThere) {
        this.activeId = (this.lists[0] || this.archived[0]).id
      }
    },

    selectList(id) {
      if (this.activeId === id) return
      this.activeId = id
    },

    // 新建：只问名字。备注、状态都留到后面；一上来弹一大堆字段没人填
    async openCreate() {
      let name
      try {
        const result = await this.$prompt('给这张清单起个名字', '新建清单', {
          confirmButtonText: '建',
          cancelButtonText: '取消',
          inputPlaceholder: '例如：周末采购',
          inputValidator: (value) => {
            if (!value || !value.trim()) return '名字不能为空'
            if (value.trim().length > MAX_LIST_NAME) return `名字最多 ${MAX_LIST_NAME} 个字`
            return true
          }
        })
        name = result.value.trim()
      } catch (e) {
        return // 点了取消
      }

      try {
        const row = await createShoppingList({ name })
        this.$message.success(`已建好「${row.name}」`)
        this.activeId = row.id
        await this.fetchLists()
      } catch (e) {
        // 提示已由 request.js 弹过（比如名字重复被拦成 422）
      }
    },

    // ---------------------------------------------------------------- 物品

    async fetchItems() {
      const listId = this.activeId
      if (!listId) return
      this.itemsLoading = true
      try {
        // 明细不分页，一次拿全：购物清单的量级是几十条，前端自己过滤更省事，
        // 而且勾选后重排要看到全部才不会错位
        const detail = await getShoppingList(listId)
        if (listId !== this.activeId) return // 切走了，丢弃过期响应
        this.items = detail.items
        // 顺手用详情里的清单覆盖左栏那条：item_count 可能已经变了
        this.syncListCounts(detail.list)
      } catch (e) {
        if (listId === this.activeId) this.items = []
      } finally {
        if (listId === this.activeId) this.itemsLoading = false
      }
    },

    // 左栏的计数只用数据，不重拉整个列表——重拉会把左栏闪一下，
    // 而且顺序（按创建时间倒序）不会因为这个变化
    syncListCounts(list) {
      if (!list) return
      const target = [...this.lists, ...this.archived].find((row) => row.id === list.id)
      if (!target) return
      target.item_count = list.item_count
      target.unpurchased_count = list.unpurchased_count
    },

    async addItem() {
      if (this.adding) return
      const name = this.draft.name.trim()
      if (!name) {
        this.$message.warning('先写要买什么')
        this.$refs.addName && this.$refs.addName.focus()
        return
      }
      if (name.length > MAX_ITEM_NAME) {
        this.$message.warning(`名字最多 ${MAX_ITEM_NAME} 个字`)
        return
      }

      const quantity = this.parseQuantity(this.draft.quantity)
      if (quantity === null) return // parseQuantity 里已经提示过了

      this.adding = true
      try {
        const payload = {
          name,
          note: this.draft.note || undefined,
          category_id: this.draft.category_id || undefined
        }
        // 数量/单位只在填了的时候发，交给后端用默认值（1 / 个），
        // 免得前端硬编码默认值跟后端对不上
        if (quantity !== '') payload.quantity = quantity
        if (this.draft.unit.trim()) payload.unit = this.draft.unit.trim()

        await createShoppingItem(this.activeId, payload)
        // 只清名字和数量，单位、分类留着——连着加「牛奶 2 瓶」「酸奶 3 瓶」
        // 时单位不变，重填一遍很烦
        this.draft.name = ''
        this.draft.quantity = ''
        this.$refs.addName && this.$refs.addName.focus()
        await this.fetchItems()
      } catch (e) {
        // 提示已由 request.js 弹过
      } finally {
        this.adding = false
      }
    },

    // 数量输入是字符串，后端要 Decimal。返回 null 表示校验没过（已提示），
    // 返回 '' 表示没填（不发这个字段）
    parseQuantity(raw) {
      const text = String(raw || '').trim()
      if (!text) return ''
      if (!/^\d+(\.\d{1,2})?$/.test(text)) {
        this.$message.warning('数量最多两位小数，且不能是负数')
        return null
      }
      if (Number(text) <= 0) {
        this.$message.warning('数量要大于 0')
        return null
      }
      if (Number(text) > 9999.99) {
        this.$message.warning('数量最多 9999.99')
        return null
      }
      return text
    },

    async setPurchased(row, checked) {
      if (this.pendingIds.includes(row.id)) return
      this.pendingIds.push(row.id)
      try {
        const updated = await changeItemPurchased(row.id, checked)
        // 用后端返回的整条替换而不是本地翻转：purchased_at 由后端决定，
        // 本地造一个时间就会出现「显示的时间跟库里不一样」
        const index = this.items.findIndex((item) => item.id === row.id)
        if (index !== -1) this.$set(this.items, index, updated)
        this.syncListCounts({ ...this.activeList, ...this.countLocally() })
      } catch (e) {
        // 失败了不翻转勾选框，界面保持原样（提示已由 request.js 弹过）
      } finally {
        this.pendingIds = this.pendingIds.filter((id) => id !== row.id)
      }
    },

    // 勾选后本地算一次计数，只更新左栏显示，不发请求
    countLocally() {
      return {
        item_count: this.items.length,
        unpurchased_count: this.items.filter((item) => !item.is_purchased).length
      }
    },

    handleHidePurchased(value) {
      this.hidePurchased = value
      writeHidePurchased(value)
    },

    async removeItem(row) {
      try {
        await deleteShoppingItem(row.id)
      } catch (e) {
        return // 提示已由 request.js 弹过（比如别处已经删了 → 404）
      }
      this.items = this.items.filter((item) => item.id !== row.id)
      this.syncListCounts({ ...this.activeList, ...this.countLocally() })
    },

    // 归档 / 恢复：清单一进归档就从左栏主体挪到折叠区，
    // 所以必须重拉列表，不能只改本地那一条的状态
    async toggleArchive(archive) {
      const list = this.activeList
      if (!list) return
      try {
        await updateShoppingList(list.id, { status: archive ? 'archived' : 'active' })
        this.$message.success(archive ? '已归档' : '已恢复')
        if (archive) this.items = []
        await this.fetchLists()
        // 恢复后它可能已经不是第一张了，主动再拉一次明细：
        // activeId 没变就不会触发 watch
        if (!archive) await this.fetchItems()
      } catch (e) {
        // 提示已由 request.js 弹过
      }
    },

    // 删清单会连带删掉所有物品，所以文案里把数量写出来。
    // 左边点了取消的情况下，二次确认会抛错，这个错误必须被当成一条路径吞掉，
    // 不能让它走 catch（否则什么也不提示，用户以为删了）
    async removeList() {
      const list = this.activeList
      if (!list) return
      const detail = list.item_count
        ? `这张清单和它下面的 ${list.item_count} 件物品会一起消失`
        : '这张清单还是空的'
      let confirmed = true
      try {
        await this.$confirm(`${detail}，且无法恢复。确定删除「${list.name}」？`, '删除清单', {
          type: 'warning',
          confirmButtonText: '删除',
          cancelButtonText: '取消',
          confirmButtonClass: 'el-button--danger'
        })
      } catch (e) {
        confirmed = false
      }
      if (!confirmed) return

      try {
        await deleteShoppingList(list.id)
        this.$message.success('已删除')
        this.items = []
        await this.fetchLists()
      } catch (e) {
        // 提示已由 request.js 弹过
      }
    },

    // ---------------------------------------------------------------- 显示

    async fetchCategories() {
      try {
        const rows = await listCategories('shopping', { silent: true })
        this.categoryOptions = rows
      } catch (e) {
        // 分类拿不到不影响加物品（分类本来就是可选的），静默降级成「不可选」
        this.categoryOptions = []
      }
    },

    // 后端给的是 "2.00" / "30.00"，去掉没用的尾零；整数就不显示小数
    quantityText(value) {
      const text = String(value || '')
      if (!text.includes('.')) return text
      return text.replace(/\.?0+$/, '')
    },

    formatDay(value) {
      if (!value) return ''
      const date = new Date(value)
      if (Number.isNaN(date.getTime())) return ''
      const pad = (n) => String(n).padStart(2, '0')
      return `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    }
  }
}

function emptyDraft() {
  return { name: '', quantity: '', unit: '', category_id: '', note: '' }
}

function readHidePurchased() {
  try {
    return window.localStorage.getItem(HIDE_KEY) === '1'
  } catch (e) {
    return false
  }
}

function writeHidePurchased(value) {
  try {
    window.localStorage.setItem(HIDE_KEY, value ? '1' : '0')
  } catch (e) {
    // 隐私模式下写不进去，无所谓：只是刷新后会回到默认值
  }
}
</script>

<style lang="scss" scoped>
.shopping {
  display: flex;
  gap: 16px;
  height: 100%;

  &__side {
    flex: 0 0 240px;
    display: flex;
    flex-direction: column;
    background: #fff;
    border-radius: 4px;
  }

  &__side-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 12px 8px 16px;
    border-bottom: 1px solid #ebeef5;
  }

  &__side-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
  }

  &__lists {
    flex: 1;
    min-height: 120px;
    padding: 8px;
    overflow-y: auto;
  }

  &__list {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    color: #303133;

    &:hover {
      background: #f5f7fa;
    }

    &.is-active {
      background: #ecf5ff;
      color: #409eff;
      font-weight: 600;
    }

    &--archived {
      color: #909399;
    }
  }

  &__list-name {
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  &__list-count {
    flex: none;
    font-size: 12px;
    color: #909399;
    font-variant-numeric: tabular-nums;

    &--done {
      color: #67c23a;
    }

    &--blank {
      opacity: 0.5;
    }
  }

  &__archived {
    margin-top: 8px;
    border-top: 1px dashed #ebeef5;
    padding-top: 8px;
  }

  &__archived-head {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    font-size: 12px;
    color: #909399;
    cursor: pointer;
    user-select: none;

    &:hover {
      color: #606266;
    }
  }

  &__main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    background: #fff;
    border-radius: 4px;
    padding: 16px;
  }

  &__main-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }

  &__main-title {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__title {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
    color: #303133;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  &__main-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: none;
  }

  &__note {
    margin: 0 0 12px;
    font-size: 13px;
    color: #909399;
  }

  &__add {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;

    // 名字占大头，其余固定宽——数量/单位不会敲长句子
    &-name {
      flex: 1;
      min-width: 160px;
    }

    &-qty {
      flex: 0 0 90px;
    }

    &-unit {
      flex: 0 0 90px;
    }

    &-category {
      flex: 0 0 140px;
    }
  }

  // 物品列表自己滚动，不让整页跟着长
  &__items {
    flex: 1;
    min-height: 120px;
    overflow-y: auto;
  }

  &__item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 4px;
    border-bottom: 1px solid #f2f3f5;

    &:last-child {
      border-bottom: none;
    }

    &:hover {
      background: #fafbfc;
    }

    // 已买：整行压暗 + 划线，位置不动
    &.is-bought {
      .shopping__item-name,
      .shopping__item-qty {
        color: #c0c4cc;
        text-decoration: line-through;
      }
    }
  }

  &__item-name {
    font-size: 14px;
    color: #303133;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  &__item-qty {
    flex: none;
    font-size: 13px;
    color: #606266;
    font-variant-numeric: tabular-nums;
  }

  &__item-note {
    font-size: 12px;
    color: #909399;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  &__item-spacer {
    flex: 1;
  }

  &__item-time {
    flex: none;
    font-size: 12px;
    color: #c0c4cc;
    font-variant-numeric: tabular-nums;
  }

  &__item-del {
    flex: none;
    color: #c0c4cc;

    &:hover {
      color: #f56c6c;
    }
  }
}

.empty-state {
  padding: 32px 12px;
  text-align: center;

  &__title {
    margin: 0 0 6px;
    font-size: 14px;
    color: #606266;
  }

  &__hint {
    margin: 0 0 12px;
    font-size: 12px;
    color: #909399;
  }
}
</style>