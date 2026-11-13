// SFC / 模块语法自检：`npm run lint:sfc`
//
// 为什么需要它：vue-cli-service build 只报「编译失败」，报不出是哪个文件
// 缺 <script> 开标签这类结构问题（曾经真的踩过：页面模板被整个吞进脚本）。
// 这里逐个文件解析，出错立刻指出文件与原因。

const fs = require('fs')
const path = require('path')

// Vue 2.7 的 `vue/compiler-sfc`：
//   - `parse()` 虽有导出，但在这版里不可靠（不认 <template>），别用；
//   - 要用 `parseComponent(sfc, { pad })`，它给 JS 代码块补了位置信息，
//     返回 { template, script, styles, customBlocks }。
const { parseComponent } = require('vue/compiler-sfc')
const { parse: parseJS } = require('@babel/parser')

const ROOT = path.resolve(__dirname, 'src')

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}

function checkJS(code) {
  parseJS(code, { sourceType: 'module', plugins: ['dynamicImport'] })
}

let failed = 0
const files = walk(ROOT).filter((f) => /\.(vue|js)$/.test(f))

for (const file of files) {
  const rel = path.relative(ROOT, file).replace(/\\/g, '/')
  const code = fs.readFileSync(file, 'utf8')
  try {
    if (file.endsWith('.vue')) {
      const descriptor = parseComponent(code, { pad: 'line' })
      const hasScript = Boolean(descriptor.script)
      const hasTemplate = Boolean(descriptor.template)
      // App.vue 是纯模板 + 全局样式，没有 script 是正常的；页面组件必须有 script
      const scriptRequired = rel !== 'App.vue'
      if (scriptRequired && !hasScript) throw new Error('缺少 <script> 区块')
      if (!hasTemplate) throw new Error('缺少 <template> 区块')
      if (hasScript) checkJS(descriptor.script.content)
      console.log(`${rel} script=${hasScript} template=${hasTemplate} styles=${(descriptor.styles || []).length}`)
    } else {
      checkJS(code)
    }
    console.log(`${rel} OK`)
  } catch (error) {
    failed += 1
    console.error(`${rel} FAILED: ${error.message}`)
  }
}

console.log(`\n${files.length} files, ${failed} failed`)
process.exit(failed ? 1 : 0)