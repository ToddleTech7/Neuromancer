# Neuromancer

把 CAVE 里的人工校对过程**采下来，再原样放回去**。

校对员照常操作；工具在旁路记录"看了什么、怎么看的、改了哪个段"，
之后能把任意一段在全新页面里重画出来，并逐检查点比对。

服务端 CAVE 操作日志是编辑的权威记录，本项目记录**过程**——两边按会话 / 时间戳 /
root id 关联。

---

## 三道活

打开 `python3 scripts/nx.py` 就是一个软件主页，四块：**开始 / 回放 / 看包 / 派生**。

| 干什么 | 在哪 | 产出 |
|---|---|---|
| **采集**一段真实操作 | 「开始」页填数据源 / 目录 / 名字 → 点开始采集 | 会话包（事件流 + 关键帧 PNG + 备注） |
| **回放**任意时刻的画面 | 「回放」tab | 可拖时间轴的工作台（真 Neuroglancer） |
| **派生**行为片段 / 关联 op log | 「派生」tab | `derived/*.jsonl` |

首页也可以直接看历史会话（事件数 / 时长 / 帧数），点「回放」切过去。
命令行照样能用：`nx.py collect` 是「开始采集」的同一条路。

工作台是单窗口三 tab：**回放 / 看包 / 派生**，点按钮切换。采集按钮在顶栏。

### 速查

| 想干什么 | 命令 / 动作 |
|---|---|
| 录一段 | `python3 scripts/nx.py collect` |
| 换个会话名 | `python3 scripts/nx.py collect --session-name 小鼠皮层_第3轮` |
| 换存放位置（一次） | `python3 scripts/nx.py collect --out ~/neuro_data` |
| 换存放位置（长期） | `export NM_SESSIONS=~/neuro_data`（collect / studio / pack / replay 全跟着走） |
| 打开工作台（最新包） | `python3 scripts/nx.py` |
| 打开指定包 | `python3 scripts/nx.py studio <包目录>` |
| **随便看一眼** | 双击包里 `打开.html`（轨迹 + 全部事件，无需服务） |
| 只重放不比对 | `python3 scripts/nx.py replay --seek-t 140` |
| 整段长会话（允许相机校正） | `python3 scripts/nx.py replay --reanchor-gesture` |
| 导出视频 | `python3 scripts/nx.py export --seek-t 140` |
| 看包里有什么 | `python3 scripts/nx.py pack` |
| 草稿丢了 | `python3 scripts/nx.py recover` |
| 环境不对 | `python3 scripts/nx.py doctor`（依赖/浏览器/目录/端口） |
| 校验包没坏 | `python3 scripts/nx.py verify-pack` |
| 导出表格 | `python3 scripts/nx.py export-csv` |

---

## 5 分钟上手

```bash
# 一次性依赖
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
# 要跑离线自检/单测，再装开发依赖：
# pip install -r requirements-dev.txt

# ① 采集：自动开一个已注入的浏览器窗口（不用装扩展）
python3 scripts/nx.py collect
#    窗口打开时**已经在录**，正常操作就行；左下角面板「打检查点 / 备注 / 结束并保存」
#    收工：先点「结束并保存」，再关窗口（服务会自己退出）
#    直接关窗口不保存，只会留草稿 —— 用 nx.py recover 找回

# ② 看：打开工作台（真 NG + 可拖时间轴 + 右侧数据面板）
python3 scripts/nx.py

# ③ 验：只靠输入事件把刚才那段重放一遍（逐段比对）
python3 scripts/nx.py replay
```

默认数据是公开 MICrONS minnie65（EM + 分割），体素 `4/4/40 nm`，
无需登录、无需 CAVE 权限。

**回放判定**：位置偏差 ≤ 60 nm、选中集 / 层 / 布局一致；
`rgb NCC` 是参考指标（>0.90 接近，不阻断），取捨见
[已定决策 §8](docs/已定决策.md)。

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

### 想用起来（三篇）

| 文档 | 回答什么 |
|---|---|
| [使用说明](docs/使用说明.md) | 三种部署怎么用、每种三步、产物在哪、怎么排错 |
| [功能清单](docs/功能清单.md) | 能力矩阵：哪些能力、验了没有 |
| [当前进度](docs/当前进度.md) | 每个功能做到哪一步（五档：已验 / 验一半 / 留口 / 计划 / 不做） |

### 想改代码（四篇）

| 文档 | 回答什么 |
|---|---|
| [设计说明](docs/设计说明.md) | 分层架构、五条不变量、模块边界、注入路线取舍 |
| [数据格式](docs/数据格式.md) | 包结构、事件与字段字典、怎么读、已知坑 |
| [docs/modules/](docs/modules/) | 六个模块各自的职责 / 接口 / 设计要点 / 验证 |
| [scripts/README.md](scripts/README.md) | 全部代码文件按角色分组 |

### 按问题查

[目录](docs/目录.md) 是完整索引：私有 NG 接入、需求映射、
已拍板的产品取舍、真机部署清单、逐日开发记录。
当前实现的 bug 与下一步优先级见 [评审报告](docs/评审报告.md)；
v1 发布门与现在能做的缺口见 [v1 发布清单](docs/v1发布清单.md)。

---

## 现状

单元测试 **153**、采集 demo **40/40**（2026-10-30 复跑；pending/color mismatch 单列）、动作回放 **16/16**、
真人会话回放 **分段 3/3**（整段 4/6；`--reanchor-gesture` 整段 6/6，标 reanchored）、recorder E2E **26/26**、join 夹具 **24/24**。

公开数据上已跑通完整链路：采集 → 落包 → 派生 → 回放 → 导出。
真实 CAVE 现场的那一次录制还在等（见
[DEPLOYMENT §4.5](docs/部署与验证.md) 的待验证清单）。

---

## 能力边界

| 项 | 现状 |
|---|---|
| 渲染过程（tile / chunk / mip 级） | 采不到：页面内看不到 Worker 的请求，URL 只到 shard+mip。已拍板不做，见 [已定决策 §7](docs/已定决策.md) |
| 工具切换 | 本构建 `viewer.tool` 不存在；改用 DOM 读工具栏状态 |
| 指针下的段 ID（`picked`） | 管道已通，真机值待验（[DEPLOYMENT §4.5 A1](docs/部署与验证.md)） |
| merge / split 提交 | 需要可写后端；公开数据只读 |
| 回放环境 | 必须能访问同一份数据源 |

---

## 开发

```bash
python3 scripts/check_offline.py            # 单测 + join + 钩子 + 复位（~30s）
python3 scripts/check_offline.py --browser  # 再加浏览器端自检
```

Agent 执行约定见 [AGENTS.md](AGENTS.md)。
