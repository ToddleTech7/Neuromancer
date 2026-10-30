# results/ 存放规则（本地产物，不入 git）

`results/` 被 `.gitignore` 整体忽略。这里放**可重跑的产物**和**少量证据**。
清理原则：脚本能重跑的一律可删。

---

## 谁会往这里写

| 产物 | 谁写的 | 落在哪 |
|---|---|---|
| **会话包** | `nx.py collect` → 采集器 `/save` | `manual_sessions/<session_id>/` |
| **草稿** | 录制中每 30s 一次（增量） | `manual_sessions/_draft_<session_id>/` |
| **回放产物** | `nx.py replay` | `<包>/replay/<年月日_时分秒>/`（跟着会话走） |
| **派生** | 工作台「派生」tab / `cave_episodes.py` / `cave_join.py` | `<包>/derived/` |
| **验证产物** | 各 `verify_*.py` | `recorder_verify/` `action_replay/` `replay_reset/` `layer_fields/` `ui_providers/` `mouse_pick/` |
| **演示产物** | `demo_*.py` | `dataset_v0*/` `pipeline_demo*/` `joined/` `export_demo/` |
| **油猴脚本** | `build_manual_userscript.py` | `manual_userscript/cave_manual.user.js` |

会话包内部格式见 [数据格式](../docs/数据格式.md)。
最小可用包 = `manifest.json` + `client/events.jsonl`；
要能比像素再加 `client/shots/*.png` + `shots.jsonl`。

---

## 会话名怎么定

目录名就是会话名，**可以自定义**：

```bash
python3 scripts/nx.py collect --session-name 小鼠皮层_第3轮
# → results/manual_sessions/sess_小鼠皮层_第3轮/
```

| 规则 | 说明 |
|---|---|
| 默认 | `sess_<UTC 时间戳>_<5 位随机>`，如 `sess_2026-10-27T06-01-49-737Z_efvcm` |
| 自定义 | `--session-name <名字>`（也支持 URL 参数 `?cave_session_name=<名字>`） |
| 前缀 | 名字不以 `sess_` 开头会自动补上 |
| 保留字符 | 中文、字母、数字、`.` `_` `-` 原样保留 |
| 替换 | 空白 → `_`； `/ \ : * ? " < > | #` 与控制字符 → `_` |
| 同名 | 同一 `session_id` 重存会先备份旧包到 `.old_*`，再写新包；不同会话同名加 `__<年月日_时分秒>` 后缀 |
| 长度 | 上限 80 字符（目录名 120 字符） |

例：`--session-name '小鼠 皮层#3'` → `sess_小鼠_皮层_3`。

---

## 哪些能删

| 目录 | 能否删 |
|---|---|
| `<包>/replay/<ts>/` | 能（`nx.py replay` 重跑） |
| `dataset_v0*/`、`pipeline_demo*/`、`export_demo/`、`joined/` | 能（`demo_*.py` / `cave_join.py` 重跑） |
| `recorder_verify/`、`action_replay/`、`replay_reset/`、`layer_fields/`、`ui_providers/`、`mouse_pick/` | 能（各 `verify_*.py` 重跑） |
| `manual_userscript/` | 能（`build_manual_userscript.py` 重跑） |
| **`manual_sessions/`** | **不能删** —— 采集的唯一副本；`.old_*` 是重存前的旧包，`_trash/` 是删除的包，确认后可删 |
| **`cave_watch/ops.jsonl`** | **不能删** —— 从真实 CAVE 导出的 op log 样本，不可再生 |

`cave_watch/ops.jsonl` 在 2026-10-29 被误删过一次，导致 join 夹具 D 只能 SKIP；
恢复要从真实环境重新导出。

---

## 取证基线

早期真实会话包曾备份在 `archive/real_sessions_2026-10/`，该目录已按 2026-10-29 的决定删除；
历史数字与原因见 [开发记录](../docs/开发记录.md)。新的采集直接落 `manual_sessions/`，
不再往 `archive/` 复制。
