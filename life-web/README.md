# life-web

LifeAI 生活管理端（Vue 2.7 + Element UI）。开发时后端跑在 `127.0.0.1:8010`，
`vue.config.js` 把 `/api` 代理过去，所以前端不用写死端口。

## Project setup
```
npm install
```

### Compiles and hot-reloads for development
```
npm run serve
```
打开 http://localhost:8080/ ，左侧菜单进入各页面。

### Compiles and minifies for production
```
npm run build
```

## 页面

| 路由 | 页面 | 说明 |
|---|---|---|
| `/` | 首页 | 概览卡片 + 今日待办 + 收支 |
| `/tasks` | 待办 | 列表/筛选/新建抽屉 |
| `/expenses` | 消费 | 列表 + 汇总 + 图表 |
| `/ai` | AI 助手 | 会话列表 + 对话（A4 接口，纯聊天，无工具） |

`/ai` 的请求封装在 `src/api/ai.js`。聊天接口单独把超时放到 70 秒（模型慢），
其余接口沿用 `src/utils/request.js` 的全局 10 秒。发送失败时保留用户气泡并标红，
不伪造回复 —— 后端在失败时只会写一条 `status=failed` 的 assistant 行。

## Customize configuration
See [Configuration Reference](https://cli.vuejs.org/config/).
