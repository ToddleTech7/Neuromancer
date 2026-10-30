# scripts/ 代码清单

53 个文件（47 个 Python + 3 个 JS + 2 个 shell + 本 README），按角色分组。**入口只有 7 个**（见文末），其余是库、验证或测试。

约定：脚本平铺、无包结构；跨文件依赖只允许"入口 → 库"，库之间不互相 import 业务逻辑。

小工具（`send` / `check` / `with_retry` / `do_GET` 等）在各脚本里自带一份：每个脚本都要能**独立运行、被单测单独 import**，抽公共库反而制造耦合。这是有意的重复。

## 1. 采集器（浏览器内，旁路）

| 文件 | 作用 |
|---|---|
| `cave_recorder.user.js` | 主采集器：输入/状态/关键帧/备注，暴露 `window.__caveRecorder`；fail-open，不拦不改 |
| `cave_gpu_patch.js` | document-start 的帧缓冲补丁（没有它读不到像素） |
| `manual_boot.js` | 手动会话面板：打检查点 / 结束并保存（只经 `__caveRecorder` 写） |

## 2. 启动与注入

| 文件 | 作用 |
|---|---|
| `recover_draft.py` | 把 `_draft_*` 草稿恢复成正式包（崩溃恢复） |
| `cave_manual_server.py` | **主入口**：起本地 NG（直连）+ 收会话包 + 三种注入模式（`--open` / `--cdp` / 油猴页） |
| `build_manual_userscript.py` | 把 recorder + 补丁 + 面板打成自包含 `.user.js`（含控制台版） |
| `cave_local_demo_server.py` | 最简本地 NG 环境（验证用，不带采集面板） |
| `demo_console_server.py` | 图形控制台：把 demo 命令变成按钮（可选，非必需） |
| `browser_util.py` | 浏览器启动辅助：优先 playwright chromium，回退系统 chrome |
| `nx_doctor.py` | 环境自检：Python/依赖/浏览器/目录/端口/最新包 |

## 3. 落包与关联

| 文件 | 作用 |
|---|---|
| `cave_session_pack.py` | 会话包 → 目录契约（append-only、白名单落盘） |
| `cave_join.py` | 会话 ↔ op log 三级匹配 + 时钟估计（0 假匹配） |
| `validate_samples.py` | 样本卡数据合约校验器（对 `schemas/`） |

## 4. 派生

| 文件 | 作用 |
|---|---|
| `cave_episodes.py` | 归一化时间线 + observation episode 切分 + no-op 分级 |
| `cave_derive.py` | 从 op log 派生"存活/演化"结局（正例质检） |
| `export_csv.py` | 会话包 → events/episodes/hover 三张 CSV |
| `load_test_pack.py` | 合成大会话包负载测试（明确 synthetic，不写 manual_sessions） |
| `cave_op_watcher.py` | 服务端 op tailer 原型（不依赖前端） |

## 5. 回放

| 文件 | 作用 |
|---|---|
| `inspect_pack.py` | **看包**：事件构成/锚点/关键帧状态/健康度（只读，`--json` 可机读） |
| `replay_state.py` | 视图指纹、字段比对、恢复点选择（纯逻辑，不开浏览器） |
| `replay_manual_session.py` | **真人会话回放**：默认只喂输入；`--reanchor-gesture` 可选 gesture-end 相机校正（标 reanchored） |
| `cave_replay_studio.py` | **界面级回放工作台**：真 NG 页面 + 可拖时间轴（跳 state 快照）+ 右侧数据面板 |
| `nx.py` | **统一入口**：`studio`（默认）/ `collect` / `replay` / `export` / `pack`，其余参数透传 |
| `cave_session_replay.py` | 会话包 → 只读 HTML：视角轨迹 + **鼠标热力图** + **注意点时间线** + 事件表 |
| `cave_replay_viewer.py` | 会话包 → 只读状态回放（R1）+ 写门禁 |

## 6. 演示

| 文件 | 作用 |
|---|---|
| `demo_manual.sh` | **一键入口**：`--open` 采集 / `--replay` 回放 / 默认远程三件套 |
| `demo_capture_episodes.py` | 公开数据：录制 → 派生 → 时间线 → 回放保真度（40 项断言） |
| `demo_full_pipeline.py` | 五段闭环 demo（前端 → 采集 → 打包 → 关联 → 回放） |
| `demo_export_samples.py` | 样本卡导出（含 contact sheet） |

## 7. 验证（会开浏览器 / 连外网）

| 文件 | 守什么 | 项数 |
|---|---|---|
| `verify_action_replay.py` | 脚本动作 + 键盘绑定重放，动作有效性地板 | 16 |
| `verify_cave_recorder_e2e.py` | 采集器端到端（事件/state/关键帧/绑定） | 26 |
| `verify_cave_recorder_public.py` | 公网 NG 站点兼容性 | 4 |
| `verify_cave_recorder_submit_hooks.py` | fetch/XHR 提交锚点观察 | 14 |
| `verify_layer_fields.py` | 图层 source 三种形态 + 样式字段（不开网，17 项） | 17 |
| `verify_ui_and_providers.py` | `ui.state` + provider 口 + 私有 localStorage + 提交头（19 项） | 19 |
| `verify_replay_fidelity.py` | 录制图 vs 按 state 重渲（NCC） | — |
| `verify_replay_reset.py` | 脏状态 → 干净状态复位 | 4 |
| `verify_cave_join.py` | 会话↔op log 关联夹具 | 24 |
| `check_offline.py` | 离线一键自检（跨平台版，不依赖 bash / `/tmp`） |
| `check_offline.sh` | 一键离线自检（可加 `--browser`） | — |
| `check_docs.py` | 文档死链/必需文档/旧文件名检查 | — |

## 8. 单元测试（纯离线，不联网）

文件名均为 `test_*.py`：

`test_action_effects` · `test_cave_derive` · `test_cave_join` · `test_cave_op_watcher` ·
`test_cave_replay` · `test_episodes` · `test_replay_pairs` ·
`test_replay_state` · `test_session_pack` · `test_validate_samples`（共 153 项）

## 9. 常用入口（只记这 7 个）

```bash
python3 scripts/nx.py                    # 回放工作台（默认，最新包）
python3 scripts/nx.py collect            # 采集（等同 demo_manual.sh --open）
python3 scripts/nx.py replay             # 逐条输入重放 + 比对
python3 scripts/nx.py export --seek-t 140 # 导出片段视频
python3 scripts/nx.py pack               # 包里存了什么
python3 scripts/nx.py verify-pack        # 校验包完整性（SHA256 + PNG）
python3 scripts/nx.py doctor             # 环境自检
python3 scripts/nx.py export-csv         # 导出 CSV
python3 scripts/nx.py recover            # 崩溃后从草稿恢复
bash scripts/check_offline.sh            # 离线自检
```

`demo_manual.sh` / `inspect_pack.py` / `replay_manual_session.py` / `cave_replay_studio.py`
仍然可以单独用（nx.py 只是分派，不改变它们的行为）；`demo_capture_episodes.py` 是公开数据全链 demo。
其余脚本都是这些入口背后的库，或专项验证，按需单独跑。
