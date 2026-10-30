# 变更记录

## 1.0.0（2026-10-30）

首个可发布的单用户版本：本地采集、落包、崩溃恢复、看包、回放、派生、样本导出。

### 采集

- 浏览器内旁路记录 pointer/wheel/key/state/checkpoint/keyframe/note；`viewer.focus` 的 `page`/`picked` 写在顶层，旧包读取时自动展开嵌套字段。
- `--open` 自开浏览器；`--cdp` 接管已有 Chrome；`/save` 与 `/draft` 双端点。
- 结束并保存会等待最后一张关键帧落进包。
- 草稿 30 秒增量落盘；保存成功后删除草稿；同 session 重存在隐藏临时目录完整写入后备份旧包并整体切换。
- 超大 state 逐级降级（`source.state`/`segmentColors`/`segments`），标 `state_truncated`。
- 图层样式（alpha/着色/colorSeed）参与回放与逐字段比对。
- 冷启动分割层未加载的帧标 `pending_tiles`，不混进像素/内容结论。
- 默认只监听 `127.0.0.1`；`--host 0.0.0.0` 才开放局域网。
- 保存按钮有“保存中/失败可重试/宿主回退”状态；重复点击不会二次写包。
- 左下角采集面板默认收成快捷按钮条（打检查点/备注/结束并保存），可展开；支持拖动、缩放，布局存本地，下次恢复。

### 落包

- 目录包：`manifest.json` + `client/events.jsonl` + `client/shots/` + `notes.jsonl`。
- 事件/备注/索引 SHA256；PNG 本体 SHA256 与大小。
- `nx verify-pack` / `inspect_pack --verify` 可离线校验。
- 包发现只认正式包，跳过 `_draft_*`/`_trash`；`NM_SESSIONS` 全链生效。
- `/save` 与宿主直写把 `client_process` 标为 `real`。
- 删除会话默认移到 `_trash/`；`purge` 才真删。

### 回放

- 只喂输入的回放 + 逐检查点位置/选中/图层比对。
- 界面级工作台：真 NG + 时间轴 + 切包/切源 + 视频导出。
- state 恢复覆盖位置/尺度/朝向/布局/切片可见性/局部视口/切片厚度/图层/选中。
- uint64 段 ID 保持十进制精度；动作有效性地板；像素不可比单列。
- `--seek-t` 用目标时刻的 state 作为期望值；单锚点不再产生空 PASS。

### 派生

- 归一化、L2 片段、no-op 事实、hover 下界、尾段 idle。
- 样本卡导出（demo）与 schema 校验。

### 工具

- 回放工作台改为白底浅色界面；中间 Neuroglancer 舞台保持黑底，避免影响图像判断。
- 工作台鼠标点按录制视口缩放并夹在 iframe 内，不再因舞台比录制窗口小而跑到回放区域外。
- `replay_manual_session` 增加分区像素报告；分区从 `ui.state.ui.panels` 生成，2D/3D 面板开关后自动变化，旧包回退固定 2D 三块。
- 备注默认自动补一张 `moment=note` 关键帧（可关）；`shots.jsonl` 保留 `detail.note_id`，便于和备注关联。
- 派生结果增加 `hover_selections`/`selected_segments` 区分、备注的 `shot_id` 引用、源事件 SHA256 与 `derived/manifest.json`；join 输出改到包内 `derived/`。

- `nx collect / studio / replay / export / pack / verify-pack / doctor / export-csv / recover`。
- `export-csv` 导出 events/episodes/hover；`load_test_pack.py` 做合成大会话包压测。
- 公网兼容脚本默认 `viz.neurodata.io`，`CAVE_PUBLIC_URL` 可覆盖。
- `check_offline.sh` 包含单测、文档自检、join、钩子、图层、界面；`--browser` 加回放复位。
- 自检前会检查 pytest/jsonschema/pandas/caveclient/neuroglancer/playwright，缺依赖时给安装命令；`requirements-dev.txt` 含 pandas。
- `nx --version` 输出产品版本。

### 已知边界

- 真实 CAVE 现场尚未验证；`picked`/当前工具/chunk/LOD/历史版本不在 v1。
- 逐像素一致不是 v1 门；NCC 只作参考。
- 公开数据 demo 2026-10-30 复跑 40/40；冷启动 `pending_tiles` 与 `color_mismatch` 单列，不计入内容/逐通道断言。
- 同 session 重存改为“临时目录完整写入 → 备份旧包 → 整体切换”：写失败保留旧包，不产生半包；提交阶段两次 rename 之间仍有极短崩溃窗口。
- 真人会话整段回放：纯输入 4/6（末段 107.56nm >60）；分段 3/3（15.19nm）；`--reanchor-gesture` 整段 6/6，但报告标 `reanchored`。v1 默认是纯输入。
