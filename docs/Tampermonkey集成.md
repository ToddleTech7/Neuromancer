# 采集器与 CAVE：Tampermonkey 集成设计

> 状态：设计 / 待实施。CAVE 现场权限与 Tampermonkey 宿主兼容性仍需验证。
> 目标不是重写采集器，而是给现有采集器增加一个浏览器页面入口。

## 结论

保留现有采集器、会话包、回放和派生工具，只增加一个**宿主适配层**：

```text
CAVE 页面（浏览器自行登录）
        │
        ├─ Tampermonkey 注入（日常）
        ├─ CDP 注入（调试 / Agent）
        └─ --open 注入（本地 demo / 公开数据）
        │
        ▼
同一份 recorder + 面板 + 保存协议
        │
        ▼
cave_manual_server → 会话包 → 回放 / join / 派生
```

CAVE 登录由 CAVE/Keycloak 完成。采集器不接管登录、不保存密码、不读取 token。

## 兼容边界

| 层 | 现有实现 | CAVE 适配 |
|---|---|---|
| 登录 | 无 | 浏览器登录后进入数据页；脚本只在数据页启动 |
| 页面 | `/v/<token>/` | 增加 `/viewer/*`，覆盖 Spelunker |
| 采集核心 | `cave_recorder.user.js` | 不 fork，不复制 |
| 面板/保存 | `manual_boot.js` | 同一套按钮和 `POST /save` |
| 保存服务 | `cave_manual_server.py` | 同一端点；HTTPS 页面优先 GM_xhr |
| 包格式 | `manifest + client/*` | 不变；只增加可选字段 |
| 回放 | Python / Playwright | 默认只读；CAVE 私有源需 sandbox 或认证代理 |
| 写操作 | `edit.*` + op log | 只记录和 join，不对生产重放 |

现有会话包和回放工具保持兼容。CAVE 页面差异通过构建参数、`@match` 和运行配置解决，不在 recorder 内部写站点分支。

## Tampermonkey 开发方式

源文件：

```text
scripts/cave_gpu_patch.js
scripts/cave_recorder.user.js
scripts/manual_boot.js
scripts/build_manual_userscript.py
```

生成物：

```text
results/manual_userscript/cave_manual.user.js
```

规则：

- 只改源文件，不手改生成物；
- 每次构建重新生成 header、匹配规则和保存端点；
- `@run-at document-start` 保留，GPU 补丁必须先于 Neuroglancer 建 WebGL；
- 脚本必须自包含，不依赖 `@require` 或本地静态文件；
- 构建头里的匹配规则至少包含：
  ```text
  *://*/v/*
  *://*/viewer/*
  ```
- 保存端点通过构建参数写入，不在源码里写死；
- 不把 CAVE 账号、证书、token、hosts 写进脚本或仓库。

本地构建：

```bash
python3 scripts/build_manual_userscript.py \
  --sink http://127.0.0.1:8900/save \
  --out results/manual_userscript/cave_manual.user.js
```

安装：

```text
http://127.0.0.1:8900/install
```

## 必须先验证的宿主问题

Tampermonkey 的 `@grant` 会影响脚本运行上下文。当前构建头使用：

```text
@grant GM_xmlhttpRequest
```

需要验证：

- `window.viewer` 是否仍能访问；
- `window.__caveRecorder` 是否出现在页面；
- `canvas.toDataURL()` 是否仍能读到画面；
- 面板是否正常显示；
- 保存是否能到本地端点。

如果沙箱拿不到 `window.viewer`，方案优先级：

1. 把页面上下文脚本改为 `@grant none`；
2. 页面上下文负责 recorder，特权沙箱只负责保存桥接；
3. 保存桥接用 `postMessage` / 自定义事件传递序列化后的包；
4. 若仍不可行，日常入口退回 CDP，Tampermonkey 仅做轻量面板。

这是实施前的**决策门**，不能先假定 `GM_xmlhttpRequest` 和页面全局可以同时使用。

## 保存与登录

登录流程：

```text
打开 CAVE
→ 正常手机号/密码登录
→ 进入 Datasets
→ 打开实际数据页
→ Tampermonkey 在 /viewer/* 注入
→ 左下角出现「CAVE 采集」
```

登录页不匹配数据页规则，因此不会启动录制。

保存：

- HTTPS 页面保存到本地 HTTP 可能触发混合内容限制；
- Tampermonkey 优先 `GM_xmlhttpRequest`，绕过页面 CORS；
- 没有 GM 能力时退回 `fetch`；
- 两者都失败时，不能静默丢数据，应提供导出/下载回退；
- 保存失败要在面板和终端同时可见。

## 真实 CAVE 页面

已验证页面形态：

```text
https://<cave-host>/viewer/spelunker/#!<state>
```

关键事实：

- 顶层 `window.viewer === true`；
- 页面不是 `/v/<token>/`，旧匹配规则会漏注入；
- source 可能包含 `graphene://middleauth+https://...`；
- 分割表权限由 CAVE 服务端控制，`missing_permission` 不是采集器故障；
- 采集器只读，权限不足时仍可记录状态和输入，但无法得到有效分割像素。

## 读写记录

读：

- `viewer.state-change` / `viewer.checkpoint`
- `input.pointer` / `input.wheel` / `input.key`
- `keyframe.capture`
- `viewer.focus`
- `api.request` / `api.ack`

写：

- `edit.request` / `edit.ack`
- `edit.operation`
- `X-CAVE-Edit-*` 请求头
- before/after root ids
- 服务端 op log join

写记录是证据，不是执行计划。回放端不得把记录的写请求直接发给生产 CAVE。

## 回放隔离

回放分为：

| 模式 | 输入 | 数据 | 写操作 |
|---|---|---|---|
| 状态/视觉回放 | 不投输入 | 历史只读 | 禁止 |
| 输入回放 | pointer/wheel/key | 本地或只读快照 | 禁止 |
| 写行为回放 | 输入或操作 | sandbox 副本 | 只进 sandbox |

生产 CAVE 只允许前两种；写行为回放必须在独立 sandbox，可重置，不能连生产表。详细边界见 [回放隔离](回放隔离.md)。

## 后续工作

### 先验证宿主兼容性

- 用当前 CAVE 页面安装 Tampermonkey 版；
- 验证 `window.viewer`、`__caveRecorder`、面板、截图、保存；
- 记录失败时的控制台错误和页面路径；
- 决定 `@grant` / 页面上下文 / 保存桥接方案。

### 固化 CAVE 构建入口

- 构建头版本号与 `VERSION` / collector 版本同步；
- 保存端点、匹配规则、站点标签参数化；
- 增加构建自检，确认 `/viewer/*` 在生成物里；
- 安装页显示当前构建版本和保存端点。

### 采集稳定性

- 登录跳转/刷新后只创建一次 recorder；
- 多标签页时限制同时录制，避免会话混淆；
- 保存失败时有下载/导出回退；
- CAVE 权限不足时在面板显示明确状态，不把 403 当采集成功；
- 记录 `viewer` 重新创建后的恢复行为。

### 回放与数据

- 为 CAVE 私有源提供只读代理或 snapshots；
- 没有认证时明确标 `visual_replay_status=partial/unavailable`；
- 写行为回放使用 sandbox，演练结束清理；
- 保持现有包格式和测试兼容。

### 跨平台与验收

- macOS / Windows 各跑一遍安装、采集、保存、回放；
- 验证证书、hosts、隧道、代理不会影响采集；
- 不把内部 host、token、账号写入仓库；
- 现有 185 项测试保持通过，并补 Tampermonkey 构建和保存回退测试。
