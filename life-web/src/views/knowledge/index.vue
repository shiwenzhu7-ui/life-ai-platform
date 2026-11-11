<template>
  <div class="knowledge">
    <!-- 顶部条：左边一句用途说明，右边上传按钮。
         说明写在这里而不是空态里，因为文件多了以后空态就不出现，
         而「这里能干什么」是常驻信息。 -->
    <div class="knowledge__toolbar">
      <div>
        <p class="knowledge__title">我的知识库</p>
        <p class="knowledge__hint">
          上传 PDF / Word / 文本，之后在 AI 助手里可以直接提问，回答会标出来自哪份文件
        </p>
      </div>
      <el-button
        type="primary"
        size="small"
        icon="el-icon-upload2"
        :loading="uploading"
        @click="$refs.picker.click()"
      >
        {{ uploading ? `上传中 ${uploadPercent}%` : '上传文档' }}
      </el-button>
    </div>

    <!-- ------------------------------------------------------------------
         真正的文件选择器：隐藏起来，由上面的按钮 click() 触发。
         不用 el-upload 的理由：
           1. el-upload 自己的 XHR 绕过了 utils/request.js，
              401 不会跳登录页、422 不会翻成中文、拖拽进度也不走同一个出口；
           2. 我们一次只传一个，不需要排队、多选、自动上传那一整套状态机。
         限制 accept 只是「文件选择框默认过滤」，不是校验 ——
         用户仍可在输入框里强制选别的类型，真正的校验在后端（422）。
         ------------------------------------------------------------------ -->
    <input
      ref="picker"
      type="file"
      accept=".pdf,.docx,.txt"
      class="knowledge__picker"
      @change="onFilePicked"
    />

    <!-- 筛选条：状态 + 文件名搜索。和待办页保持同一套布局节奏。
         两个下拉/输入都绑在 filters 对象上，通过各自的 setter 触发查询，
         而不是用 watch 盯它们：watch 会在「程序里改值」时也触发，
         而 setter 只响应真实输入。 -->
    <div class="knowledge__filters">
      <el-input
        :value="filters.keyword"
        class="knowledge__filter"
        placeholder="搜索文件名"
        prefix-icon="el-icon-search"
        clearable
        @input="onKeywordInput"
      />
      <el-select
        :value="filters.status"
        class="knowledge__filter"
        clearable
        placeholder="处理状态"
        @change="onStatusChange"
      >
        <el-option
          v-for="item in statusOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" border stripe style="width: 100%">
      <template slot="empty">
        <div class="knowledge__empty">
          <i class="el-icon-folder-opened knowledge__empty-icon" />
          <p class="knowledge__empty-title">
            {{ hasFilter ? '没有符合条件的文档' : '知识库还是空的' }}
          </p>
          <p class="knowledge__empty-hint">
            {{
              hasFilter
                ? '试试换个关键词，或者清掉筛选条件'
                : '传一份 PDF / Word / 文本进来，就能在 AI 助手里问它'
            }}
          </p>
        </div>
      </template>

      <el-table-column label="文件名" min-width="260" show-overflow-tooltip>
        <template slot-scope="{ row }">
          <i class="el-icon-document knowledge__file-icon" />
          <span>{{ row.filename }}</span>
        </template>
      </el-table-column>

      <el-table-column label="类型" width="80" align="center">
        <template slot-scope="{ row }">
          <el-tag size="mini" effect="plain">{{ (row.file_ext || '').toUpperCase() }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="大小" width="100" align="right">
        <template slot-scope="{ row }">{{ formatSize(row.file_size) }}</template>
      </el-table-column>

      <el-table-column label="切块" width="80" align="right">
        <template slot-scope="{ row }">
          <span v-if="row.chunk_count">{{ row.chunk_count }}</span>
          <span v-else class="knowledge__muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="140">
        <template slot-scope="{ row }">
          <el-tag size="mini" :type="statusOf(row.status).type">
            {{ statusOf(row.status).label }}
          </el-tag>
          <!-- 失败原因和状态挤在同一列：用户真正关心的就是「为什么没好」 -->
          <p v-if="row.error_message" class="knowledge__error" :title="row.error_message">
            {{ row.error_message }}
          </p>
        </template>
      </el-table-column>

      <el-table-column label="上传时间" width="150">
        <template slot-scope="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>

      <el-table-column label="操作" width="140" align="center">
        <template slot-scope="{ row }">
          <el-button
            type="text"
            size="mini"
            :disabled="!previewable(row)"
            @click="openPreview(row)"
          >
            切块预览
          </el-button>
          <el-button
            type="text"
            size="mini"
            class="knowledge__danger"
            :loading="deletingId === row.id"
            @click="handleDelete(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar :page="page" :size="size" :total="total" @change="handlePageChange" />

    <!--
      切块预览：从右侧滑出的抽屉，不遮挡列表。
      这个功能的意义是让用户亲眼看到「我的文档被切成了什么样」——
      PDF 解析出乱码、切块断在半句话上，一眼就能发现；
      不做的话，只能等 AI 答得离谱再反推是哪里出了问题。
    -->
    <el-drawer
      title="切块预览"
      :visible.sync="preview.visible"
      direction="rtl"
      size="560px"
      @closed="closePreview"
    >
      <div v-if="preview.loading" v-loading="true" class="knowledge__preview-loading" />

      <div v-else-if="preview.error" class="knowledge__preview-error">
        <p>{{ preview.error }}</p>
        <el-button size="small" @click="loadPreview">重试</el-button>
      </div>

      <template v-else>
        <header class="knowledge__preview-head">
          <p class="knowledge__preview-title">{{ preview.document.filename }}</p>
          <p class="knowledge__preview-meta">
            共 {{ preview.total }} 段
            <template v-if="preview.truncated">
              ，这里只显示前 {{ preview.chunks.length }} 段
            </template>
          </p>
        </header>

        <!-- 按页分组：PDF 一页常被切成好几块，逐块重复「第 3 页」很吵；
             没有页码（DOCX / TXT 整篇算一篇）时分组标题是空的，自然不显示 -->
        <section
          v-for="group in previewGroups"
          :key="group.key"
          class="knowledge__group"
        >
          <h4 v-if="group.label" class="knowledge__group-label">{{ group.label }}</h4>
          <article
            v-for="chunk in group.chunks"
            :key="chunk.id"
            class="knowledge__chunk"
          >
            <p class="knowledge__chunk-head">
              第 {{ chunk.chunk_index + 1 }} 段
              <span class="knowledge__chunk-meta">{{ chunk.token_count }} 字</span>
            </p>
            <p class="knowledge__chunk-body">{{ chunk.content }}</p>
          </article>
        </section>

        <p v-if="!preview.total" class="knowledge__preview-empty">
          这份文档还没有切块（可能还在处理中，或者处理失败了）
        </p>
      </template>
    </el-drawer>
  </div>
</template>

<script>
import { formatDateTime } from '@/utils/format'
import { uploadDocument, listDocuments, deleteDocument, getDocument } from '@/api/knowledge'
import PaginationBar from '@/components/PaginationBar.vue'

// 后端 KB_MAX_UPLOAD_BYTES 的默认值，20MB。
// 前端先拦一道只是为了让用户少等一次网络往返，**不是安全边界**：
// 后端在读取流的过程中按真实字节数判断，超了立刻 413。
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024
const ALLOWED_EXTS = ['pdf', 'docx', 'txt']

// 把 File 名字里的扩展名取出来（小写）。没有扩展名返回空串。
function extOf(name) {
  const idx = String(name || '').lastIndexOf('.')
  return idx > 0 ? name.slice(idx + 1).toLowerCase() : ''
}

// 状态 → 中文 + 标签颜色。放在组件外，纯常量不需要响应式。
//
// 这六个词是后端的处理流水线：上传 → 解析 → 切块 → 向量化 → 就绪。
// 中间三步都很短（小文件不到 1 秒），但轮询时偶尔能看到，所以都要有中文。
const STATUS_MAP = {
  pending: { label: '排队中', type: 'info' },
  parsing: { label: '解析中', type: 'warning' },
  chunking: { label: '切块中', type: 'warning' },
  embedding: { label: '向量化中', type: 'warning' },
  ready: { label: '已就绪', type: 'success' },
  failed: { label: '处理失败', type: 'danger' }
}

// 非终态：还在后台跑。只要列表里还剩一个这样的文档，就要继续轮询。
// ready / failed 是终点，永远不会再变，所以不在这个集合里。
const ACTIVE_STATUSES = ['pending', 'parsing', 'chunking', 'embedding']

// 轮询间隔。后台处理一份普通文档通常 1~3 秒（切块 + 512 维嵌入都是本地算的），
// 3 秒够快也不至于让接口压力过大。
const POLL_INTERVAL_MS = 3000

// 关键词输入防抖。每敲一个字就查一次会把接口打满，
// 400ms 是「手速快的人还在打字、手速慢的人已经想好了」的折中。
const SEARCH_DEBOUNCE_MS = 400

// 预览抽屉一次拉多少块。后端上限是 200，这里取 50：
// 一份 512 字符/块的文档，50 段已经是 2.5 万字，够看清「切得对不对」；
// 真要看全文，用户该回去看原文件，而不是在抽屉里滚动两分钟。
const PREVIEW_CHUNK_LIMIT = 50

// 判断「还是刚才那批筛选条件吗」：异步返回时要用它比对，
// 不一致说明用户已经换了条件，这份结果过期了，直接丢掉。
function sameFilters(a, b) {
  return a.page === b.page && a.size === b.size && a.status === b.status && a.keyword === b.keyword
}

export default {
  name: 'KnowledgeView',
  components: { PaginationBar },
  data() {
    return {
      loading: false,
      // 一次查询的四个维度。改任何一条都要回第 1 页（见 resetToFirstPage）
      filters: {
        page: 1,
        size: 20,
        status: '',
        keyword: ''
      },
      // 关键词和下拉的「已提交值」。输入框借用一个中间变量，
      // 防抖到期后才写进这里，避免每次按键都改 filters。
      keywordInput: '',
      rows: [],
      total: 0,
      // 上传是一个「一次只能进行一个」的动作：按钮转圈、显示百分比、
      // 期间不能再点，所以三个状态都放在页面顶层，而不是每行各存一份。
      uploading: false,
      uploadPercent: 0,
      // 正在删的那一行 id。用 id 而不是布尔值：删除是行级动作，
      // 布尔会让整张表的按钮一起转圈，看起来像「所有文件都在删」
      deletingId: null,
      // 切块预览抽屉的状态。整块打包成一个对象，是因为「打开 → 加载 →
      // 成功/失败」永远一起变，拆成四个顶层字段反而容易漏更新其中一个
      preview: {
        visible: false,
        loading: false,
        error: '',
        document: {},
        chunks: [],
        total: 0,
        truncated: false
      },
      statusOptions: Object.keys(STATUS_MAP).map((value) => ({
        value,
        label: STATUS_MAP[value].label
      }))
    }
  },
  computed: {
    hasFilter() {
      return Boolean(this.filters.keyword || this.filters.status)
    },

    // 当前页里还有没有在处理的文档。用它决定轮询启停。
    hasActive() {
      return this.rows.some((row) => ACTIVE_STATUSES.includes(row.status))
    },

    // 抽屉里的块按页分组。只做展示层分组，后端返回的顺序本来就是
    // chunk_index 递增，这里不排序、不去重，免得掩盖后端的问题。
    previewGroups() {
      const groups = []
      this.preview.chunks.forEach((chunk) => {
        const last = groups[groups.length - 1]
        if (last && last.pageNo === chunk.page_no) {
          last.chunks.push(chunk)
          return
        }
        groups.push({
          key: chunk.page_no === null ? 'no-page' : `page-${chunk.page_no}`,
          pageNo: chunk.page_no,
          label: chunk.page_no === null ? '' : `第 ${chunk.page_no} 页`,
          chunks: [chunk]
        })
      })
      return groups
    }
  },
  watch: {
    // 轮询的开关由数据驱动，而不是在 fetchData 里手工 start/stop：
    // 数据可能因为删除、翻页、筛选而整体换掉，只有「算出来还有没有活跃行」是唯一可靠的判据。
    hasActive(active) {
      if (active) {
        this.startPolling()
      } else {
        this.stopPolling()
      }
    }
  },
  created() {
    this.fetchData()
  },
  beforeDestroy() {
    // 组件离开时必须停掉两个定时器，否则还会继续发请求、并且对已销毁的实例赋值
    this.stopPolling()
    if (this.keywordTimer) {
      clearTimeout(this.keywordTimer)
      this.keywordTimer = null
    }
  },
  methods: {
    formatDateTime,

    statusOf(status) {
      return STATUS_MAP[status] || { label: status, type: '' }
    },

    // ------------------------------------------------------------ 查询

    // silent：轮询是背景刷新，失败了不该每秒弹一个红条。
    // 真正的错误提示留给用户主动操作（改筛选、翻页、首次进入）。
    async fetchData({ silent = false } = {}) {
      const asked = { ...this.filters }
      if (!silent) this.loading = true
      try {
        const data = await listDocuments({
          page: asked.page,
          size: asked.size,
          status: asked.status,
          keyword: asked.keyword
        }, silent ? { silent: true } : undefined)

        // 请求发出后用户可能已经改条件了。晚到的旧响应会覆盖新结果，
        // 表现为「明明选了已就绪，列表里却混着排队中」。比对一下，过期就丢。
        if (!sameFilters(asked, this.filters)) return

        this.rows = data.items
        this.total = data.total

        // 删掉最后一条后可能停在一个空页上：退到最后一页重新拉一次
        if (this.rows.length === 0 && this.total > 0 && this.filters.page > 1) {
          this.filters.page = Math.ceil(this.total / this.filters.size)
          await this.fetchData({ silent })
        }
      } catch (e) {
        // 非静默查询失败时清空列表，别让旧数据骗人；静默轮询失败就保持现状，下一轮再试
        if (!silent) {
          this.rows = []
          this.total = 0
        }
      } finally {
        this.loading = false
      }
    },

    startPolling() {
      if (this.pollTimer) return
      this.pollTimer = setInterval(async () => {
        await this.fetchData({ silent: true })
        // 这一轮拉回来发现没有活跃行了，watch 会调 stopPolling，这里不用重复判断
      }, POLL_INTERVAL_MS)
    },

    stopPolling() {
      if (!this.pollTimer) return
      clearInterval(this.pollTimer)
      this.pollTimer = null
    },

    // 改筛选条件一律回第 1 页：留在第 3 页可能已经没有结果了
    resetToFirstPage() {
      this.filters.page = 1
      this.fetchData()
    },

    onStatusChange(status) {
      this.filters.status = status || ''
      this.resetToFirstPage()
    },

    // 关键词走防抖；清空（点小叉）时立即查，不用等 400ms
    onKeywordInput(value) {
      this.keywordInput = value
      if (this.keywordTimer) clearTimeout(this.keywordTimer)
      const next = (value || '').trim()
      if (next === this.filters.keyword) return
      if (!next) {
        this.filters.keyword = ''
        this.resetToFirstPage()
        return
      }
      this.keywordTimer = setTimeout(() => {
        this.keywordTimer = null
        // 再确认一次：防抖期间可能已经被清空了
        if (!this.keywordInput.trim()) return
        this.filters.keyword = this.keywordInput.trim()
        this.resetToFirstPage()
      }, SEARCH_DEBOUNCE_MS)
    },

    handlePageChange({ page, size }) {
      this.filters.page = page
      this.filters.size = size
      this.fetchData()
    },

    // 文件大小按 1024 进位。只保留一位小数，且 0.x 的显示 0.0 而不是空白
    formatSize(bytes) {
      const n = Number(bytes) || 0
      if (n < 1024) return `${n} B`
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
      return `${(n / (1024 * 1024)).toFixed(1)} MB`
    },

    // ------------------------------------------------------------ 删除
    //
    // 后端是**硬删**：documents 行、document_chunks 行、Milvus 里的向量、
    // uploads 下的原文件，四样一起消失，没有回收站也没有恢复接口。
    // 所以这里的二次确认不是走过场 —— 它是用户最后一道刹车。

    // 先说清楚「删掉之后会怎样」，而不是只问「确定删除吗」。
    // 用户真正需要判断的是「这个文件已经没用了」，提示语要指向这件事。
    async handleDelete(row) {
      if (this.deletingId !== null) return
      try {
        await this.$confirm(`${this.deleteWarning(row)}`, '删除这份文档？', {
          confirmButtonText: '删除',
          cancelButtonText: '取消',
          type: 'warning',
          // 默认的「确定」按钮是蓝色主按钮，这里换成红色：破坏性操作不该长得像正常操作
          confirmButtonClass: 'el-button--danger'
        })
      } catch (e) {
        return // 点了取消 / 关掉弹窗
      }

      this.deletingId = row.id
      try {
        await deleteDocument(row.id)
        this.$message.success(`已删除「${row.filename}」，不会再参与回答`)
        // 删掉的正好是当前页最后一条时，页码要退一页，
        // 否则 fetchData 里那段「空页回退」逻辑会先把用户送到最后一页 —— 顺序反了
        if (this.rows.length === 1 && this.filters.page > 1) {
          this.filters.page -= 1
        }
        await this.fetchData()
      } catch (e) {
        // 404 = 别处已经删了（另一个标签页），列表刷新一下即可，不用当错误报
        if (e && e.status === 404) {
          this.$message.info('这份文档已经不存在了')
          await this.fetchData()
        }
        // 其它错误（500 / 网络）request.js 已弹过，这里不重复
      } finally {
        this.deletingId = null
      }
    },

    // 确认框正文。把后果说全：文件本身、切块、向量都会没，AI 也用不了了。
    // 失败的行没有向量，单独说一句，省得用户以为删掉的东西比实际更多。
    deleteWarning(row) {
      const lines = [`「${row.filename}」会被彻底删除，没有回收站，删了不能恢复。`]
      if (row.status === 'ready' && row.chunk_count) {
        lines.push(`它的 ${row.chunk_count} 个切块和对应的向量会一起清掉，AI 之后不会再引用它。`)
      } else if (row.status === 'failed') {
        lines.push('这份文档处理失败了，没有可用于回答的内容，删除只会清掉记录和原文件。')
      } else {
        lines.push('它还在后台处理中，删除后处理会被一并终止。')
      }
      return lines.join('\n')
    },

    // ------------------------------------------------------------ 切块预览
    //
    // 只有**已经切好块**的文档才值得看：失败和排队中的没有块，
    // 点开只会看到一个空抽屉，用户会以为功能坏了。
    previewable(row) {
      return row.chunk_count > 0
    },

    openPreview(row) {
      if (!this.previewable(row)) return
      this.preview.visible = true
      this.preview.loading = true
      this.preview.error = ''
      this.preview.document = row
      this.preview.chunks = []
      this.preview.total = 0
      this.preview.truncated = false
      this.loadPreview()
    },

    // 详情接口返回 { document, chunks }，chunks 最多 50 条（请求里传的 chunk_limit）。
    // 没有单独的总数/截断标记，所以「共 N 段」用 document.chunk_count，
    // 它和 chunks 的长度不一样时，说明抽屉里只显示了前一部分。
    async loadPreview() {
      const row = this.preview.document
      if (!row || !row.id) return
      this.preview.loading = true
      this.preview.error = ''
      try {
        const data = await getDocument(
          row.id,
          { chunk_limit: PREVIEW_CHUNK_LIMIT },
          { silent: true }
        )
        // 请求回来时用户可能已经点了另一个文件
        if (!this.preview.visible || this.preview.document.id !== row.id) return
        this.preview.chunks = data.chunks || []
        // chunk_count 是文档里真实的块数；列表行可能还没刷新成最新值，以详情为准
        this.preview.total = (data.document && data.document.chunk_count) || this.preview.chunks.length
        this.preview.truncated = this.preview.total > this.preview.chunks.length
      } catch (e) {
        if (!this.preview.visible) return
        this.preview.error = (e && e.message) || '切块加载失败'
      } finally {
        this.preview.loading = false
      }
    },

    // 抽屉关掉时清干净：下次打开是新的文件，不能残留上一份的内容
    closePreview() {
      this.preview.visible = false
      this.preview.loading = false
      this.preview.error = ''
      this.preview.document = {}
      this.preview.chunks = []
      this.preview.total = 0
      this.preview.truncated = false
    },

    // ------------------------------------------------------------ 上传
    //
    // 选完文件立刻清空 input.value，否则「同一个文件传两次」时
    // change 事件不会触发（value 没变）—— 用户会以为按钮坏了。
    onFilePicked(event) {
      const file = event.target.files && event.target.files[0]
      event.target.value = ''
      if (!file) return
      this.upload(file)
    },

    // 本地预检。顺序和成本都是有意安排的：
    // 先在内存里看一眼名字和大小（微秒级），再决定要不要花几十秒去传。
    checkFile(file) {
      const ext = extOf(file.name)
      if (!ALLOWED_EXTS.includes(ext)) {
        return `不支持的文件类型：${ext ? `.${ext}` : '（无扩展名）'}，只支持 PDF / Word / TXT`
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        return `文件 ${this.formatSize(file.size)} 超过 20MB 上限`
      }
      if (file.size === 0) {
        return '这个文件是空的'
      }
      return null
    },

    async upload(file) {
      // 上传中再点一次按钮不会有文件选择框（按钮是 loading 态），
      // 但从别的入口调进来时仍要挡住，不然会有两个请求抢同一个进度条
      if (this.uploading) return

      const problem = this.checkFile(file)
      if (problem) {
        this.$message.warning(problem)
        return
      }

      this.uploading = true
      this.uploadPercent = 0

      try {
        const doc = await uploadDocument(file, {
          // 上传是唯一会跑几十秒的接口，进度回调让我们能显示百分比。
          // 注意 axios 报的是「已发出字节数」，浏览器在有本地代理时可能瞬间冲到头，
          // 所以进度条到 100% 不等于服务端已经处理完 —— 那要看 status。
          onUploadProgress: (e) => {
            if (!e || !e.total) return
            this.uploadPercent = Math.min(99, Math.round((e.loaded / e.total) * 100))
          }
        })
        this.uploadPercent = 100
        this.$message.success(`已上传「${doc.filename}」，正在后台处理`)
        // 把后端返回的这条 pending 记录插到最前面：用户立刻看到「我的文件进来了」，
        // 不用等下一次轮询。之后轮询拉回来的数据里含这条（id 相同），
        // 整表替换时它会被同 id 的新状态覆盖，不会出现两份。
        await this.fetchData()
        if (this.filters.page === 1) {
          // 只在第 1 页时手动插队：翻到第 3 页上传时，新文件不该出现在第 3 页顶部
          this.rows = [doc, ...this.rows.filter((row) => row.id !== doc.id)]
        }
      } catch (e) {
        // 三种「不算失败」的特殊情况要单独说人话，其余交给 request.js 已经弹过的提示：
        //   409 重复   —— 后端在 detail 里给了已有那份的 id，引导用户去看它
        //   413 太大   —— 后端按真实字节数判的，可能比本地预检更严（比如有编码差异）
        //   422 不支持 —— 本地放过了但后端不认（比如大小写、扩展名不在白名单）
        if (e && e.status === 409) {
          const existed = e.detail && e.detail.document_id
          this.$message.info(
            existed
              ? `这份文件已经在列表里了（编号 ${existed}），不用重复上传`
              : '这份文件已经在列表里了，不用重复上传'
          )
          this.fetchData()
        } else if (e && (e.status === 413 || e.status === 422)) {
          // 后端 message 已经是中文，request.js 弹过；这里再补一条带文件名的
          this.$message.warning(`「${file.name}」没有上传成功：${e.message}`)
        }
        // 其它错误（网络中断、500）不重复弹，request.js 的拦截器已经处理
      } finally {
        this.uploading = false
        this.uploadPercent = 0
      }
    }
  }
}
</script>

<style lang="scss" scoped>
.knowledge {
  padding: 16px;
  background: #fff;
  border-radius: 4px;

  &__toolbar {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    padding-bottom: 16px;
  }

  &__title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: #303133;
  }

  &__hint {
    margin: 4px 0 0;
    font-size: 12px;
    color: #909399;
  }

  &__filters {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding-bottom: 12px;
  }

  &__filter {
    width: 200px;
  }

  // 隐藏的文件选择框。用 display:none 而不是 visibility/opacity：
  // 三个都行，但 display:none 不会在文档流里占位，也不可被 Tab 聚焦 ——
  // 键盘用户走的是那个正常的按钮，无障碍上只有一个入口。
  &__picker {
    display: none;
  }

  &__file-icon {
    margin-right: 6px;
    color: #909399;
  }

  &__muted {
    color: #c0c4cc;
  }

  &__error {
    margin: 4px 0 0;
    font-size: 12px;
    color: #f56c6c;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  // ---------------- 切块预览抽屉 ----------------

  &__preview-loading {
    height: 160px;
  }

  &__preview-error {
    padding: 24px 0;
    text-align: center;
    font-size: 13px;
    color: #f56c6c;
  }

  &__preview-head {
    padding-bottom: 12px;
    border-bottom: 1px solid #ebeef5;
  }

  &__preview-title {
    margin: 0 0 4px;
    font-size: 14px;
    font-weight: 600;
    color: #303133;
    word-break: break-all;
  }

  &__preview-meta {
    margin: 0;
    font-size: 12px;
    color: #909399;
  }

  &__preview-empty {
    padding: 24px 0;
    text-align: center;
    font-size: 13px;
    color: #909399;
  }

  &__group + &__group {
    margin-top: 16px;
  }

  &__group-label {
    margin: 16px 0 8px;
    padding-left: 8px;
    font-size: 12px;
    font-weight: 600;
    color: #409eff;
    border-left: 3px solid #409eff;
  }

  &__chunk {
    margin-bottom: 10px;
    padding: 10px 12px;
    background: #fafbfc;
    border: 1px solid #ebeef5;
    border-radius: 4px;
  }

  &__chunk-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin: 0 0 6px;
    font-size: 12px;
    font-weight: 600;
    color: #606266;
  }

  &__chunk-meta {
    font-weight: 400;
    color: #c0c4cc;
  }

  // 正文保留换行（原文里的段落结构对判断「切得对不对」很重要），
  // 长英文单词/URL 也要能断行，否则会把抽屉撑出横向滚动条
  &__chunk-body {
    margin: 0;
    font-size: 13px;
    line-height: 1.65;
    color: #303133;
    white-space: pre-wrap;
    word-break: break-word;
  }

  &__danger {
    color: #f56c6c;
  }

  &__empty {
    padding: 32px 0;
    text-align: center;
  }

  &__empty-icon {
    font-size: 40px;
    color: #c0c4cc;
  }

  &__empty-title {
    margin: 8px 0 4px;
    font-size: 14px;
    color: #606266;
  }

  &__empty-hint {
    margin: 0;
    font-size: 12px;
    color: #909399;
  }
}
</style>