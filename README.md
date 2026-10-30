# Neuromancer

把 CAVE 里的人工校对过程**采下来，再原样放回去**。

校对员照常操作；工具在旁路记录"看了什么、怎么看的、改了哪个段"，
之后能把任意一段在全新页面里重画出来，并逐检查点比对。

服务端 CAVE 操作日志是编辑的权威记录，本项目记录**过程**——两边按会话 / 时间戳 /
root id 关联。

> 状态：`v1.0.0` 研究预览版。单用户、本机或 SSH 隧道使用；真实 CAVE 现场仍在验证。
> 采集器只读，不改变工作人员视野、选择或分割。

---

## 核心功能

运行 `python3 scripts/nx.py` 会打开软件主页，包含**开始 / 回放 / 看包 / 派生**。

| 功能 | 操作入口 | 产物 |
|---|---|---|
| **采集**真实校对操作 | 「开始」页填写数据源、目录和会话名 → 开始采集 | 会话包（事件流 + 关键帧 PNG + 备注） |
| **回放**指定时刻的画面 | 「回放」tab | 可拖时间轴的工作台（真 Neuroglancer） |
| **派生**行为片段、关联服务端 op log | 「派生」tab | `derived/*.jsonl` |

首页列出历史会话及其事件数、时长和帧数，可直接切到回放。
命令行照样能用：`nx.py collect` 是「开始采集」的同一条路。

工作台是单窗口，顶部切换**回放 / 看包 / 派生**；采集按钮在顶栏。

### 常用操作

| 操作 | 命令 / 动作 |
|---|---|
| 采集一段操作 | `python3 scripts/nx.py collect` |
| 指定会话名 | `python3 scripts/nx.py collect --session-name 小鼠皮层_第3轮` |
| 指定本次输出目录 | `python3 scripts/nx.py collect --out ~/neuro_data` |
| 设置会话根目录 | `export NM_SESSIONS=~/neuro_data`（collect / studio / pack / replay 都从该目录读写） |
| 打开工作台（最近会话） | `python3 scripts/nx.py` |
| 打开指定会话包 | `python3 scripts/nx.py studio <包目录>` |
| 离线打开轨迹与事件表 | 双击包里 `打开.html`（轨迹 + 全部事件，无需服务） |
| 回放到指定时刻（不比对） | `python3 scripts/nx.py replay --seek-t 140` |
| 整段长会话回放（可选相机校正） | `python3 scripts/nx.py replay --reanchor-gesture` |
| 导出回放视频 | `python3 scripts/nx.py export --seek-t 140` |
| 查看包结构、事件与完整性摘要 | `python3 scripts/nx.py pack` |
| 从草稿恢复未保存会话 | `python3 scripts/nx.py recover` |
| 检查运行环境与依赖 | `python3 scripts/nx.py doctor`（依赖/浏览器/目录/端口） |
| 校验事件、备注与图片完整性 | `python3 scripts/nx.py verify-pack` |
| 导出事件、片段与悬停表 | `python3 scripts/nx.py export-csv` |

---

## 快速开始

```bash
# 一次性依赖
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
# 要跑离线自检/单测，再装开发依赖：
# pip install -r requirements-dev.txt

# 采集：自动开一个已注入的浏览器窗口（不用装扩展）
python3 scripts/nx.py collect
#    窗口打开时**已经在录**，正常操作就行；左下角面板「打检查点 / 备注 / 结束并保存」
#    收工：先点「结束并保存」，再关窗口（服务会自己退出）
#    直接关窗口不保存，只会留草稿 —— 用 nx.py recover 找回

# 查看：打开工作台（真 NG + 可拖时间轴 + 右侧数据面板）
python3 scripts/nx.py

# 验证：只靠输入事件把刚才那段重放一遍（逐段比对）
python3 scripts/nx.py replay
```

默认数据是公开 MICrONS minnie65（EM + 分割），体素 `4/4/40 nm`，
无需登录、无需 CAVE 权限。

**回放判定**：位置偏差 ≤ 60 nm、选中集 / 层 / 布局一致；
`rgb NCC` 是参考指标（>0.90 接近，不阻断），取舍见
[已定决策：像素门](docs/已定决策.md)。

---

## 目录

```text
neuromancer/
  scripts/     全部代码（采集器 / 落包 / 派生 / 回放 / 验证 / 测试）
  schemas/     样本卡 JSON Schema
  docs/        设计、使用、数据格式（入口 docs/目录.md）
  data/        运行时数据（按需创建）
  results/     会话包与运行产物（不入库）
```

---

## 文档地图

### 使用与能力

| 文档 | 内容 |
|---|---|
| [使用说明](docs/使用说明.md) | 安装与启动、采集与回放、落包位置、故障排查 |
| [功能清单](docs/功能清单.md) | 功能范围、验证状态、能力边界 |
| [当前进度](docs/当前进度.md) | 每项功能的状态、证据和缺口 |

### 实现与维护

| 文档 | 内容 |
|---|---|
| [设计说明](docs/设计说明.md) | 分层、核心不变量、模块边界、注入方式 |
| [数据格式](docs/数据格式.md) | 包结构、事件与字段字典、读取方式、已知限制 |
| [docs/modules/](docs/modules/) | 各模块的职责、接口、设计要点与验证 |
| [scripts/README.md](scripts/README.md) | 代码文件的角色分组索引 |

### 按问题查找

[目录](docs/目录.md) 是完整索引：私有 NG 接入、需求映射、
产品取舍、真实环境部署清单、开发记录。
已知问题与下一步优先级见 [评审报告](docs/评审报告.md)；
发布检查项与当前缺口见 [v1 发布清单](docs/v1发布清单.md)。

---

## 验证状态

离线测试、文档自检、采集 demo、动作回放、真人会话分段回放、recorder E2E 与 join 夹具均有通过记录；验证结果见[目录](docs/目录.md)。
真人会话的默认纯输入整段回放受 wheel 动画漂移影响；按检查点分段可以稳定复现，`--reanchor-gesture` 提供可选的相机校正并在报告中标记。

公开数据上已跑通完整链路：采集 → 落包 → 派生 → 回放 → 导出。
真实 CAVE 现场录制仍待验证，检查项见
[部署与验证：待验证清单](docs/部署与验证.md)。

---

## 能力边界

| 项 | 现状 |
|---|---|
| 渲染过程（tile / chunk / mip 级） | 采不到：页面内看不到 Worker 的请求，URL 只到 shard+mip。已拍板不做，见 [已定决策：渲染过程](docs/已定决策.md) |
| 工具切换 | 本构建 `viewer.tool` 不存在；改用 DOM 读工具栏状态 |
| 指针下的段 ID（`picked`） | 管道已通，真机值待验（[部署与验证：待验证清单](docs/部署与验证.md)） |
| merge / split 提交 | 需要可写后端；公开数据只读 |
| 回放环境 | 必须能访问同一份数据源 |

---

## 开发

```bash
python3 scripts/check_offline.py            # 单测 + join + 钩子 + 复位（~30s）
python3 scripts/check_offline.py --browser  # 再加浏览器端自检
```

Agent 执行约定见 [AGENTS.md](AGENTS.md)。
