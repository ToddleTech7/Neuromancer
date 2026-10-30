// ==UserScript==
// @name         CAVE Recorder（Neuromancer）
// @namespace    neuromancer.local
// @version      0.9.0
// @description  只读记录 Neuroglancer 会话：视角状态流/选择/鼠标/滚轮/按键/图层/关键帧/备注 + 状态与缺口 + 提交锚点（只观察 CAVE API 调用与回执），一键导出会话包。不修改任何请求/响应。
// @match        *://*/v/*
// @match        http://localhost:*/*
// @match        http://127.0.0.1:*/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==
// 说明：
// - 自建前端上线后，把部署域名加进上面的 @match 列表即可。
// - 本脚本设计为可同时被 Tampermonkey 与自动化浏览器（Playwright）注入；
//   暴露 window.__caveRecorder = { start, stop, export, exportJSON,
//   captureShot, addNote, status } 供程序化控制。
// - 失败不影响页面（fail-open）；只读，不写入远端。
// - 截图按钮：成功后自动下载 PNG（截图与状态流分开保存；会话包只含 meta）。
// - multicut 字段名为防御式探测（multicut.sources/sinks），以实际前端为准。
// - v0.6 变更（与 v0.5 输出兼容，均为增量字段）：
//   * seq/缺口状态 + 离散 collector_status（可见/焦点/在线），用于区分“在想”与“采集器中断”；
//     **1 Hz heartbeat 已废除（2026-10-26 用户决定）**：它无法区分“人走了”与
//     “采集器死了”（recorder 自己挂了，心跳也停），且下游无一个消费者。
//     现在靠三样东西：① seq 连续性（缺口即证据）；② collector_status 只在状态真变时记；
//     ③ 需要硬证据时由宿主（Playwright/扩展）轮询 __caveRecorder.status() 记外部时间戳。
//   * 视角字段修复：dimensions 在 NG 2.41 是 {x:[4e-9,'m'],...} 对象（不是数组），
//     旧版 Array.from 只得 []；鼠标位置是 [0]/[1]/[2] 索引对象（旧版得 []）；
//     2D 朝向字段是 crossSectionOrientation（旧版只读 projectionOrientation）；
//   * nm/px 由 crossSectionScale × 体素 nm 推出（本地 NG 2.41 实测 1/6/12/48 四档
//     线性吻合），带 derived_method 标记；
//   * 图层记录 source/visible/opacity（QF05/QF06 的最小集）；
//   * state_checkpoint（全量 viewer state，R1 回放/seek 的基准）；
//   * client_request_id（本地生成）与提交前后自动关键帧 + capture_status。

(function () {
  'use strict';
  if (window.__caveRecorder && window.__caveRecorder.loaded) return;

  const SCHEMA = 'cave_recorder.session.v0.2';
  const COLLECTOR_VERSION = '0.9.0';
  const SCHEMA_BASE = 'cave_recorder.session.v0.1';   // 旧读者兼容标记
  // 事件名对齐：《CAVE_前端_Neuroglancer_需求清单_v0.4(3)》RF01–RF15。
  // 旧名写进 legacy_type（一个版本后删），下游用 cave_session_pack.ctype() 统一读。
  // 不改名的代价是“做了也对不上表”—— v0.6 就是这个状态。
  const EV = {
    session_start: 'session.start',
    session_stop: 'session.end',
    viewer_attached: 'viewer.attach',
    state_sample: 'viewer.state-change',
    state_checkpoint: 'viewer.checkpoint',
    selection_sample: 'selection.change',
    pointer: 'input.pointer',
    wheel: 'input.wheel',
    key: 'input.key',
    mouse_sample: 'viewer.focus',
    multicut_change: 'draft.update',
    ui_state: 'ui.state',
    private_journal: 'private.journal',
    ext_snapshot: 'ext.snapshot',
    gesture: 'viewport.gesture',
    keyframe: 'keyframe.capture',
    note: 'evidence.bookmark',
    collector_status: 'collector.status',
    heartbeat: 'collector.heartbeat',
    api_request: 'api.request',
    api_response: 'api.ack',
    api_operation_id: 'edit.operation',
    api_capability: 'api.capability',
    api_error: 'api.error',
  };
  // 能力查询 URL：看到这些就把响应体存下来（组 Undo 到底有没有，只能看响应）
  const CAPABILITY_RE = /undo\/capabilities|edit_groups?\b|\/capabilities\b/i;
  // 写请求单独走 edit.request / edit.ack（RF08），读请求留在 api.*：
  // 二者混在一起会让“提交链路”无法单独对表。
  function canonType(type, data) {
    if (type === 'api_request' && data && data.write) return 'edit.request';
    if (type === 'api_response' && data && data.write) return 'edit.ack';
    return EV[type] || type;
  }
  // 原始输入采样：D10（日志粒度）未决 → 做成**可配置**而不是写死。
  //   move_sample_ms = 0      → 全量（回放最保真，事件量约 ×7）
  //   move_sample_ms = 50     → 端点全量 + move 每 50ms 一条（默认）
  // 三种设法，优先级从高到低：
  //   R.setConfig({ move_sample_ms: 0 })      控制台/工作台（运行中改也生效）
  //   URL 加 ?cave_move_ms=0                  不想开控制台时
  //   默认 50
  // 生效值**写进 session.start.config 与 capabilities()**，下游据此判断这份数据
  // 的粒度；不要拿“默认值”去假没写的包。改轮毂间隔不影响已有数据语义。
  // 默认 50ms；keyframe_on_checkpoint 默认关（每 30s 一张图，一小时≈120 MB）。
  // 打开它的唯一理由是**回放保真度自检**：只有每个 checkpoint 都有图，
  // 才能逐个“恢复后截图 vs 当时那张图”做像素对比。
  const CONFIG_DEFAULT = { move_sample_ms: 50, wheel_burst_gap_ms: 400,
                           // 状态轮询间隔（ms）；0 = 只靠订阅，不轮询。
                           // 订阅（semantic_watch）负责离散变化（选中集/层/布局）即时记，
                           // 轮询只做兜底：订阅丢了（viewer 重建）也不至于整段空白。
                           // 精度优先：200ms 轮询 + 50ms 去抖（订阅仍是主力）
                           state_interval_ms: 200,
                           semantic_watch: true,
                           semantic_debounce_ms: 50,
                           // 语义关键帧：缩放跨档/图层可见性/选中集变化时各拍一张
                           //（手动 + 提交前后 + 检查点拍照仍然保留）
                           semantic_keyframe: true,
                           // 停留：视图中心 + 选中集 + 可见层都不变超过这么久 → 拍一张
                           dwell_ms: 5000,
                           // 界面状态（面板尺寸/工具栏/通知）：像素只算 canvas，
                           // 但这些「非像素」的东西要记（2026-10-29 用户拍板）；主题不记。
                           // 单独节拍：1s（getBoundingClientRect 会强制 layout，不能跟 200ms 跑）
                           ui_state: true,
                           ui_interval_ms: 1000,
                           // 私有本机记录（localStorage 里的提交保护/batch 进度）
                           private_journal: true,
                           journal_interval_ms: 5000,
                           // 会话名：给了就用它，否则自动 sess_<时间>_<随机>
                           session_name: null,
                           // 草稿：每 30s 把“新事件”增量 POST 到 /draft，
                           // 崩溃/误关最多丢最后一个间隔；0 = 关闭
                           draft_ms: 30000,
                           // 事件上限（设置面板可调；默认 50 万 ≈ 10 小时）
                           max_events: 500000,
                           keyframe_on_checkpoint: false,
                           // 复刻（R2）的关键：每个状态变化都附完整 state。
                           // 实测约 0.5 KB/次（比摘要还小），一小时 1–2 MB。
                           full_state_on_change: true,
                           // L1：拍照前按**内容**等一次（不是固定睡眠）。
                           // 分割层是懒加载的，刚跳转就拍会拍到一帧没有彩色叠加的
                           // 灰图（实测彩色占比 0.006，而同一时刻画完是 0.72）。
                           // 三个参数随 session.start.config / manifest.config 一起走，
                           // 下游读到的是“这份数据当时按什么规则算画完”。
                           keyframe_paint_ms: 3000,
                           keyframe_paint_colored: 0.05,
                           keyframe_paint_stable_eps: 0.002 };

  function clampInt(v, lo, hi) {
    // 注意 Number(null) === 0、Number('') === 0：
    // 不先把“没给值”挡掉，缺参数会被当成 0，
    // move_sample_ms 会悄悄从 50ms 变成“全量”，而没人看得出来。
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    if (!isFinite(n)) return null;
    return Math.max(lo, Math.min(hi, Math.round(n)));
  }

  // 设置面板写 localStorage，下次打开自动生效；URL 参数仍可临时覆盖。
  function readSettingsFromStorage() {
    const out = {};
    try {
      const raw = localStorage.getItem('cave_settings_v1');
      if (!raw) return out;
      const s = JSON.parse(raw) || {};
      const m = clampInt(s.move_sample_ms, 0, 1000);
      if (m !== null) out.move_sample_ms = m;
      const si = clampInt(s.state_interval_ms, 0, 5000);
      if (si !== null) out.state_interval_ms = si;
      const sd = clampInt(s.semantic_debounce_ms, 0, 1000);
      if (sd !== null) out.semantic_debounce_ms = sd;
      const me = clampInt(s.max_events, 1000, 1000000);
      if (me !== null) out.max_events = me;
      if ('semantic_watch' in s) out.semantic_watch = !!s.semantic_watch;
      if ('semantic_keyframe' in s) out.semantic_keyframe = !!s.semantic_keyframe;
      const dw = clampInt(s.dwell_ms, 0, 600000);
      if (dw !== null) out.dwell_ms = dw;
      const god = clampInt(s.draft_ms, 0, 600000);
      if (god !== null) out.draft_ms = god;
      if ('keyframe_on_checkpoint' in s) {
        out.keyframe_on_checkpoint = !!s.keyframe_on_checkpoint;
      }
    } catch (e) { /* 存储坏了就当没设置 */ }
    return out;
  }

  function readConfigFromUrl() {
    const out = {};
    try {
      const q = new URLSearchParams(location.search);
      const v = clampInt(q.get('cave_move_ms'), 0, 1000);
      if (v !== null) out.move_sample_ms = v;
      const g = clampInt(q.get('cave_wheel_gap_ms'), 50, 5000);
      if (g !== null) out.wheel_burst_gap_ms = g;
      const si = clampInt(q.get('cave_state_ms'), 0, 5000);
      if (si !== null) out.state_interval_ms = si;
      const sw = q.get('cave_watch');
      if (sw !== null && sw !== '') {
        out.semantic_watch = !(sw === '0' || sw === 'false');
      }
      const skf = q.get('cave_semantic_kf');
      if (skf !== null && skf !== '') {
        out.semantic_keyframe = !(skf === '0' || skf === 'false');
      }
      const dw = clampInt(q.get('cave_dwell_ms'), 0, 600000);
      if (dw !== null) out.dwell_ms = dw;
      const ui = q.get('cave_ui');
      if (ui !== null && ui !== '') {
        out.ui_state = !(ui === '0' || ui === 'false');
      }
      const pj = q.get('cave_journal');
      if (pj !== null && pj !== '') {
        out.private_journal = !(pj === '0' || pj === 'false');
      }
      const sn = q.get('cave_session_name');
      if (sn) out.session_name = sn;
      const dm = clampInt(q.get('cave_draft_ms'), 0, 600000);
      if (dm !== null) out.draft_ms = dm;
      const kf = q.get('cave_kf');
      if (kf !== null && kf !== '') {
        out.keyframe_on_checkpoint = (kf === '1' || kf === 'true');
      }
      const fsc = q.get('cave_full_state');
      if (fsc !== null && fsc !== '') {
        out.full_state_on_change = !(fsc === '0' || fsc === 'false');
      }
      const kpm = clampInt(q.get('cave_kf_paint_ms'), 0, 10000);
      if (kpm !== null) out.keyframe_paint_ms = kpm;
      const kpc = q.get('cave_kf_colored');
      if (kpc !== null && kpc !== '') {
        const f = Number(kpc);
        if (isFinite(f)) out.keyframe_paint_colored = Math.max(0, Math.min(1, f));
      }
    } catch (e) { /* 老浏览器：保持默认 */ }
    return out;
  }
  const R = {
    loaded: true, recording: false, sessionId: null, t0: null,
    events: [], notes: [], shots: [], errors: [], counters: {},
    viewer: null, timers: [],
    last: { state: null, sel: null, mouse: null, multicut: null,
            ui: null, journal: null },
    lastUiAt: 0, lastJournalAt: 0,
    // 外部扩展口（P2-* 统一入口）：name -> {fn, sig, err, errAt, n}。
    // 没注册就是空对象，采样时零开销。
    providers: Object.create(null),
    // 全量模式下 move 约 60 条/秒（真人拖拽），200k 可撑≈55 分钟；
    // 超了会 bump('dropped') 并在 counts 里看得见，不会静默丢。
    MAX_EVENTS: 200000, MAX_CHECKPOINTS: 1000,
    stateId: 0, shotSeq: 0, seq: 0, overflow: null, draftSeq: 0,
    // 最近一次入流 state 的 sha 与时刻：拍照时用来判断“这张图属于流里已有的 state，
    // 还是属于一个没入流的新状态”。后者要随 shot 存完整 state，否则那一刻就永久丢了。
    lastStateSha: null, lastStateAt: 0,
    // 上一张关键帧的画面指纹 + 它的 state：下一张据此判断“画面到底变了没”
    // （只看彩色占比会把“上一帧的旧画面”当成已画完）。
    lastShotThumb: null, lastShotStateSha: null,
    lastShotThumbArr: null,
    hidden: false, focused: true, online: true,
    lastStatusSig: null,
    lastCheckpointWall: 0, initialCheckpoint: null,
    pendingWrites: [],          // 未完成的写请求（request_id → 元信息）
    gaps: [],
    gesture: null,              // 当前拖拽手势
    lastMoveWall: 0,
    wheelBurst: null,           // 当前滚轮 burst
    nAcked: 0,                  // RF14：本地队列已确认计数（本地模式永为 0）
  };
  // 采样配置（见上）：运行时可改，改了就发 collector.config
  R.config = Object.assign({}, CONFIG_DEFAULT,
                           readSettingsFromStorage(), readConfigFromUrl());
  R.MAX_EVENTS = R.config.max_events;
  window.__caveRecorder = R;

  // 只读测试钩子：验证脚本用它直接跑纯函数（不需要真实 viewer / 外网）。
  // 只是函数引用，不暴露可变状态；不改变采集行为。
  R._internals = {
    layerInfo: layerInfo, layerStyle: layerStyle, sourceInfo: sourceInfo,
    nmPerPx: nmPerPx, extractViewpoint: extractViewpoint,
    uiState: uiState, pollProviders: pollProviders,
  };

  // 防手滑：还录着且有点数据时关窗口/刷新，让浏览器弹一次确认。
  // 注意这只防“误关”，不防崩溃；崩溃保护需要草稿落盘（见 GAPS）。
  window.addEventListener('beforeunload', function (e) {
    if (R.recording && R.events.length > 0) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  const wall = () => Date.now();
  const mono = () => (window.performance && performance.now)
    ? performance.now() : wall();
  function bump(k) { R.counters[k] = (R.counters[k] || 0) + 1; }

  // 丢弃也要留证据：条数满了写不进事件流，只能记在 R.overflow，
  // 随 session.end / dump 一起落盘（否则用户只看得到“事件停住”）。
  function noteOverflow(kind) {
    if (!R.overflow) R.overflow = {};
    const o = R.overflow[kind] ||
      (R.overflow[kind] = { total: 0, first_t_wall: null, limit: 0 });
    o.total += 1;
    if (o.first_t_wall == null) o.first_t_wall = wall();
    o.limit = kind === 'events' ? R.MAX_EVENTS : R.MAX_CHECKPOINTS;
  }

  function emit(type, data) {
    try {
      if (R.events.length >= R.MAX_EVENTS) {
        // 满了也要占号：否则下游按 seq 连续性看会误以为“没中断”
        R.seq += 1;
        bump('dropped');
        noteOverflow('events');
        return;
      }
      const canon = canonType(type, data);
      const ev = Object.assign(
        { seq: ++R.seq, t_wall: wall(), t_mono: mono(),
          type: canon },
        data || {});
      if (canon !== type) ev.legacy_type = type;
      R.events.push(ev); bump(canon); updateUI();
    } catch (e) { R.errors.push(String(e)); }
  }

  /* ---------- viewer 检测 ---------- */
  function tryViewer() {
    try {
      const v = window.viewer;
      if (v && v.state && typeof v.state.toJSON === 'function') {
        if (R.viewer !== v) {
          R.viewer = v;
          emit('viewer_attached', { url: location.href });
          updateUI();
        }
        if (R.config.semantic_watch && !attachStateWatch()
            && R.config.state_interval_ms === 0) {
          // 旧版 NG 没有可订阅的信号：只靠订阅会一条状态都记不到。
          R.config.state_interval_ms = 600;
          resetSampleTimer();
          emit('collector_status', {
            degrade: 'watch_unavailable', state_interval_ms: 600,
            state_id: R.stateId, gap_reason: null,
          });
        }
        return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  /* ---------- 提取 ---------- */
  function norm(a) {
    try {
      return a == null ? null
        : Array.from(a).map(x => (typeof x === 'number'
          ? +x.toFixed(4) : x));
    } catch (e) { return null; }
  }
  function num(x) { return typeof x === 'number' ? +x.toFixed(6) : x; }

  // NG 2.41 实测：state.dimensions 形如 {x:[4e-09,'m'], y:[4e-09,'m'],
  // z:[4e-08,'m']}（单位 m，不是数组）。旧版按数组处理，得到 [] 从而丢失
  // 全部坐标换算依据。
  const UNIT_NM = { m: 1e9, mm: 1e6, um: 1e3, '\u00b5m': 1e3, nm: 1, pm: 1e-3 };
  function toNm(scale, unit) {
    const f = UNIT_NM[String(unit || 'nm')];
    return (typeof scale === 'number' && f) ? scale * f : null;
  }
  function extractDimensions(raw) {
    try {
      if (!raw) return null;
      if (Array.isArray(raw)) {
        return { names: null, scales_nm: raw.map(v => num(v)) };
      }
      const names = Object.keys(raw);
      const scales = names.map(k => {
        const e = raw[k];
        return Array.isArray(e) ? toNm(e[0], e[1]) : toNm(e, 'nm');
      });
      return { names: names, scales_nm: scales };
    } catch (e) { return null; }
  }
  function viewportInfo() {
    let w = null, h = null;
    try {
      const cs = Array.from(document.querySelectorAll('canvas'))
        .filter(x => x.width > 100 && x.height > 100)
        .sort((a, b) => (b.width * b.height) - (a.width * a.height));
      if (cs.length) {
        w = cs[0].clientWidth || cs[0].width;
        h = cs[0].clientHeight || cs[0].height;
      }
    } catch (e) { /* ignore */ }
    return { css_px: [w, h], dpr: num(window.devicePixelRatio || 1) };
  }

  /* ---------- 界面状态（canvas 以外的东西：面板/工具栏/通知） ---------- */
  // 用户拍板（2026-10-29）：**像素只算 canvas**，但工具/侧栏/通知这些**状态要记**；
  // 主题（dark/light）无所谓，不记。
  // 为什么单独立事件而不再塞进 viewpoint：这些变化与“看哪里”无关，
  // 而且开面板/切工具时 position 可能完全不变——摆进 sig 会把它们当成“没变化”。
  function uiState() {
    const out = {};
    try {
      // 渲染面板：真实 split view 时每个 pane 的尺寸/位置都不一样（补多 panel 缺口）
      const ps = [];
      const els = document.querySelectorAll('.neuroglancer-rendered-data-panel');
      for (let i = 0; i < els.length; i++) {
        const r = els[i].getBoundingClientRect();
        ps.push([Math.round(r.x), Math.round(r.y),
                 Math.round(r.width), Math.round(r.height)]);
      }
      if (ps.length) out.panels = ps;
    } catch (e) { /* ignore */ }
    try {
      const dd = document.querySelector('.neuroglancer-tool-palette-dropdown');
      const st = document.querySelector('.neuroglancer-annotation-tool-status');
      const tool = {};
      if (dd) {
        tool.palette_open = true;
        const items = dd.querySelectorAll(
          '.neuroglancer-tool-palette-dropdown-canned-item');
        tool.items = [];
        for (let i = 0; i < items.length && i < 20; i++) {
          tool.items.push((items[i].textContent || '').trim().slice(0, 60));
        }
      } else {
        tool.palette_open = false;
      }
      if (st) {
        const t = (st.textContent || '').trim().slice(0, 120);
        if (t) tool.status = t;
      }
      if (Object.keys(tool).length) out.tool = tool;
    } catch (e) { /* ignore */ }
    try {
      // 通知：优先用约定的 data-cave-notify（对方肯加就是结构化数据），
      // 否则只能读已知容器；两个都没有就什么都不记（不猜）。
      const ns = [];
      const sel = '[data-cave-notify], .neuroglancer-status-overlay,'
        + ' .neuroglancer-notification';
      const els = document.querySelectorAll(sel);
      for (let i = 0; i < els.length && ns.length < 10; i++) {
        const text = (els[i].textContent || '').trim().slice(0, 200);
        if (!text) continue;
        const n = { text: text };
        const lv = els[i].getAttribute('data-level');
        if (lv) n.level = lv;
        ns.push(n);
      }
      if (ns.length) out.notices = ns;
    } catch (e) { /* ignore */ }
    return out;
  }
  function pollUi() {
    if (!R.config.ui_state) return;
    // UI 采样比状态慢：getBoundingClientRect() 会强制同步 layout，
    // 200ms 跑一次在 NG 这种重渲染页面上是白花钱。面板尺寸变化本来就是低频
    // （拖分隔条/切布局），1s 足够；tool/notices 也一起走这个节拍。
    const now = wall();
    if (R.lastUiAt && (now - R.lastUiAt) < (R.config.ui_interval_ms || 1000)) return;
    R.lastUiAt = now;
    let u;
    try { u = uiState(); } catch (e) { return; }
    const sig = JSON.stringify(u);
    if (sig === R.last.ui) return;
    R.last.ui = sig;
    emit('ui_state', { ui: u, state_id: R.stateId });
  }

  /* ---------- 外部扩展口：P2-* 统一入口 ---------- */
  // 采不到的东西（Brush 候选/局部 Undo/合并计划/in-flight…）不在 recorder 里猜，
  // 而是留一个**只读函数**的口：谁有能力读到私有状态（前端 hook / 油猴 / CDP），
  // 谁就调 R.registerProvider(name, fn)。注册了才采，没注册就一点开销都没有。
  // 约定：fn 返回可 JSON 序列化的对象；返回 undefined 表示“这次没值”。
  function registerProvider(name, fn) {
    if (!name || typeof fn !== 'function') return false;
    R.providers[name] = { fn: fn, sig: null, err: null, errAt: 0, n: 0 };
    emit('collector_status', { provider: name, provider_attached: true });
    return true;
  }
  function unregisterProvider(name) {
    if (!R.providers[name]) return false;
    delete R.providers[name];
    emit('collector_status', { provider: name, provider_detached: true });
    return true;
  }
  /* ---------- P1-1：私有本机记录（localStorage） ---------- */
  // 私有前端把「提交保护 / batch 进度」存在 localStorage，键名带 graphUrl：
  //   neuroglancer-graph-edit:<graphUrl>
  //   neuroglancer.brush-merge.v1:<graphUrl>
  // 这是他们**唯一持久化**的编辑过程记录，不进 viewer state —— 只能从这里读。
  // 如实边界：键名与格式属私有实现，换版本可能变；读不到就不记（不猜）；
  // 局部值过大只存前 2000 字 + sha，不把整个 localStorage 抄进事件流。
  const JOURNAL_KEY_RE = /^neuroglancer[.-](graph-edit|brush-merge)/;
  const JOURNAL_MAX_CHARS = 2000;
  function pollJournal() {
    if (!R.config.private_journal) return;
    // 本机记录变化很慢（只在提交/批次推进时写），5s 一次足够。
    const now = wall();
    if (R.lastJournalAt &&
        (now - R.lastJournalAt) < (R.config.journal_interval_ms || 5000)) {
      return;
    }
    R.lastJournalAt = now;
    const entries = [];
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || !JOURNAL_KEY_RE.test(k)) continue;
        let raw = null;
        try { raw = localStorage.getItem(k); } catch (e) { continue; }
        if (raw == null) continue;
        const e = { key: k, bytes: raw.length, sha: fnv1a(raw) };
        if (raw.length <= JOURNAL_MAX_CHARS) e.value = raw;
        else e.value_head = raw.slice(0, JOURNAL_MAX_CHARS);
        entries.push(e);
      }
    } catch (e) { return; }   // localStorage 被禁用（隐私模式）等
    if (!entries.length) return;
    const sig = entries.map(function (x) { return x.key + ':' + x.sha; })
      .join('|');
    if (sig === R.last.journal) return;
    R.last.journal = sig;
    emit('private_journal', { entries: entries, state_id: R.stateId });
  }

  function pollProviders() {
    // 自动挂：对方按 docs/私有扩展接入.md §3.1 暴露的只读快照
    if (!R.providers.private && typeof window.__caveSnapshot === 'function') {
      registerProvider('private', function () { return window.__caveSnapshot(); });
    }
    for (const name in R.providers) {
      const p = R.providers[name];
      // 出错后 30s 内不重试：否则每 200ms 刷一条错误，把事件流冲掉
      if (p.errAt && (wall() - p.errAt) < 30000) continue;
      let val;
      try { val = p.fn(); }
      catch (e) {
        p.err = String(e); p.errAt = wall();
        emit('collector_status', { provider: name, provider_error: String(e) });
        continue;
      }
      if (val === undefined) continue;
      let sig;
      try { sig = JSON.stringify(val); } catch (e) { continue; }
      if (sig === p.sig) continue;
      p.sig = sig; p.n += 1;
      // 单个 provider 一次最多 8 KB：防某个私有快照把事件流撑爆
      emit('ext_snapshot', {
        provider: name, value: val, value_bytes: sig.length,
        truncated: sig.length > 8192, state_id: R.stateId,
      });
    }
  }
  // 图层的「样子」（回放要一模一样就得有它）+ 数据源身份。
  // 只取有值的字段，不塞空值，避免每条 state_sample 膨胀。
  const LAYER_STYLE_KEYS = ['baseSegmentColoring', 'baseSegmentHighlighting',
    'selectedAlpha', 'notSelectedAlpha', 'objectAlpha', 'colorSeed'];
  function layerStyle(l) {
    const out = {};
    for (const k of LAYER_STYLE_KEYS) {
      const v = l[k];
      if (v === null || v === undefined) continue;
      out[k] = (typeof v === 'number') ? num(v) : v;
    }
    if (l.segmentDefaultColor != null) {
      out.segmentDefaultColor = String(l.segmentDefaultColor);
    }
    // segmentColors 是 Map/对象，可能几百项：只记条目数，不记本体
    const sc = l.segmentColors;
    if (sc) {
      const n = (typeof sc.size === 'number') ? sc.size
        : (typeof sc === 'object' ? Object.keys(sc).length : 0);
      if (n) out.segmentColors_count = n;
    }
    return out;
  }
  // 数据源身份：graphene 的 source 是对象 {url, state}，旧版只认 string/数组，
  // 对象直接返回 null —— 图层摘要里就只剩名字，看不到是哪个 graph。
  function sourceInfo(src) {
    const one = (s) => {
      if (typeof s === 'string') return { url: s };
      if (!s || typeof s !== 'object') return null;
      const o = {};
      if (s.url != null) o.url = String(s.url);
      if (s.state != null) o.has_state = true;
      return (o.url || o.has_state) ? o : null;
    };
    if (Array.isArray(src)) {
      return src.map(one).filter(Boolean);
    }
    const o = one(src);
    return o || null;
  }
  function layerInfo(layers) {
    try {
      return (layers || []).map(l => {
        const info = {
          name: l.name || null,
          type: l.type || null,
          visible: (l.visible !== false),
          opacity: (typeof l.opacity === 'number') ? num(l.opacity) : null,
          source: (typeof l.source === 'string') ? l.source
            : (Array.isArray(l.source) ? l.source.slice(0, 2) : null),
          segments_count: (l.segments || []).length,
        };
        const si = sourceInfo(l.source);
        if (si) info.source_detail = si;
        const st = layerStyle(l);
        if (Object.keys(st).length) info.style = st;
        return info;
      });
    } catch (e) { return []; }
  }
  // nm/px 推导：实测 nm_per_px(轴) = crossSectionScale × 该轴体素 nm
  // （本地 NG 2.41，cross=1/6/12/48 四档线性吻合）。保留 derived_method 字段，
  // 私有 fork 必须复测后才能当精确值使用。
  const NM_PER_PX_METHOD =
    'crossSectionScale*dims_scale_nm（本地 NG 2.41 实测，私有 fork 待复测）';
  function nmPerPx(sc, dims) {
    if (typeof sc !== 'number' || !dims || !dims.scales_nm) return null;
    const v = dims.scales_nm;
    if (v.length < 2 || typeof v[0] !== 'number'
        || typeof v[1] !== 'number') return null;
    return { x: num(sc * v[0]), y: num(sc * v[1]) };
  }

  // 缩放值在 state.toJSON() 里可能是 undefined（NG 版本差异）；
  // 退回到 viewer 上的 trackable（`.value` / 内部 `.value_`）。
  // 两处都拿不到时返回 undefined，调用侧据此判"这条数据没有缩放信息"。
  function scaleOf(js, v, key) {
    const direct = js && js[key];
    if (typeof direct === 'number' && isFinite(direct)) {
      return { val: direct, src: 'state' };
    }
    const t = v && v[key];
    if (t && typeof t === 'object') {
      const cand = [t.value, t.value_];
      for (let i = 0; i < cand.length; i++) {
        if (typeof cand[i] === 'number' && isFinite(cand[i])) {
          return { val: cand[i], src: 'viewer' };
        }
      }
    }
    return { val: undefined, src: 'none' };
  }

  function extractViewpoint() {
    const v = R.viewer; if (!v) return null;
    let js;
    try { js = v.state.toJSON(); } catch (e) { return null; }
    const layers = (js.layers || []);
    const dims = extractDimensions(js.dimensions);
    const pos = norm(js.position);
    let posNm = null;
    if (pos && dims && dims.scales_nm
        && dims.scales_nm.length === pos.length) {
      posNm = pos.map((p, i) => (typeof p === 'number'
        && typeof dims.scales_nm[i] === 'number'
        ? num(p * dims.scales_nm[i]) : null));
    }
    const crossS = scaleOf(js, v, 'crossSectionScale');
    const projS = scaleOf(js, v, 'projectionScale');
    const vp = {
      state_id: R.stateId,
      position: pos,                     // 坐标空间单位（= dims.scales）
      position_nm: posNm,                // 换算后 nm（依据见 dimensions）
      crossSectionScale: (crossS.val === undefined) ? null : num(crossS.val),
      projectionScale: (projS.val === undefined) ? null : num(projS.val),
      // 缩放值从哪来：state（正常）/ viewer（兜底）/ none（拿不到）
      scale_source: (crossS.src === 'state' || projS.src === 'state')
        ? ((crossS.src === 'none' || projS.src === 'none') ? 'partial' : 'state')
        : ((crossS.src === 'viewer' || projS.src === 'viewer') ? 'viewer' : 'none'),
      crossSectionOrientation: norm(js.crossSectionOrientation),
      projectionOrientation: norm(js.projectionOrientation),
      nm_per_px: nmPerPx(js.crossSectionScale, dims),
      nm_per_px_method: NM_PER_PX_METHOD,
      layout: js.layout,
      // 切片是否显示、局部视口、切片厚度：都直接影响画面，但不影响 position
      showSlices: (typeof js.showSlices === 'boolean') ? js.showSlices : null,
      partialViewport: norm(js.partialViewport),
      crossSectionDepth: num(js.crossSectionDepth),
      dimensions: dims,
      viewport: viewportInfo(),
      layers: layerInfo(layers),
      visibleLayers: layers.filter(l => l.visible !== false)
        .map(l => l.name || '?'),
      selectedLayer: (v.selectedLayer && v.selectedLayer.name) || null,
    };
    let mc = null;
    for (const l of layers) {
      if (l && l.multicut) {
        mc = {
          layer: l.name,
          sources: (l.multicut.sources || []).length,
          sinks: (l.multicut.sinks || []).length,
          raw: l.multicut,
          state_id: R.stateId,
        };
        break;
      }
    }
    // 选中集单独返回：它不该进 vp（会让每条 state-change 带一遍完整 id 列表），
    // 但必须进去重 sig，否则“只改选中”会被当成没变化。
    const segs = {};
    for (const l of layers) {
      if (l && Array.isArray(l.segments) && l.segments.length) {
        segs[l.name || '?'] = l.segments.map(String);
      }
    }
    return { vp, mc, segments: segs };
  }

  function currentSelection() {
    const v = R.viewer; if (!v) return null;
    try {
      const m = v.layerSelectedValues; if (!m) return null;
      // 实测形态（neuroglancer 2.41 构建产物）：类实例，暴露 toJSON()；
      // 直接 Object.keys 只会拿到内部字段（disposers 等）——必须先 toJSON
      let plain = null;
      if (typeof m.toJSON === 'function') {
        try { plain = m.toJSON(); } catch (e) { plain = null; }
      }
      if (!plain && typeof m.forEach === 'function') {
        plain = {};
        m.forEach((val, key) => { plain[key] = val; });
      }
      if (!plain) plain = m;
      const out = {};
      const add = (key, val) => {
        try {
          let raw = (val && typeof val === 'object' && 'value' in val)
            ? val.value : val;
          let arr = [];
          if (raw && typeof raw.forEach === 'function') {
            raw.forEach(x => arr.push(String(x)));
          } else if (raw && typeof raw[Symbol.iterator] === 'function' &&
                     typeof raw !== 'string') {
            arr = Array.from(raw).map(String);
          } else if (raw !== null && raw !== undefined) {
            arr = [String(raw)];
          }
          // layerSelectedValues 里是选中段的 id；去掉 NG 的 `!` 前缀后保留纯数字。
          // 之前强加“8 位以上”会把小 id 数据集（测试图/私有段）整层丢掉。
          arr = arr.map(s => s.replace(/^!/, '')).filter(s => /^\d+$/.test(s));
          if (arr.length) {
            out[key] = { count: arr.length, ids: arr.slice(0, 8) };
          }
        } catch (e) { /* ignore */ }
      };
      Object.keys(plain).forEach(k => add(k, plain[k]));
      return Object.keys(out).length ? out : null;
    } catch (e) { return null; }
  }

  function currentMouse() {
    const v = R.viewer; if (!v || !v.mouseState) return null;
    try {
      const ms = v.mouseState;
      const p = ms.position; if (!p) return null;
      // 实测：position 是索引对象 {0,1,2}（无 length），不是数组。
      const arr = (typeof p.length === 'number')
        ? Array.from(p) : [p[0], p[1], p[2]];
      const out = {
        position: arr.map(x => (typeof x === 'number' ? +x.toFixed(4) : x)),
        page: [num(ms.pageX), num(ms.pageY)],
      };
      // 命中的分割段（若渲染层已 pick）：给 F09 hit 提供直接证据。
      // 实测（公开 minnie65）：`pickedValue` 是 **Uint64 实例**（keys: low/high，
      // 有 toNumber/toString），不是数字也不是数组；`pickedRenderLayer` 也可能为 null。
      // **0 = 没命中任何段**，不写进事件——否则一堆 picked:"0" 会把真命中冲淡。
      // 如实边界：迄今**没有一次成功命中的证据**，但两次失败都是验证环境的问题：
      //   ① 坐标给错：minnie65 有 voxel_offset（x/y ≥110592nm、z ≥592640nm），
      //      最初用的 (100000,55000,20000) 完全在数据范围外；
      //   ② 改正坐标后瓦片仍没加载出来（viz.neurodata.io 上 0 个数据请求，画面灰）。
      // 不是“pick 在无头下不可用”（这个推断已作废）。真数据下能不能取到仍未验。
      try {
        if (ms.pickedValue != null) {
          let pv = ms.pickedValue, txt = null;
          if (pv && typeof pv.forEach === 'function' && typeof pv !== 'string') {
            const a = []; pv.forEach(x => a.push(String(x)));
            txt = a.join(',');
          } else {
            try { txt = String(pv); } catch (e) { txt = null; }
          }
          if (txt && txt !== '0' && txt !== '[object Object]') {
            out.picked = txt.slice(0, 200);
          } else if (txt === '0') {
            out.picked_miss = true;   // 在画布里但没压到段（不是采不到）
          }
        }
        if (ms.pickedRenderLayer) {
          out.picked_layer = (ms.pickedRenderLayer.name || null);
        }
      } catch (e) { /* ignore */ }
      return out;
    } catch (e) { return null; }
  }

  /* ---------- 采样循环 ---------- */
  // 完整 state 的本钱与上限。
  // “复刻”要求任意时刻都能一模一样重画，所以每个状态变化都要 **完整 state**，
  // 不是摘要（摘要只能恢复位置/尺度，恢复不了图层内部状态与布局细节）。
  // 超大的 state（图层极多）不存，但留下去因，不静默丢。
  const MAX_FULL_STATE_BYTES = 256 * 1024;
  // 能“真正恢复”的最小字段集。缺了其中任意一个的 state 要么是 viewer 还没
  // 初始化完（刚开页），要么是异常——照存，但必须打标 state_partial，
  // 否则下游会把一份只有 layout 的 state 当成“可恢复”去用。
  const STATE_REQUIRED = ['position', 'layers', 'dimensions', 'layout'];

  function fnv1a(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h.toString(16);
  }

  // 超大 state 的降级：先丢“大且可重建”的字段，保留相机/图层骨架，
  // 并逐项记下丢了什么。绝不把整条 state 静默丢掉。
  function trimState(js) {
    const reasons = [];
    const cut = function (why) { if (reasons.indexOf(why) < 0) reasons.push(why); };
    const layers = js.layers || [];
    for (const l of layers) {
      if (l && l.source && typeof l.source === 'object'
          && !Array.isArray(l.source) && l.source.state != null) {
        delete l.source.state;
        cut('layer.source.state');
      }
      if (l && l.segmentColors) {
        let n = 0;
        try {
          n = (typeof l.segmentColors.size === 'number')
            ? l.segmentColors.size : Object.keys(l.segmentColors).length;
        } catch (e) { n = 1; }
        if (n) {
          delete l.segmentColors;
          l.segmentColors_omitted = n;
          cut('layer.segmentColors');
        }
      }
    }
    let str = '';
    try { str = JSON.stringify(js); } catch (e) { return { state: js, reasons }; }
    if (str.length <= MAX_FULL_STATE_BYTES) return { state: js, reasons };
    // 仍然太大：保留每层前 512 个选中 id，标出截断。
    for (const l of layers) {
      if (l && Array.isArray(l.segments) && l.segments.length > 512) {
        l.segments = l.segments.slice(0, 512);
        l.segments_clipped = true;
        cut('layer.segments');
      }
    }
    try { str = JSON.stringify(js); } catch (e) { return { state: js, reasons }; }
    if (str.length <= MAX_FULL_STATE_BYTES) return { state: js, reasons };
    // 最后手段：图层只留身份/可见性，仍保留相机与坐标系。
    for (const l of layers) {
      if (!l) continue;
      for (const k of ['segments', 'segmentColors', 'segmentDefaultColor',
                       'source']) {
        if (k in l) delete l[k];
      }
    }
    cut('layers.detail');
    return { state: js, reasons };
  }

  function fullState() {
    const v = R.viewer;
    if (!v) return null;
    let js, str;
    try { js = v.state.toJSON(); } catch (e) { return null; }
    try { str = JSON.stringify(js); } catch (e) { return null; }
    const fullBytes = str.length;   // 近似字节数（state 基本是 ASCII）
    let trimmed = null;
    if (fullBytes > MAX_FULL_STATE_BYTES) {
      trimmed = trimState(js);
      js = trimmed.state;
      try { str = JSON.stringify(js); } catch (e) { return null; }
    }
    if (str.length > MAX_FULL_STATE_BYTES) {
      return { state: null, state_bytes: fullBytes, state_sha: null,
               state_omitted: 'too_large', state_bytes_full: fullBytes,
               state_truncated: (trimmed && trimmed.reasons || []).join(',') };
    }
    const missing = STATE_REQUIRED.filter(function (k) { return !(k in js); });
    return { state: js, state_bytes: str.length,
             state_bytes_full: fullBytes,
             state_sha: fnv1a(str),
             state_partial: missing.length ? missing.join(',') : null,
             state_truncated: (trimmed && trimmed.reasons.length)
               ? trimmed.reasons.join(',') : null };
  }

  // 轮询（兜底）与订阅（state.changed）走同一条采样路径：
  // 两路各自去重会互相吞掉对方的变化，必须共用一个 R.last.state。
  function sampleState(trigger) {
    if (!R.recording) return;
    const sel = currentSelection();
    const got = extractViewpoint();
    if (got && got.vp) {
      const forSig = Object.assign({}, got.vp);
      delete forSig.state_id;
      forSig.segments = got.segments;   // 只进 sig，不进 vp
      const sig = JSON.stringify(forSig);
      if (sig !== R.last.state) {
        R.last.state = sig;
        R.stateId += 1;
        got.vp.state_id = R.stateId;
        const payload = { viewpoint: got.vp, state_id: R.stateId,
                          trigger: trigger || 'poll' };
        if (R.config.full_state_on_change) {
          const fs = fullState();
          if (fs) {
            payload.state = fs.state;
            payload.state_sha = fs.state_sha;
            payload.state_bytes = fs.state_bytes;
            if (fs.state_omitted) payload.state_omitted = fs.state_omitted;
            if (fs.state_partial) payload.state_partial = fs.state_partial;
            if (fs.state_truncated) payload.state_truncated = fs.state_truncated;
            if (fs.state_bytes_full) payload.state_bytes_full = fs.state_bytes_full;
          }
        }
        emit('state_sample', payload);
        R.lastStateSha = (payload.state_sha !== undefined)
          ? payload.state_sha : null;
        R.lastStateAt = wall();
        maybeCheckpoint(got.vp, false);
        semanticShots(got.vp, got.segments);
      }
      // 停留检测必须在“没变化”时也跑，所以放在 sig 块外
      checkDwell(got.vp, got.segments);
      const mcsig = JSON.stringify(got.mc);
      if (mcsig !== R.last.multicut) {
        R.last.multicut = mcsig;
        if (got.mc) emit('multicut_change', got.mc);
      }
    }
    const ssig = JSON.stringify(sel);
    if (ssig !== R.last.sel) {
      R.last.sel = ssig;
      if (sel) emit('selection_sample', {
        selection: sel, state_id: R.stateId,
      });
    }
    const mouse = currentMouse();
    const msig = JSON.stringify(mouse);
    if (msig !== R.last.mouse) {
      R.last.mouse = msig;
      if (mouse) emit('mouse_sample', {
        position: mouse, state_id: R.stateId,
      });
    }
    // 界面状态与外部 provider 也走同一个节拍：
    // 它们与 viewpoint 无关（开面板时 position 可能一点不变），所以各自去重。
    pollUi();
    pollJournal();
    pollProviders();
  }

  function sampleAll() {
    if (!R.recording) return;
    // 滚轮 burst 靠下一次输入触发落盘，但如果人就停在这里不动作，
    // 最后一簇滚轮会丢在内存里——这里补一次超时 flush。
    if (R.wheelBurst &&
        (wall() - R.wheelBurst.last_ms) > R.config.wheel_burst_gap_ms) {
      flushWheelBurst();
    }
    sampleState('poll');
  }

  /* ---------- 语义关键帧 / 停留 ---------- */
  // 拍图是**语义触发**，不是按帧：缩放跨档（≥2 倍）、图层可见性变化、
  // 选中集变化各拍一张（同类 1.5s 内限流）；人停住 dwell_ms 再拍一张。
  // 手动、提交前后、检查点拍照仍然保留，互不替代。
  function semanticKey(vp, segs) {
    const vis = (vp.visibleLayers || []).join('|');
    // 中心取整到 100nm：太细会把渲染抖动当成“人动了”
    const center = vp.position_nm
      ? vp.position_nm.map(x => (x == null ? null : Math.round(x / 100) * 100))
      : null;
    const seg = JSON.stringify(segs || null);
    return { vis: vis, seg: seg, center: center,
             dkey: JSON.stringify([center, seg, vis]) };
  }

  // 控制台排查用：`__caveRecorder.diag()`。
  // 回答两个问题——① 缩放值到底拿没拿到；② 语义关键帧触发过几次。
  R.diag = function () {
    let vp = null;
    try {
      const g = extractViewpoint();          // 返回 {vp, mc, segments}
      vp = (g && g.vp) ? g.vp : null;
    } catch (e) { vp = null; }
    const shots = (R.shots || []).map(function (x) { return x.moment; });
    const moments = {};
    shots.forEach(function (m) { moments[m] = (moments[m] || 0) + 1; });
    return {
      recording: !!R.recording,
      semantic_keyframe: !!R.config.semantic_keyframe,
      keyframe_on_checkpoint: !!R.config.keyframe_on_checkpoint,
      dwell_ms: R.config.dwell_ms,
      scale_source: vp ? vp.scale_source : 'no-viewpoint',
      crossSectionScale: vp ? vp.crossSectionScale : null,
      projectionScale: vp ? vp.projectionScale : null,
      lastSemantic: R.lastSemantic || null,
      semantic_triggered: R.semanticCounts || {},
      scale_history: (R.scaleHistory || []).slice(-6),
      last_eval: R.lastEval || null,
      shots_total: shots.length,
      shots_by_moment: moments,
      events: (R.events || []).length,
    };
  };

  // 两个 3x3 旋转矩阵之间的夹角（度）。cross/projectionOrientation 都是矩阵。
  function orientDeg(a, b) {
    if (!a || !b || a.length !== 9 || b.length !== 9) return null;
    let tr = 0;
    for (let i = 0; i < 3; i++) {
      for (let k = 0; k < 3; k++) tr += a[i * 3 + k] * b[i * 3 + k];
    }
    const c = Math.max(-1, Math.min(1, (tr - 1) / 2));
    return Math.acos(c) * 180 / Math.PI;
  }

  function checkDwell(vp, segs) {
    // 0 = 关闭停留（设置面板的“关”）；旧实现会在 0ms 后立刻拍。
    if (R.config.dwell_ms <= 0) return;
    const now = wall();
    const k = semanticKey(vp, segs);
    if (!R.dwell || R.dwell.key !== k.dkey) {
      R.dwell = { key: k.dkey, since: now, shot: false };
      return;
    }
    if (R.dwell.shot || now - R.dwell.since < R.config.dwell_ms) return;
    R.dwell.shot = true;
    emit('viewer.dwell', {
      duration_ms: now - R.dwell.since, center: k.center, segments: segs,
      visible_layers: vp.visibleLayers, state_id: R.stateId,
    });
    if (R.config.semantic_keyframe) {
      captureShot({ moment: 'dwell',
                    detail: { dwelled_ms: now - R.dwell.since,
                              threshold_ms: R.config.dwell_ms,
                              center: k.center } });
    }
  }

  function semanticShots(vp, segs) {
    const now = wall();
    const k = semanticKey(vp, segs);
    const px = (vp.viewport && vp.viewport.css_px) || [];
    const cur = { proj: vp.projectionScale, cross: vp.crossSectionScale,
                  vis: k.vis, seg: k.seg,
                  pos: vp.position_nm || null,
                  // 可见宽度（nm）：位置移动超过半屏就算"换了一段内容"
                  view: (vp.nm_per_px && px[0]) ? vp.nm_per_px.x * px[0] : null,
                  cOri: vp.crossSectionOrientation || null,
                  pOri: vp.projectionOrientation || null };
    // 留一小段历史：缩放拿不到值时，人能从 diag() 看出"值根本没变"还是"没采到"
    R.scaleHistory = R.scaleHistory || [];
    R.scaleHistory.push({ t: Math.round(now - (R.scaleT0 || (R.scaleT0 = now))) / 1000,
                          proj: (cur.proj === undefined ? null : cur.proj),
                          cross: (cur.cross === undefined ? null : cur.cross) });
    if (R.scaleHistory.length > 12) R.scaleHistory.shift();
    const prev = R.lastSemantic;
    if (!prev || !R.config.semantic_keyframe) { R.lastSemantic = cur; return; }
    let fired = false;
    // 这张图"为什么拍、变化多大"随图落盘（shots.jsonl 的 detail 字段）：
    // 缩放记倍数，平移记移动量和屏宽，转动记角度，选中/图层记前后差异。
    const fire = (reason, detail) => {
      const last = R.lastSemanticShot || (R.lastSemanticShot = {});
      if (last[reason] && now - last[reason] < 1500) return;  // 限流
      last[reason] = now;
      R.semanticCounts = R.semanticCounts || {};
      R.semanticCounts[reason] = (R.semanticCounts[reason] || 0) + 1;
      const d = detail || {};
      d.since_last_shot_ms = R.lastShotAt ? (now - R.lastShotAt) : null;
      captureShot({ moment: reason, detail: d });
      R.lastShotAt = now;
      fired = true;
    };
    // 缩放跨"一档"就算：实测滚轮一次约 ×1.2，2 倍要滚好几下，
    // 人会觉得"我明明放大了却一张都没拍"。1.5 倍等于 NG 的典型两档。
    const RATIO = 1.5;
    const ratio = (a, b) => (!!(a && b) && (a / b >= RATIO || b / a >= RATIO));
    const rCross = (cur.cross && prev.cross) ? +(cur.cross / prev.cross).toFixed(3) : null;
    const rProj = (cur.proj && prev.proj) ? +(cur.proj / prev.proj).toFixed(3) : null;
    if (ratio(cur.cross, prev.cross) || ratio(cur.proj, prev.proj)) {
      fire('scale_change', {
        axis: ratio(cur.cross, prev.cross) ? 'cross_section' : 'projection',
        cross: cur.cross, prev_cross: prev.cross, ratio_cross: rCross,
        proj: cur.proj, prev_proj: prev.proj, ratio_proj: rProj,
        threshold: RATIO,
      });
    }
    // 视角变了也是"看到了新东西"：平移/转过半屏、转视角超过 10° 各拍一张。
    // 2D 截面和 3D 相机都算——用户要的是"我看的画面变了就有图"。
    let movedNm = null;
    if (cur.pos && prev.pos) {
      const dx = cur.pos[0] - prev.pos[0];
      const dy = cur.pos[1] - prev.pos[1];
      const dz = cur.pos[2] - prev.pos[2];
      movedNm = Math.sqrt(dx * dx + dy * dy + dz * dz);
    }
    const viewNm = cur.view || prev.view;
    const cd = orientDeg(cur.cOri, prev.cOri);
    const pd = orientDeg(cur.pOri, prev.pOri);
    const movedRatio = (movedNm != null && viewNm) ? +(movedNm / viewNm).toFixed(3) : null;
    const byMove = (movedNm != null && viewNm && movedNm > viewNm * 0.5);
    const byCrossTurn = (cd != null && cd > 10);
    const byProjTurn = (pd != null && pd > 10);
    if (byMove || byCrossTurn || byProjTurn) {
      fire('view_change', {
        kind: byMove ? 'pan' : (byCrossTurn ? 'slice_rotate' : 'camera_rotate'),
        moved_nm: (movedNm == null) ? null : Math.round(movedNm),
        view_nm: viewNm ? Math.round(viewNm) : null,
        moved_ratio: movedRatio, threshold_ratio: 0.5,
        turn_deg_cross: (cd == null) ? null : +cd.toFixed(1),
        turn_deg_proj: (pd == null) ? null : +pd.toFixed(1),
        threshold_deg: 10,
        position: cur.pos, prev_position: prev.pos,
      });
    }

    // 记下最后一次判断的中间值（必须放在 movedNm / viewNm / cd 算出来之后：
    // 之前写在使用之前，const 的 TDZ 直接抛 ReferenceError，被外层 try 吞掉，
    // 表现就是"调视角、缩放都不触发，而且一点报错都没有"）。
    R.lastEval = {
      semantic_on: !!R.config.semantic_keyframe,
      cross: (cur.cross === undefined ? null : cur.cross),
      prev_cross: (prev.cross === undefined ? null : prev.cross),
      ratio_cross: (cur.cross && prev.cross) ? +(cur.cross / prev.cross).toFixed(3) : null,
      moved_nm: (movedNm == null) ? null : Math.round(movedNm),
      view_nm: viewNm ? Math.round(viewNm) : null,
      turn_deg: (cd == null) ? null : +cd.toFixed(1),
      proj: (cur.proj === undefined ? null : cur.proj),
      prev_proj: (prev.proj === undefined ? null : prev.proj),
      at: Math.round(now),
    };
    if (prev.vis !== cur.vis) {
      fire('layer_change', { visible_layers: cur.vis, prev_visible_layers: prev.vis });
    }
    if (prev.seg !== cur.seg) {
      let added = [], removed = [];
      try {
        const a0 = JSON.parse(prev.seg || '{}');
        const b0 = JSON.parse(cur.seg || '{}');
        Object.keys(b0).forEach(function (k) { if (!(k in a0)) added.push(k); });
        Object.keys(a0).forEach(function (k) { if (!(k in b0)) removed.push(k); });
      } catch (e) { /* 解析不了就只记前后原文 */ }
      fire('selection_change', {
        selected: cur.seg, prev_selected: prev.seg,
        added: added, removed: removed,
      });
    }
    // 基准**只在真拍了一张之后**才推进。
    // 原来每帧都推进：滚轮一格放大 1.1 倍，相邻两帧比值永远不到 2，
    // 连续放大 10 倍也一张都不拍（2026-10-29 用户实测正是这个现象）。
    //
    // 例外：某个轴从"拿不到"（null）变成有值时，即使没到阈值也要把基准补上，
    // 否则 prev.proj 会一直是 null，之后永远算不出比值（实测 3D 缩放就是这样）。
    const wasNull = (prev.cross == null) || (prev.proj == null)
      || (prev.pos == null) || (prev.cOri == null);
    if (fired || wasNull) R.lastSemantic = cur;
  }

  // 轮询间隔可配（0 = 只靠订阅）；改配置时要重建 timer，否则新值不生效。
  let sampleTimer = null;
  let dwellTimer = null;
  function resetSampleTimer() {
    if (sampleTimer) { clearInterval(sampleTimer); sampleTimer = null; }
    if (dwellTimer) { clearInterval(dwellTimer); dwellTimer = null; }
    const ms = R.config.state_interval_ms;
    if (ms > 0) {
      sampleTimer = setInterval(sampleAll, ms);
      return;
    }
    // 只靠订阅时，停留 = 没有变化 = 订阅不触发；dwell 必须有自己的节拍。
    dwellTimer = setInterval(function () {
      if (!R.recording) return;
      const got = extractViewpoint();
      if (got && got.vp) checkDwell(got.vp, got.segments);
    }, 1000);
  }

  /* ---------- 订阅：离散变化即时记（轮询仍作兜底） ---------- */
  let stateWatchSig = null;
  let stateWatchTimer = null;

  function onStateChanged() {
    if (!R.recording) return;
    const wait = Math.max(0, R.config.semantic_debounce_ms | 0);
    if (stateWatchTimer) return;      // 去抖：一次拖动只记一帧
    stateWatchTimer = setTimeout(function () {
      stateWatchTimer = null;
      sampleState('subscribe');
    }, wait);
  }

  function detachStateWatch() {
    const sig = stateWatchSig;
    if (sig) {
      try {
        if (typeof sig.removeListener === 'function') {
          sig.removeListener(onStateChanged);
        } else if (sig.handlers && typeof sig.handlers.delete === 'function') {
          sig.handlers.delete(onStateChanged);
        }
      } catch (e) { /* 已经拆掉就算了 */ }
    }
    stateWatchSig = null;
    if (stateWatchTimer) { clearTimeout(stateWatchTimer); stateWatchTimer = null; }
  }

  // NG 不同构建里 state.changed 有两种形态：
  //   标准 Signal：addListener/removeListener
  //   精简版（本地 NG 2.41 实测）：{handlers: Set, count, dispatch}，只能往 Set 里加
  // 两种都支持；都不认就返回 false，回退到纯轮询。
  function attachStateWatch() {
    const v = R.viewer;
    if (!v || !v.state) return false;
    const sig = v.state.changed;
    if (!sig) return false;
    if (stateWatchSig === sig) return true;
    detachStateWatch();
    try {
      if (typeof sig.addListener === 'function') {
        sig.addListener(onStateChanged);
      } else if (sig.handlers && typeof sig.handlers.add === 'function') {
        sig.handlers.add(onStateChanged);
      } else {
        return false;
      }
    } catch (e) { return false; }
    stateWatchSig = sig;
    return true;
  }

  /* ---------- 状态 / 缺口 / checkpoint ---------- */
  function statusEvent(reason) {
    const ev = {
      visible: !R.hidden, focused: R.focused, online: R.online,
      gap_reason: reason || null, state_id: R.stateId,
    };
    // 只在状态真变化时记一条（blur/focus 会因工具调用反复抖动）
    const sig = [ev.visible, ev.focused, ev.online, ev.gap_reason].join('|');
    if (sig === R.lastStatusSig) return;
    R.lastStatusSig = sig;
    // 只有"看不见/断网"算采集缺口；失焦只是窗口切换，不作为 gap。
    if (reason === 'page_hidden' || reason === 'network_offline') {
      R.gaps.push({ t_wall: wall(), reason: reason });
    } else if (R.gaps.length) {
      // 回到正常：闭合最后一条缺口，包里的 gaps 才自带时长
      const last = R.gaps[R.gaps.length - 1];
      if (last.t_end == null) last.t_end = wall();
    }
    emit('collector_status', ev);
  }

  // 全量 viewer state 快照（R1 回放/seek 的基准；不是后端分割快照）。
  function checkpoint(reason) {
    const v = R.viewer;
    if (!v) return null;
    let state = null;
    try { state = v.state.toJSON(); } catch (e) { return null; }
    if ((R.counters.state_checkpoint || 0) >= R.MAX_CHECKPOINTS) {
      bump('checkpoint_dropped'); noteOverflow('checkpoints'); return null;
    }
    const ev = {
      state_id: R.stateId, reason: reason || 'periodic', state: state,
    };
    if (!R.initialCheckpoint) {
      R.initialCheckpoint = { state_id: R.stateId, t_wall: wall(),
                              state: state };
    }
    R.lastCheckpointWall = wall();
    emit('state_checkpoint', ev);
    // 回放保真度自检的前提：checkpoint 与图一一对应。
    // 两个不拍的例外（实测踩过）：
    //   session_start：那一刻 canvas 还没画，拍到的是 blank_readback；
    //   session_stop ：紧接着就要导出，异步图来不及落进包里。
    if (R.config.keyframe_on_checkpoint) {
      const why = reason || 'periodic';
      R.lastCheckpointShot = null;
      if (why === 'session_start') {
        setTimeout(function () {
          if (R.recording) {
            R.lastCheckpointShot = captureShot({
                          moment: 'checkpoint',
                          detail: { reason: 'session_start', auto: true,
                                    note: '开场基线（延迟 600ms 等首帧画完）' } });
          }
        }, 600);
      } else if (why !== 'session_stop') {
        R.lastCheckpointShot = captureShot({
                      moment: 'checkpoint',
                      detail: { reason: why,
                                auto: why !== 'manual',
                                checkpoint_index: (R.checkpoints || []).length } });
      }
    }
    return ev;
  }
  function maybeCheckpoint(vp, force) {
    const now = wall();
    if (!force && (now - R.lastCheckpointWall) < 30000) return null;
    return checkpoint('state_change' + (force ? '_forced' : ''));
  }

  /* ---------- 静态区间：不做周期心跳 ---------- */
  // F26/RF14 要的是“区分在线与空闲”，不是“固定频率写一条存活信号”。
  // 周期心跳有两个无法回避的问题：① recorder 自己挂了心跳也停，
  // 数据上分不出“人走了”与“采集器死了”；② 它会污染事件流（实测占 28%）。
  // 更准的做法是**离线从输入事件时间戳算静默区间**（derived/episodes.jsonl
  // 里的 idle_segments）：可重算、不丢原始、且能说出“哪段没输入”。
  // 因此这里不产生任何周期事件。

  /* ---------- 手势聚合（RF02/RF03 + PP02） ---------- */
  // 只做**机械切分**：pointerdown→up 配成一次拖拽/点击，连续滚轮聚成一次缩放。
  // 不猜“这次拖拽在想什么”（PP08 弱动作标签是离线的事）。
  // raw 事件并行保留（PP01：不删除真实动作）。
  function viewRef() {
    try {
      const r = extractViewpoint();
      if (!r || !r.vp) return null;
      const v = r.vp;
      return {
        state_id: v.state_id,
        position_nm: v.position_nm,
        crossSectionScale: v.crossSectionScale,
        projectionScale: v.projectionScale,
        layout: v.layout,
      };
    } catch (e) { return null; }
  }
  // RF01 要 panel_id：插件拿不到 panel 身份，只能记“哪个 canvas + 尺寸”。
  // 不冒充 panel_id，字段名就叫 canvas。
  function panelOf(ev) {
    try {
      const t = ev.target;
      if (!t || t.tagName !== 'CANVAS') return null;
      const cs = Array.from(document.querySelectorAll('canvas'));
      const i = cs.indexOf(t);
      if (i < 0) return null;
      return { canvas_index: i, w: t.width, h: t.height,
               client_w: t.clientWidth, client_h: t.clientHeight };
    } catch (e) { return null; }
  }
  function gestureBegin(ev) {
    const xy = [Math.round(ev.clientX), Math.round(ev.clientY)];
    R.gesture = {
      start_ms: wall(), start_mono: Math.round(mono()),
      start_xy: xy, last_xy: xy.slice(),
      button: ev.button, pointer_id: ev.pointerId,
      n_raw: 0, path_px: 0,
      start_view: viewRef(), panel: panelOf(ev),
    };
  }
  function gestureMove(ev) {
    const g = R.gesture; if (!g) return;
    const x = Math.round(ev.clientX), y = Math.round(ev.clientY);
    const dx = x - g.last_xy[0], dy = y - g.last_xy[1];
    g.path_px += Math.sqrt(dx * dx + dy * dy);
    g.last_xy = [x, y];
    g.n_raw += 1;
  }
  function gestureEnd(kindHint) {
    const g = R.gesture; if (!g) return;
    R.gesture = null;
    const end = viewRef();
    const net = [g.last_xy[0] - g.start_xy[0],
                 g.last_xy[1] - g.start_xy[1]];
    const moved = Math.abs(net[0]) + Math.abs(net[1]) + Math.round(g.path_px) >= 4;
    const kind = kindHint || (moved ? 'drag' : 'click');
    // “拖了但视角没变”（撞边界/点到 UI）必须能区分于“真的移动了”
    const samePos = g.start_view && end
      && JSON.stringify(g.start_view.position_nm) === JSON.stringify(end.position_nm);
    emit('gesture', {
      kind: kind, button: g.button, pointer_id: g.pointer_id,
      start_ms: g.start_ms, end_ms: wall(),
      duration_ms: wall() - g.start_ms,
      n_raw: g.n_raw, path_px: Math.round(g.path_px), net_px: net,
      start_xy: g.start_xy, end_xy: g.last_xy,
      panel: g.panel,
      start_view: g.start_view, end_view: end,
      view_changed: (g.start_view && end) ? !samePos : null,
    });
  }
  function flushWheelBurst() {
    const b = R.wheelBurst; if (!b) return;
    R.wheelBurst = null;
    const end = viewRef();
    const same = b.start_view && end && b.start_view.crossSectionScale
      !== end.crossSectionScale;
    emit('gesture', {
      kind: 'zoom', button: null, pointer_id: null,
      start_ms: b.start_ms, end_ms: b.last_ms,
      duration_ms: b.last_ms - b.start_ms,
      n_raw: b.n_raw, path_px: null, net_px: null,
      delta_y_sum: Math.round(b.delta_y_sum),
      delta_x_sum: Math.round(b.delta_x_sum),
      delta_mode: b.delta_mode, modifiers: b.modifiers,
      start_xy: b.start_xy, end_xy: b.last_xy, panel: b.panel,
      start_view: b.start_view, end_view: end,
      // 滚轮不一定改变切片尺度（清单 RF03 原话），所以只报“刻度变了没”
      scale_changed: !!same,
    });
  }

  /* ---------- 原始输入 ---------- */
  // 采集器自己的 UI（手动会话面板）不算“用户操作”：
  // 不排除的话，点「打检查点」会被记成一次画布点击，回放时真的去点画布。
  function isOwnUI(ev) {
    try {
      const t = ev && ev.target;
      return !!(t && t.closest && t.closest('[data-cave-ui]'));
    } catch (e) { return false; }
  }

  function onPointer(ev) {
    if (isOwnUI(ev)) return;
    if (!R.recording) return;
    try {
      const now = wall();
      const isEdge = ev.type !== 'pointermove';
      // 端点全记；move 按 R.config.move_sample_ms 采样（0 = 全量）
      if (isEdge ||
          R.config.move_sample_ms <= 0 ||
          (now - R.lastMoveWall) >= R.config.move_sample_ms) {
        if (!isEdge) R.lastMoveWall = now;
        emit('pointer', {
          phase: ev.type, button: ev.button, buttons: ev.buttons,
          pointer_id: ev.pointerId, pointer_type: ev.pointerType,
          x: Math.round(ev.clientX), y: Math.round(ev.clientY),
          state_id: R.stateId, panel: panelOf(ev),
        });
      }
      if (ev.type === 'pointerdown') gestureBegin(ev);
      else if (ev.type === 'pointermove') gestureMove(ev);
      else if (ev.type === 'pointerup') { gestureMove(ev); gestureEnd(); }
      else if (ev.type === 'pointercancel') { gestureMove(ev); gestureEnd(); }
    } catch (e) { /* ignore */ }
  }
  // RF04：规范化 action id。插件**读不到** NG 的 action dispatcher，
  // 所以默认 action:null + action_source:'unmapped'（不猜），
  // 等改源码时在 action dispatcher 上 hook（已记入 PLUGIN_GAPS）。
  function onKey(ev) {
    if (!R.recording) return;
    if (isOwnUI(ev)) return;
    const t = ev.target;
    const tag = (t && t.tagName) ? t.tagName.toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' ||
        (t && t.isContentEditable)) return;
    try {
      const mods = [];
      if (ev.ctrlKey) mods.push('ctrl');
      if (ev.shiftKey) mods.push('shift');
      if (ev.altKey) mods.push('alt');
      if (ev.metaKey) mods.push('meta');
      let action = null, src = 'unmapped', sect = null;
      try {
        // 实测（2026-10-26）：`inputEventBindings` 不是 Map，而是
        // {global, sliceView, perspectiveView} 三个 Map，键形如 `at:arrowright` /
        // `bubble:arrowright`，值是 {action:'x+'}。之前只试 `binds.get()`，
        // 于是 action 永远是 null——字段看着采了，其实是空。
        const binds = window.viewer && window.viewer.inputEventBindings;
        const pick = function (m, key) {
          try {
            if (!m) return null;
            if (typeof m.get === 'function') return m.get(key) || null;
            return m[key] || null;
          } catch (e) { return null; }
        };
        const code = String(ev.code || '').toLowerCase();
        const low = String(ev.key || '').toLowerCase();
        const cands = [];
        if (binds) {
          ['global', 'sliceView', 'perspectiveView'].forEach(function (s) {
            [code, low].forEach(function (k) {
              if (!k) return;
              cands.push([binds[s], 'at:' + k, s]);
              cands.push([binds[s], 'bubble:' + k, s]);
            });
          });
          // 老结构（直接 Map / 平铺对象）：保留，但不优先
          cands.push([binds, ev.key, 'flat']);
          cands.push([binds, low, 'flat']);
        }
        for (let i = 0; i < cands.length; i++) {
          const hit = pick(cands[i][0], cands[i][1]);
          if (!hit) continue;
          const a = (typeof hit === 'string') ? hit
            : (hit.action || hit.name || null);
          if (a) { action = a; src = 'inputEventBindings'; sect = cands[i][2]; break; }
        }
      } catch (e) { /* ignore */ }
      emit('key', {
        key: ev.key, code: ev.code, modifiers: mods,
        key_combo: (mods.join('+') + (mods.length ? '+' : '') + ev.key),
        action: action, action_source: src, action_section: sect,
        tool_context: toolContextHint(),
        state_id: R.stateId,
      });
    } catch (e) { /* ignore */ }
  }
  // 工具上下文：插件不知道当前工具（RF07 属于改源码才能做的事），
  // 只能从 DOM 上已选中的按钮/类名猜一个候选，并标明来源。
  function toolContextHint() {
    try {
      const a = document.activeElement;
      if (a && a.getAttribute) {
        const n = a.getAttribute('aria-label') || a.getAttribute('title')
          || a.getAttribute('data-tool');
        if (n) return { tool: n, source: 'active_element' };
      }
    } catch (e) { /* ignore */ }
    return { tool: null, source: 'plugin_cannot_read_tool_state' };
  }
  function onWheel(ev) {
    if (!R.recording) return;
    if (isOwnUI(ev)) return;
    try {
      const now = wall();
      if (R.wheelBurst &&
          (now - R.wheelBurst.last_ms) > R.config.wheel_burst_gap_ms) {
        flushWheelBurst();
      }
      const mods = [];
      if (ev.ctrlKey) mods.push('ctrl');
      if (ev.shiftKey) mods.push('shift');
      if (ev.altKey) mods.push('alt');
      if (ev.metaKey) mods.push('meta');
      if (!R.wheelBurst) {
        R.wheelBurst = {
          start_ms: now, last_ms: now, n_raw: 0,
          delta_y_sum: 0, delta_x_sum: 0,
          delta_mode: ev.deltaMode, modifiers: mods,
          start_xy: [Math.round(ev.clientX), Math.round(ev.clientY)],
          last_xy: [Math.round(ev.clientX), Math.round(ev.clientY)],
          start_view: viewRef(), panel: panelOf(ev),
        };
      }
      const b = R.wheelBurst;
      b.last_ms = now; b.n_raw += 1;
      b.delta_y_sum += ev.deltaY; b.delta_x_sum += ev.deltaX;
      b.last_xy = [Math.round(ev.clientX), Math.round(ev.clientY)];
      emit('wheel', {
        dy: Math.round(ev.deltaY), dx: Math.round(ev.deltaX),
        delta_mode: ev.deltaMode, modifiers: mods,
        x: Math.round(ev.clientX), y: Math.round(ev.clientY),
        state_id: R.stateId, panel: panelOf(ev),
      });
    } catch (e) { /* ignore */ }
  }
  function onVis() {
    R.hidden = !!document.hidden;
    if (R.recording) statusEvent(R.hidden ? 'page_hidden' : null);
  }
  function onBlur() {
    R.focused = false;
    if (R.recording) statusEvent('focus_lost');
  }
  function onFocus() {
    R.focused = true;
    if (R.recording) statusEvent(null);
  }
  function onOffline() {
    R.online = false;
    if (R.recording) statusEvent('network_offline');
  }
  function onOnline() {
    R.online = true;
    if (R.recording) statusEvent(null);
  }
  document.addEventListener('pointerdown', onPointer, true);
  document.addEventListener('pointerup', onPointer, true);
  // pointermove 之前**根本没挂**（2026-10-26 实测发现）：
  // 于是“move 按 50ms 采样”这段代码从未执行过，input.pointer 只有
  // down/up/click 端点，gesture 的 n_raw/path_px 也永远偏小。
  // 挂上后才有“怎么移过去的”，RF02/RF01 才是真的。
  document.addEventListener('pointermove', onPointer,
    { capture: true, passive: true });
  // 触控/笔被系统抢走时不会有 pointerup：不收尾手势会一直摄着
  document.addEventListener('pointercancel', onPointer, true);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('wheel', onWheel,
    { capture: true, passive: true });
  document.addEventListener('visibilitychange', onVis, true);
  window.addEventListener('blur', onBlur, true);
  window.addEventListener('focus', onFocus, true);
  window.addEventListener('offline', onOffline, true);
  window.addEventListener('online', onOnline, true);

  /* ---------- 提交锚点：只观察 fetch/XHR，不改动请求与响应 ---------- */
  // 目的：捕获 CAVE/ChunkedGraph 写接口（do_merge / execute_split /
  // split_preview / undo）的调用与回执（operation_id），为客户端事件流
  // 与服务端 op log 的 join 提供锚点。纯观察：参数原样透传、返回原
  // promise、异常原样抛出；仅 recording 时记录。
  // 范围限定：只记 CAVE API 调用（E2E 实测：neuroglancer 自身
  // 的 state 同步 POST /state/<token> 等非 CAVE 写请求会被误捕，已排除）。
  const API_MAX = 3000;
  function classifyApi(method, url) {
    const u = String(url || '').toLowerCase();
    const m = String(method || 'GET').toUpperCase();
    const isCaveApi = /\/(segmentation|meshing|annotation|auth|info)\/api\//
      .test(u) || /chunkedgraph/.test(u) || /\/api\/v\d+\//.test(u);
    if (!isCaveApi) return null;
    const isWrite = m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS';
    let kind = 'read';
    if (/preview/.test(u)) kind = 'split_preview';
    else if (/do_merge|merge/.test(u)) kind = 'merge';
    else if (/split/.test(u)) kind = 'split';
    else if (/undo/.test(u)) kind = 'undo';
    else if (isWrite) kind = 'write';
    return { kind: kind, write: isWrite, cave: isCaveApi };
  }
  function apiEmit(type, data) {
    if ((R.counters[type] || 0) >= API_MAX) {
      bump('api_dropped'); return;
    }
    emit(type, data);
  }
  // 服务端时钟锚点：HTTP Date 头（秒级）→ epoch ms；只读观察，失败返回 null。
  function parseServerDate(hdr) {
    try {
      const t = Date.parse(hdr);
      return isNaN(t) ? null : t;
    } catch (e) { return null; }
  }

  /* ---------- P1-2：请求头里的「组 / 步骤」 ---------- */
  // 写接口的请求头带 X-CAVE-Edit-Group / Step / Request-ID。
  // 没有它，我们只知道“发了一次 merge”，不知道它属于哪一组、第几步——
  // 组撤销到底可不可用、一次 merge 的边界在哪，都要靠这三个值。
  // 如实边界：头名属私有约定；没有就不写，不猜。
  function readHeader(headers, name) {
    if (!headers) return null;
    const want = String(name).toLowerCase();
    try {
      if (typeof headers.get === 'function') {
        const v = headers.get(name);
        if (v != null) return String(v);
      }
      if (Array.isArray(headers)) {
        for (let i = 0; i < headers.length; i++) {
          const p = headers[i];
          if (p && String(p[0]).toLowerCase() === want) return String(p[1]);
        }
        return null;
      }
      if (typeof headers === 'object') {
        for (const k in headers) {
          if (k.toLowerCase() === want) return String(headers[k]);
        }
      }
    } catch (e) { /* ignore */ }
    return null;
  }
  function editHeaders(headers) {
    const g = readHeader(headers, 'X-CAVE-Edit-Group');
    const s = readHeader(headers, 'X-CAVE-Edit-Step');
    const r = readHeader(headers, 'X-CAVE-Request-ID');
    if (g == null && s == null && r == null) return null;
    const out = {};
    if (g != null) out.edit_group = g.slice(0, 120);
    if (s != null) out.edit_step = s.slice(0, 40);
    // 注意：edit_request_id 是**服务端/前端给的**；
    // request_id 是我们本地生成的。两者不同源，不能互相冒充。
    if (r != null) out.edit_request_id = r.slice(0, 120);
    return out;
  }
  // 从回执文本里提取 operation_id（fetch 与 XHR 共用的宽松解析）。
  function extractOpId(txt) {
    try {
      const js = JSON.parse(txt);
      if (js && js.operation_id != null) return js.operation_id;
      if (js && js.data && js.data.operation_id != null) {
        return js.data.operation_id;
      }
    } catch (e) { /* 非 JSON 回执 */ }
    return null;
  }
  // 客户端请求 ID：本地生成，用于与后端 operation_id 绑成唯一事件链（B01/RB01）。
  // 没有后端回执透传时，它仍能把“提交动作 ↔ 回执 ↔ 关键帧”串起来。
  function newUuid() {
    try {
      if (window.crypto && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
      }
    } catch (e) { /* ignore */ }
    return 'req-' + Date.now().toString(36) + '-' +
      Math.random().toString(36).slice(2, 10);
  }
  function isSubmitKind(kind) {
    return kind === 'merge' || kind === 'split' || kind === 'undo'
      || kind === 'write';
  }
  // 若前端已在请求体里带 client_request_id/request_id，则直接采用（不另造），
  // 否则本地生成——这样 B01 的回执透传一旦上线，两边名字天然一致。
  function requestIdFromBody(body) {
    if (!body) return null;
    try {
      const js = typeof body === 'string' ? JSON.parse(body) : body;
      const v = js && (js.client_request_id || js.request_id);
      return (typeof v === 'string' && v.length >= 8) ? v : null;
    } catch (e) { return null; }
  }
  function requestIdFor(body) {
    return requestIdFromBody(body) || newUuid();
  }

  function hookFetch() {
    try {
      const orig = window.fetch;
      if (!orig || orig.__caveHooked) return;
      function wrapped() {
        const args = arguments;
        let url = '', method = 'GET', body = null, hdrs = null;
        try {
          const req = args[0];
          if (typeof req === 'string') url = req;
          else if (req && req.url) {
            url = req.url; method = req.method || 'GET';
            if (req.headers) hdrs = req.headers;
          }
          if (args[1] && args[1].method) method = args[1].method;
          if (args[1] && args[1].headers) hdrs = args[1].headers;
          if (args[1] && args[1].body &&
              typeof args[1].body === 'string') {
            body = args[1].body;
          }
        } catch (e) { /* ignore */ }
        const info = R.recording ? classifyApi(method, url) : null;
        const t0 = wall();
        const p = orig.apply(this, args);
        if (info) {
          try {
            const reqId = (info.write && isSubmitKind(info.kind))
              ? requestIdFor(body) : null;
            if (reqId) {
              R.pendingWrites.push({
                request_id: reqId, kind: info.kind, t_wall: t0,
                state_id: R.stateId,
              });
            }
            const reqEv = {
              kind: info.kind, write: !!info.write, method: String(method).toUpperCase(),
              url: String(url).slice(0, 300),
              body: body ? String(body).slice(0, 500) : null,
              request_id: reqId, state_id: R.stateId,
            };
            const eh = editHeaders(hdrs);
            if (eh) Object.assign(reqEv, eh);
            apiEmit('api_request', reqEv);
            if (reqId) {
              maybeCheckpoint(null, true);
              captureShot({ moment: 'pre_submit', request_id: reqId });
            }
            p.then(function (resp) {
              try {
                let sdate = null;
                try {
                  if (resp && resp.headers && resp.headers.get) {
                    sdate = parseServerDate(resp.headers.get('date'));
                  }
                } catch (e) { /* ignore */ }
                const respEv = {
                  kind: info.kind, write: !!info.write, status: resp.status,
                  url: String(url).slice(0, 300), ms: wall() - t0,
                  request_id: reqId, state_id: R.stateId,
                };
                if (sdate != null) respEv.server_date_ms = sdate;
                apiEmit('api_response', respEv);
                if (reqId) captureShot({ moment: 'post_submit',
                                         request_id: reqId });
                if (resp && resp.clone) {
                  resp.clone().text().then(function (txt) {
                    const op = extractOpId(txt);
                    if (op != null) {
                      apiEmit('api_operation_id', {
                        kind: info.kind, op_id: op, via: 'fetch',
                        request_id: reqId, status: resp.status,
                      });
                    }
                    // 「组 Undo / edit_groups」不是采集能定的：服务端到底有没有
                    // 这些能力，唯一依据是能力查询的**响应体**。看到就存一份。
                    if (CAPABILITY_RE.test(String(url))) {
                      apiEmit('api_capability', {
                        url: String(url).slice(0, 300), status: resp.status,
                        body: String(txt).slice(0, 4000),
                        truncated: String(txt).length > 4000,
                        state_id: R.stateId,
                      });
                    }
                  }).catch(function () {});
                }
              } catch (e) { /* ignore */ }
            }, function (err) {
              try {
                apiEmit('api_error', {
                  kind: info.kind, url: String(url).slice(0, 300),
                  err: String(err).slice(0, 120),
                  request_id: reqId, state_id: R.stateId,
                });
                if (reqId) captureShot({ moment: 'submit_failed',
                                         request_id: reqId });
              } catch (e) { /* ignore */ }
            });
          } catch (e) { /* ignore */ }
        }
        return p;
      }
      wrapped.__caveHooked = true;
      window.fetch = wrapped;
    } catch (e) { R.errors.push('hookFetch:' + e); }
  }
  function hookXHR() {
    try {
      const XO = window.XMLHttpRequest;
      if (!XO || XO.prototype.__caveHooked) return;
      const origOpen = XO.prototype.open;
      const origSend = XO.prototype.send;
      const origSetHdr = XO.prototype.setRequestHeader;
      // P1-2：把请求头先收下来（只在有要的那个头时才存，不建全量副本）
      if (typeof origSetHdr === 'function') {
        XO.prototype.setRequestHeader = function (name, value) {
          try {
            const ln = String(name || '').toLowerCase();
            if (ln.indexOf('x-cave-') === 0) {
              if (!this.__caveHdrs) this.__caveHdrs = {};
              this.__caveHdrs[name] = value;
            }
          } catch (e) { /* ignore */ }
          return origSetHdr.apply(this, arguments);
        };
      }
      XO.prototype.open = function (method, url) {
        try { this.__caveMeta = { method: method, url: url }; }
        catch (e) { /* ignore */ }
        return origOpen.apply(this, arguments);
      };
      // 只读观察请求体（不改写）：用于识别前端已带的 client_request_id。
      XO.prototype.send = function (body) {
        try {
          const meta = this.__caveMeta;
          if (typeof body === 'string') {
            this.__caveBody = body.slice(0, 2000);
          }
          if (meta && R.recording) {
            const info = classifyApi(meta.method, meta.url);
            if (info) {
              const t0 = wall(), xhr = this;
              const reqId = (info.write && isSubmitKind(info.kind))
                ? requestIdFor(this.__caveBody) : null;
              if (reqId) {
                R.pendingWrites.push({
                  request_id: reqId, kind: info.kind, t_wall: t0,
                  state_id: R.stateId, via: 'xhr',
                });
              }
              const reqEv = {
                kind: info.kind, write: !!info.write,
                method: String(meta.method).toUpperCase(),
                url: String(meta.url).slice(0, 300), via: 'xhr',
                request_id: reqId, state_id: R.stateId,
              };
              const eh = editHeaders(this.__caveHdrs);
              if (eh) Object.assign(reqEv, eh);
              apiEmit('api_request', reqEv);
              if (reqId) {
                maybeCheckpoint(null, true);
                captureShot({ moment: 'pre_submit', request_id: reqId });
              }
              xhr.addEventListener('loadend', function () {
                try {
                  let sdate = null;
                  try {
                    sdate = parseServerDate(
                      xhr.getResponseHeader('Date'));
                  } catch (e) { /* ignore */ }
                  const respEv = {
                    kind: info.kind, write: !!info.write, status: xhr.status,
                    url: String(meta.url).slice(0, 300),
                    ms: wall() - t0, via: 'xhr',
                    request_id: reqId, state_id: R.stateId,
                  };
                  if (sdate != null) respEv.server_date_ms = sdate;
                  apiEmit('api_response', respEv);
                  if (reqId) captureShot({ moment: 'post_submit',
                                           request_id: reqId });
                  // 写接口回执里提取 operation_id（锚点；只读解析）
                  if (info.write && info.kind !== 'split_preview'
                      && xhr.status === 200) {
                    let txt = null;
                    try { txt = xhr.responseText; } catch (e) { }
                    if (txt) {
                      const op = extractOpId(txt);
                      if (op != null) {
                        apiEmit('api_operation_id', {
                          kind: info.kind, op_id: op, via: 'xhr',
                          request_id: reqId, status: xhr.status,
                        });
                      }
                    }
                  }
                } catch (e) { /* ignore */ }
              });
            }
          }
        } catch (e) { /* ignore */ }
        return origSend.apply(this, arguments);
      };
      XO.prototype.__caveHooked = true;
    } catch (e) { R.errors.push('hookXHR:' + e); }
  }

  /* ---------- 截图（尽力而为） ---------- */
  // 在渲染帧内读取：两层 rAF（等上一帧渲染完成后立即取），
  // 提高 WebGL 未开启 preserveDrawingBuffer 时的捕获成功率。
  // 实测（2026-10-26，headless chromium）：readback 会返回**纯白图**且
  // dataURL 长度已超过 2000 字符，旧判据会把空白当成功。因此增加像素方差
  // 检查：近似纯色 ⇒ capture_status='blank_readback'，不写 image_ref。
  function dataUrlLooksBlank(url, cb) {
    try {
      const img = new Image();
      img.onload = function () {
        try {
          const w = 32, h = 32;
          const cv = document.createElement('canvas');
          cv.width = w; cv.height = h;
          const ctx = cv.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          const d = ctx.getImageData(0, 0, w, h).data;
          let mn = 255, mx = 0;
          for (let i = 0; i < d.length; i += 4) {
            const v = (d[i] + d[i + 1] + d[i + 2]) / 3;
            if (v < mn) mn = v;
            if (v > mx) mx = v;
          }
          cb((mx - mn) < 6);
        } catch (e) { cb(false); }
      };
      img.onerror = function () { cb(false); };
      img.src = url;
    } catch (e) { cb(false); }
  }

  // 取像素必须发生在 WebGL **绘制之后、合成之前**的同一个任务里。
  // 浏览器在合成后清空 drawing buffer（ng 没开 preserveDrawingBuffer），
  // 所以等 rAF / 下一帧再 toDataURL 只能拿到纯色——v0.6 把这个现象记成
  // blank_readback，但它并不是“插件永远做不到 R2”，而是取样时机错了。
  // 做法：包一层 draw* 调用，绘制返回后立即读。只在首次截图时安装，
  // 不截图就不付这个开销。
  const GL_DRAW_FNS = ['drawElements', 'drawArrays',
                       'drawElementsInstanced', 'drawArraysInstanced'];

  function ensureShotHook(c) {
    if (c.__caveShotHooked) return !!c.__caveGL;
    let gl = null;
    try { gl = c.getContext('webgl2') || c.getContext('webgl'); }
    catch (e) { gl = null; }
    if (!gl) { c.__caveShotHooked = true; return false; }
    c.__caveShotHooked = true;
    c.__caveGL = gl;
    GL_DRAW_FNS.forEach(function (name) {
      const orig = gl[name];
      if (typeof orig !== 'function') return;
      gl[name] = function () {
        let out;
        try {
          out = orig.apply(gl, arguments);
        } finally {
          if (R.pendingShot === c && R.pendingShotFire) {
            const fire = R.pendingShotFire;
            R.pendingShot = null; R.pendingShotFire = null;
            try { fire(); } catch (e) { /* ignore */ }
          }
        }
        return out;
      };
    });
    return true;
  }

  /* ---------- 图像内容：等画完 + 内容指纹（L1） ---------- */
  // 为什么要内容判据而不是固定睡眠：加载时间随网络、mip、缩放、图层数、缓存
  // 命中而变，任何“等 N 秒”都会在某些场景太长、另一些太短。这里只做两件事：
  //   ① 把“画完了没有”变成**可记录的量**（colored/mean/std/thumb16_sha）；
  //   ② 用同一份参数（随包走）决定等不等，而不是写死一个数。
  // 另一条不能省：**只看彩色占比会把“上一帧的旧画面”当成已画完**——刚跳转时
  // 画布上还是上一处（本来就有颜色），门禁会立即放行。所以还要看 16×16 指纹
  // “相对上一张变过”，且连续两次不再变；否则拍到的是旧画面 + 新的 state（错配）。
  const PAINT_STABLE_SAMPLES = 3;   // 连续 N 次（指纹/彩色/亮度全同）→ 已稳定
  // 稳定性不能要求“像素位完全相等”：EM 瓦片是流式进画布的，几秒内每帧都会
  // 微变，位相等会永远达不到（实测彩色占比 0.70 却判 settled=false）。
  // 用 16×16 拇指图的**平均绝对差**当距离：≤ 2 算没变，> 4 算变过了。
  const PAINT_THUMB_SAME = 2.0;
  const PAINT_THUMB_CHANGED = 4.0;

  function thumbDiff(a, b) {
    if (!a || !b || a.length !== b.length) return 999;
    let s = 0;
    for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]);
    return s / a.length;
  }

  function paintRule() {
    return {
      colored_min: Number(R.config.keyframe_paint_colored) || 0,
      stable_eps: Number(R.config.keyframe_paint_stable_eps) || 0,
      timeout_ms: Math.max(0, Number(R.config.keyframe_paint_ms) || 0),
    };
  }

  // 降采样后统计：彩色占比（分割叠加在不在）+ 亮度均值/方差（空帧）+ 16×16 结构指纹。
  // 灰度均值相同、结构不同的两张图，靠 thumb16_sha 才分得开。
  function contentStats(c) {
    try {
      const W = 160, H = 100;
      const t = document.createElement('canvas');
      t.width = W; t.height = H;
      const x = t.getContext('2d');
      x.clearRect(0, 0, W, H);
      x.drawImage(c, 0, 0, W, H);
      const d = x.getImageData(0, 0, W, H).data;
      let col = 0, n = 0, sum = 0, sum2 = 0;
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], g = d[i + 1], b = d[i + 2];
        const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
        if (mx - mn > 12) col++;
        const v = (r + g + b) / 3;
        sum += v; sum2 += v * v; n++;
      }
      const mean = n ? sum / n : 0;
      const std = n ? Math.sqrt(Math.max(0, sum2 / n - mean * mean)) : 0;
      const t2 = document.createElement('canvas');
      t2.width = 16; t2.height = 16;
      const x2 = t2.getContext('2d');
      x2.clearRect(0, 0, 16, 16);
      x2.drawImage(c, 0, 0, 16, 16);
      const d2 = x2.getImageData(0, 0, 16, 16).data;
      let s2 = '';
      const thumb = new Array(256);
      for (let i = 0, p = 0; i < d2.length; i += 4, p++) {
        const v = ((d2[i] + d2[i+1] + d2[i+2]) / 3) | 0;
        s2 += String.fromCharCode(v);
        thumb[p] = v;
      }
      return {
        colored: +(n ? col / n : 0).toFixed(4),
        mean: +mean.toFixed(2), std: +std.toFixed(2),
        thumb16_sha: fnv1a(s2), thumb16: thumb, sample: [W, H],
      };
    } catch (e) { return null; }
  }

  // 等“画面画出来”。budget=0 表示不等（例如 pre_submit：那张图的语义是
  // “编辑执行前”，等下去可能拍到提交之后——那不是模糊，是错的证据）。
  function waitPainted(c, budgetMs, opts) {
    const rule = paintRule();
    const budget = Math.max(0, Number(budgetMs) || 0);
    const prevArr = (opts && opts.prev_thumb_arr) || null;
    const needChange = !!(opts && opts.require_change && prevArr);
    return new Promise(function (resolve) {
      const t0 = mono();
      let prev = null, stableHits = 0;
      function done(v) { resolve(v); }
      function tick() {
        const st = contentStats(c);
        const waited = Math.round(mono() - t0);
        const coloredOk = !!(st && st.colored >= rule.colored_min);
        const changed = !needChange
          || !!(st && thumbDiff(st.thumb16, prevArr) > PAINT_THUMB_CHANGED);
        if (st && prev) {
          const still = thumbDiff(st.thumb16, prev.thumb16) <= PAINT_THUMB_SAME
            && Math.abs(st.colored - prev.colored) <= rule.stable_eps
            && Math.abs(st.mean - prev.mean) <= 1.0;
          stableHits = still ? stableHits + 1 : 0;
        }
        prev = st;
        const stable = stableHits >= (PAINT_STABLE_SAMPLES - 1);
        // settled 的语义是“这张图确实对应这个状态”：画面已画出（彩色达标）
        // 且相对上一张变过了。**不要求静止**，因为拖动中的 checkpoint 本来就
        // 永远静不下来；这时 settled=true、stable=false，下游知道“差一帧运动”。
        if (coloredOk && changed) {
          done({ settled: true, stable: stable, changed: changed, stats: st,
                 waited_ms: waited, timed_out: false });
          return;
        }
        if (waited >= budget) {
          // 等不到就照实记：不是“成功”，也不是错误——
          // 可能是画面已静止但视图确实没变（同一视角连拍两次）
          done({ settled: false, stable: stable, changed: changed, stats: st,
                 waited_ms: waited, timed_out: budget > 0 });
          return;
        }
        setTimeout(tick, 120);
      }
      tick();
    });
  }

  function captureShot(meta) {
    meta = meta || {};
    R.shotSeq += 1;
    const shotId = 'shot_' + R.shotSeq;
    const moment = meta.moment || 'manual';
    const reqId = meta.request_id || null;
    const sid = R.stateId;
    // 为什么拍的、变化多大：事后看 shots.jsonl 就能解释每张图，
    // 不用回头猜"这张是缩放还是转视角"。
    const detail = meta.detail ? JSON.parse(JSON.stringify(meta.detail)) : null;
    let c = null;
    try {
      const canvases = Array.from(document.querySelectorAll('canvas'))
        .filter(x => x.width > 100 && x.height > 100)
        .sort((a, b) => (b.width * b.height) - (a.width * a.height));
      if (!canvases.length) {
        R.shots.push({ shot_id: shotId, t_wall: wall(), w: null, h: null,
                       ok: false, capture_status: 'no_canvas', moment: moment,
                       request_id: reqId, state_id: sid, detail: detail,
                       dataURL: null });
        emit('keyframe', { shot_id: shotId, moment: moment,
                           request_id: reqId, state_id: sid, detail: detail,
                           capture_status: 'no_canvas', image_ref: null });
        return Promise.resolve({ ok: false, note: 'no canvas' });
      }
      c = canvases[0];
    } catch (e) {
      R.errors.push('shot:' + e);
      return Promise.resolve({ ok: false, note: String(e) });
    }
    // pre_submit 不等；未打 GPU 补丁时 readback 本来就拿不到像素（会记 blank），
    // 等画完只是白等，也一并跳过。其余时机按 paintRule().timeout_ms 等。
    const budget = (moment === 'pre_submit' || !window.__caveGLPatched)
      ? 0 : paintRule().timeout_ms;
    // 视图变过就必须看到画面变过：拿“上一张的指纹”当参数，而不是拿时间猜。
    let requireChange = false;
    try {
      const st0 = fullState();
      requireChange = !!(R.lastShotThumbArr &&
        (!st0 || !st0.state_sha || st0.state_sha !== R.lastShotStateSha));
    } catch (e) { requireChange = !!R.lastShotThumb; }
    let paint = null;
    return new Promise(function (resolve) {
      let settled = false;
      function finishWhenPainted(via) {
        waitPainted(c, budget, { require_change: requireChange,
                                 prev_thumb_arr: R.lastShotThumbArr })
          .then(function (wp) {
            paint = wp;
            finish(via);
          });
      }
      function finish(via) {
        if (settled) return;
        settled = true;
        R.pendingShot = null; R.pendingShotFire = null;
        // L1：快门这一刻的 state（不复用 500ms 前那次采样）+ 图像内容指纹。
        // 图与 state 的绑定是**构造出来的**，不靠时间猜。
        let snap = null;
        try { snap = fullState(); } catch (e) { snap = null; }
        const stateInStream = !!(snap && snap.state_sha
          && snap.state_sha === R.lastStateSha);
        const content = (paint && paint.stats) ? paint.stats : contentStats(c);
        // 拇指图数组只用于运行时比较（“变过没/稳了没”），不落盘（每张 256 字节）
        const thumbArr = content ? content.thumb16 : null;
        const contentOut = content ? Object.assign({}, content) : null;
        if (contentOut) delete contentOut.thumb16;
        const paintInfo = {
          settled: paint ? !!paint.settled : null,
          stable: paint ? !!paint.stable : null,
          waited_ms: paint ? paint.waited_ms : 0,
          timed_out: paint ? !!paint.timed_out : false,
        };
        let data = null, note = '';
        try {
          data = c.toDataURL('image/png');
        } catch (e) { note = String(e); }
        const longEnough = !!data && data.length > 2000;
        const done = function (blank) {
          const ok = longEnough && !blank;
          // 图像本身可读，但画面还没画完（彩色占比低于规则）⇒ 标 painted_partial：
          // 图保留、但下游不得把它当“当时所见”的证据（也不能拿去比回放）。
          const partialPaint = !!(ok && content
            && content.colored < paintRule().colored_min);
          const status = !ok ? (note ? 'error'
            : (blank ? 'blank_readback' : 'too_small'))
            : (partialPaint ? 'painted_partial' : 'captured');
          R.shots.push({
            shot_id: shotId, t_wall: wall(), w: c.width, h: c.height, ok,
            note: ok ? '' : (note
              || (blank ? 'readback 近似纯色（取样时机不在绘制后）' : 'dataURL 过短')),
            capture_status: status, readback_via: via,
            moment: moment, request_id: reqId,
            state_id: sid, detail: detail, dataURL: ok ? data : null,
            // ---- L1：图 ↔ state 的绑定与完整性参数 ----
            state_sha: snap ? snap.state_sha : null,
            state_in_stream: stateInStream,
            state_age_ms: R.lastStateAt ? (wall() - R.lastStateAt) : null,
            state_partial: snap ? snap.state_partial : null,
            state_truncated: snap ? snap.state_truncated : null,
            // 不在流里的 state 必须随图存一份，否则“那一刻”永久丢了（无法回放）
            state: stateInStream ? null : (snap ? snap.state : null),
            paint_settled: paintInfo.settled,
            paint_stable: paintInfo.stable,
            paint_changed: paint ? paint.changed : null,
            paint_waited_ms: paintInfo.waited_ms,
            paint_timed_out: paintInfo.timed_out,
            content: contentOut,
          });
          // 记住这张的指纹与它的 state：下一张据此判断“画面到底变了没”
          R.lastShotThumb = contentOut ? contentOut.thumb16_sha : null;
          R.lastShotThumbArr = thumbArr;
          R.lastShotStateSha = snap ? snap.state_sha : null;
          emit('keyframe', {
            shot_id: shotId, moment: moment, request_id: reqId,
            state_id: sid, detail: detail, capture_status: status, readback_via: via,
            image_ref: (ok ? (shotId + '.png') : null),
            w: c.width, h: c.height,
            state_sha: snap ? snap.state_sha : null,
            state_in_stream: stateInStream,
            paint_settled: paintInfo.settled,
            paint_waited_ms: paintInfo.waited_ms,
            paint_colored: contentOut ? contentOut.colored : null,
          });
          bump('shot'); updateUI();
          resolve({ ok: ok, w: c.width, h: c.height, note: note,
                    status: status, via: via });
        };
        if (!longEnough) { done(false); return; }
        dataUrlLooksBlank(data, done);
      }

      const hooked = window.__caveGLPatched ? false : ensureShotHook(c);
      if (hooked) {
        // 等下一帧的绘制：画面变了才有 draw；静止时靠超时兜底，
        // 兜底拿到的大概率是清空后的纯色，那就如实记 blank，不假装成功。
        R.pendingShot = c;
        R.pendingShotFire = function () { finishWhenPainted('gl_draw'); };
        setTimeout(function () { finishWhenPainted('timeout_no_draw'); }, 700);
        return;
      }
      try {
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(function () {
            requestAnimationFrame(function () {
              finishWhenPainted(window.__caveGLPatched
                ? 'raf_preserved' : 'raf');
            });
          });
        } else { finishWhenPainted('sync'); }
      } catch (e) { finishWhenPainted('sync'); }
    });
  }

  // RF05：viewer build 与 layout。build 拿不到就写 null + method，
  // **不用“猜的版本号”冒充**——R2 验收明写“不允许串版本”。
  function buildInfo() {
    let ng = null, method = 'unavailable';
    try {
      const v = window.viewer || {};
      ng = v.version || (v.constructor && v.constructor.version) || null;
      if (ng) method = 'viewer.version';
      if (!ng) {
        const src = Array.from(document.scripts || [])
          .map(s => s.src || '').join(' ');
        const m = src.match(/(\d+\.\d+\.\d+)/);
        if (m) { ng = m[1]; method = 'script_url_guess'; }
      }
    } catch (e) { /* ignore */ }
    return { neuroglancer: ng, method: method };
  }

  /* ---------- 会话控制 ---------- */
  // 会话名：目录名就是它（`results/manual_sessions/<session_id>/`）。
  // 优先级：start({session_id}) > config.session_name（含 URL 参数）> 自动生成。
  // 只保留 [A-Za-z0-9._-]，其余换成 `_`；没有 `sess_` 前缀就补上。
  function sessionIdFor(opts) {
    const want = (opts && opts.session_id) || R.config.session_name;
    if (want) {
      // 保留中文等可见字符（目录名要能看懂）；只挡路径危险字符与空白。
      let safe = String(want).trim()
        .replace(/[\/\\:*?"<>|#]+/g, '_')
        .replace(/\s+/g, '_')
        .replace(/^[._]+|[._]+$/g, '');
      if (safe) {
        safe = safe.slice(0, 80);
        return safe.indexOf('sess_') === 0 ? safe : ('sess_' + safe);
      }
    }
    return 'sess_' +
      new Date().toISOString().replace(/[:.]/g, '-') + '_' +
      Math.random().toString(36).slice(2, 7);
  }
  function start(opts) {
    if (R.recording) stop();
    R.recording = true;
    R.sessionId = sessionIdFor(opts);
    R.t0 = wall();
    R.events = []; R.notes = []; R.shots = []; R.counters = {};
    R.seq = 0; R.overflow = null; R.draftSeq = 0; R.draftShots = {};
    R.lastSemantic = null; R.lastSemanticShot = {}; R.dwell = null;
    R.semanticCounts = {}; R.scaleHistory = []; R.lastEval = null;
    R.scaleT0 = null;
    R.stateId = 0; R.shotSeq = 0;
    R.gesture = null; R.wheelBurst = null; R.lastMoveWall = 0; R.nAcked = 0;
    R.gaps = []; R.pendingWrites = []; R.initialCheckpoint = null;
    R.lastCheckpointWall = 0; R.lastStatusSig = null;
    R.hidden = !!document.hidden; R.focused = document.hasFocus();
    R.online = (navigator.onLine !== false);
    R.last = { state: null, sel: null, mouse: null, multicut: null };
    hookFetch();   // 防页面初始化后替换 fetch/XHR（幂等）
    hookXHR();
    tryViewer();
    const init = extractViewpoint();
    emit('session_start', {
      session_id: R.sessionId,
      collector_version: COLLECTOR_VERSION,
      page: { url: location.href, title: document.title },
      viewer_detected: !!R.viewer,
      userAgent: navigator.userAgent,
      viewport: viewportInfo(),
      // RF05：build 与 layout 是 R2 版本比对的基准
      build: buildInfo(),
      initial_state: init ? init.vp : null,
      // D10：这份数据是按多少毫秒采的，写清楚，别让下游猜默认值
      config: samplingConfig(),
      task_id: null, actor_alias: null,
    });
    checkpoint('session_start');
    resetSampleTimer();
    if (R.config.draft_ms > 0) {
      R.timers.push(setInterval(flushDraft, R.config.draft_ms));
    }
    R.timers.push(setInterval(tryViewer, 2000));
    if (R.config.semantic_watch) attachStateWatch();
    updateUI();
    return R.sessionId;
  }

  // 草稿落盘：只发 seq 大于已发位置的事件（增量），失败下次重发。
  // 正式 /save 成功后服务端会删掉同 session 的草稿。
  function flushDraft() {
    const url = window.__caveDraftUrl;
    if (!url || !R.recording || !R.sessionId) return;
    const pending = R.events.filter(e => (e.seq || 0) > R.draftSeq);
    // 图也增量：一次最多 8 张（base64 后约 0.8 MB/张，8 张 < 服务端 12 MB 上限）
    const sent = R.draftShots || (R.draftShots = {});
    const newShots = R.shots.filter(function (s) {
      return s && s.shot_id && !sent[s.shot_id]
        && (s.dataURL || '').indexOf('data:image/') === 0;
    }).slice(0, 8);
    if (!pending.length && !newShots.length) return;
    const payload = {
      session_id: R.sessionId,
      collector_version: COLLECTOR_VERSION,
      page: { url: location.href, title: document.title },
      time: { start_wall_ms: R.t0, end_wall_ms: wall() },
      config: samplingConfig(),
      counts: Object.assign({}, R.counters),
      events: pending,
      notes: R.notes.slice(),
      shots: newShots.map(function (s) {
        // 草稿也必须带 state 绑定与内容判据：否则恢复出来的图只剩 png，
        // 不知道它对应哪份 state、当时有没有画完。字段与 shotOut() 同源。
        return {
          shot_id: s.shot_id, t_wall: s.t_wall, w: s.w, h: s.h, ok: s.ok,
          note: s.note, capture_status: s.capture_status,
          readback_via: s.readback_via || null, moment: s.moment,
          request_id: s.request_id, state_id: s.state_id,
          state_sha: s.state_sha, state_in_stream: !!s.state_in_stream,
          state_age_ms: (s.state_age_ms === undefined ? null : s.state_age_ms),
          state_partial: s.state_partial || null,
          state_truncated: s.state_truncated || null,
          paint_settled: (s.paint_settled === undefined ? null : s.paint_settled),
          paint_stable: !!s.paint_stable,
          paint_changed: (s.paint_changed === undefined ? null : s.paint_changed),
          paint_waited_ms: (s.paint_waited_ms || 0),
          paint_timed_out: !!s.paint_timed_out,
          content: s.content || null,
          state: (!s.state_in_stream && s.state) ? s.state : null,
          dataURL: s.dataURL,
        };
      }),
    };
    const prev = R.draftSeq;
    if (pending.length) R.draftSeq = pending[pending.length - 1].seq;
    newShots.forEach(function (s) { sent[s.shot_id] = 1; });
    const rollback = function () {
      R.draftSeq = prev;
      newShots.forEach(function (s) { delete sent[s.shot_id]; });
    };
    try {
      fetch(url, { method: 'POST', body: JSON.stringify(payload) })
        .then(function (r) { if (!r.ok) rollback(); })
        .catch(rollback);
    } catch (e) { rollback(); }
  }

  function stop() {
    if (!R.recording) return;
    // 先冲一次草稿：即使随后保存失败，服务端也有到最后一条的副本
    try { flushDraft(); } catch (e) {}
    // 收尾：把未结束的手势与滚轮 burst 先落成事件，否则轨迹尾部丢失
    if (R.gesture) gestureEnd();
    flushWheelBurst();
    checkpoint('session_stop');
    R.recording = false;
    emit('session_stop', {
      counts: Object.assign({}, R.counters),
      overflow: R.overflow ? JSON.parse(JSON.stringify(R.overflow)) : null,
      gaps: R.gaps.slice(),
      pending_writes: R.pendingWrites.slice(),
      state_id: R.stateId,
    });
    R.timers.forEach(t => clearInterval(t));
    R.timers = [];
    updateUI();
  }

  // 备注（显式表达；可选、可跳过）。类型留给离线标注，不在此处推断。
  // textArg：页面内自建输入框传入文本。为什么不能只靠 window.prompt：
  // 自动化/内嵌浏览器里 prompt 会被宿主默认 dismiss（Playwright 就是这样），
  // 表现为“点了备注窗口一闪就没了”、文本永远为空。
  function addNote(textArg) {
    let text = '';
    try {
      text = (textArg == null) ? '' : String(textArg).trim();
      if (!text) text = window.prompt('备注（将绑定当前视角）:') || '';
    } catch (e) { /* ignore */ }
    if (!text) return;
    let vp = null;
    try { vp = extractViewpoint(); } catch (e) { /* ignore */ }
    const noteId = 'note_' + (R.notes.length + 1);
    R.notes.push({
      note_id: noteId, t_wall: wall(), text: text,
      state_id: R.stateId, viewpoint: vp ? vp.vp : null,
    });
    emit('note', { text: text, note_id: noteId, state_id: R.stateId });
  }

  // 能力声明：直接回答“哪些字段拿到了、哪些不能”。私有 fork 接入后
  // 只能把状态从 not_available 改成 available，不能删掉该键。
  // 采样配置快照：session.start / 导出包 / capabilities 都用它，保证三处一致
  function samplingConfig() {
    return {
      move_sample_ms: R.config.move_sample_ms,
      move_mode: R.config.move_sample_ms <= 0 ? 'full' : 'sampled',
      wheel_burst_gap_ms: R.config.wheel_burst_gap_ms,
      state_interval_ms: R.config.state_interval_ms,
      state_mode: R.config.state_interval_ms <= 0 ? 'watch_only'
        : (R.config.semantic_watch ? 'watch+poll' : 'poll_only'),
      semantic_watch: !!R.config.semantic_watch,
      semantic_debounce_ms: R.config.semantic_debounce_ms,
      semantic_keyframe: !!R.config.semantic_keyframe,
      dwell_ms: R.config.dwell_ms,
      draft_ms: R.config.draft_ms,
      max_events: R.config.max_events,
      keyframe_on_checkpoint: !!R.config.keyframe_on_checkpoint,
      full_state_on_change: !!R.config.full_state_on_change,
      // “算画完”的规则随数据走：回放端读同一份参数，不各写一套
      keyframe_paint: paintRule(),
      // 端点（down/up/click/enter/leave）始终全量，不受上面影响
      endpoints: 'full',
      note: 'move_sample_ms=0 为全量；可用 ?cave_move_ms= 或 R.setConfig() 修改',
    };
  }

  // 插件形态下的能力声明。对齐清单 RF01–RF15：
  //   available     = 插件就能采，已验证
  //   partial       = 能采但是近似值（必须看 source/method 字段）
  //   needs_fork    = 插件做不到，需改 Neuroglancer 源码
  //   needs_backend = 需私有 CAVE 配合
  // 这份声明是给下游的“不许当真的字段”清单，也同步写进 docs/PLUGIN_GAPS.md。
  function capabilities() {
    return {
      state_stream: 'available（RF06）',
      state_checkpoint: 'available（RF06；R1 基准）',
      nm_per_px: 'available（derived: ' + NM_PER_PX_METHOD + '）',
      viewport_px_and_dpr: 'available',
      layer_source_visible_opacity: 'available（仅基础字段）',
      selection_ids: 'partial（RF07：每层前 8 个；supervoxel 与 root 未区分）',
      input_pointer: 'available（RF01 端点全量；move 50ms 采样）',
      input_wheel: 'available（RF03；含 delta_mode/modifiers）',
      gesture: 'available（RF02+PP02：drag/click/zoom 机械切分，带 start/end view ref）',
      action_id: 'needs_fork（RF04：读不到 action dispatcher；记 key_combo + action_source=unmapped）',
      tool_state: 'needs_fork（RF07：读不到 tool handler）',
      draft_source_sink: 'partial（layer.multicut 防御式探测，以 fork 为准）',
      preview_attempt: 'partial（RF08：能观察 split_preview 请求，不知道预览结果语义）',
      client_request_id: 'partial（本地生成；需后端回显才能绑 op_id）',
      edit_timeout_retry: 'needs_backend（RF08：无服务端补偿查询）',
      keyframe: 'partial（RF13：能取像素，但须启动期补丁；未补丁时回退 gl_draw 并如实记 blank_readback）',
      keyframe_state_binding: 'available（L1：快门时钉 state，state_sha 不一致就随图存完整 state）',
      keyframe_content: 'available（colored/mean/std/thumb16_sha；完整性用内容判，不用时间猜）',
      keyframe_paint_rule: 'partial（等画完用彩色占比近似；真解是 RF10 V_loaded）',
      visual_replay_r2: (window.__caveGLPatched
        ? 'available（RF13/Q09=B：cave_gpu_patch 保住帧缓冲，关键帧与回放可直接用）'
        : 'needs_startup_patch（RF13/Q09=B：需以 document-start 注入 cave_gpu_patch.js）'),
      gpu_patch: window.__caveGLPatched ? 'applied' : 'missing',
      panel_id: 'partial（RF01：只有 canvas_index+尺寸，无 panel 身份）',
      v_view: 'partial（RF09：2D 视口+nm/px；3D 视锥/裁剪未采）',
      v_loaded: 'needs_fork（RF10：需 chunk manager hooks）',
      v_focus: 'available（RF11：中心位置/选中/命中）',
      v_action: 'partial（RF12：点击位置有，工具作用位置需 fork）',
      heartbeat: 'not_emitted（F26/RF14 的“空闲”改由 derived/episodes 的 idle_segments 从输入时间戳精确计算；周期心跳分不清“人走了”与“采集器死了”）',
      sampling: (R.config.move_sample_ms <= 0
        ? 'full（move 全量；端点也是全量）'
        : 'sampled（move ' + R.config.move_sample_ms +
          'ms；端点全量）') + ' · 可配置 ?cave_move_ms=0|50|… 或 R.setConfig()',
      full_state: (R.config.full_state_on_change
        ? 'available（每个状态变化都带完整 state → 任意时刻可恢复）'
        : 'off（只记摘要；回放只能靠 checkpoint 剖点）'),
      upload_ack_retry: 'needs_backend（RF14/F27：本地队列占位，无 ack 通道）',
      ng_build: 'partial（RF05：viewer.version 或 script_url_guess，拿不到为 null）',
      chunk_load_state: 'needs_fork（RF10：需 chunk hooks）',
    };
  }

  // shot → 落盘结构。L1 新字段（state 绑定 / 内容指纹 / 等待结果）必须原样带出：
  // 下游靠它们判断“这张图是不是当时那一帧、当时画完了没有”。
  function shotOut(s, withImage) {
    const o = {
      shot_id: s.shot_id, t_wall: s.t_wall, w: s.w, h: s.h, ok: s.ok,
      note: s.note, capture_status: s.capture_status,
      readback_via: s.readback_via || null,
      moment: s.moment, request_id: s.request_id, state_id: s.state_id,
      state_sha: s.state_sha || null,
      state_in_stream: !!s.state_in_stream,
      state_age_ms: (s.state_age_ms === undefined ? null : s.state_age_ms),
      state_partial: s.state_partial || null,
      state_truncated: s.state_truncated || null,
      paint_settled: (s.paint_settled === undefined ? null : s.paint_settled),
      paint_stable: !!s.paint_stable,
      paint_changed: (s.paint_changed === undefined ? null : s.paint_changed),
      paint_waited_ms: (s.paint_waited_ms || 0),
      paint_timed_out: !!s.paint_timed_out,
      content: s.content || null,
    };
    // 不在事件流里的 state 必须随图一起走（否则这张图无法精确回放）
    if (!o.state_in_stream && s.state) o.state = s.state;
    if (withImage) o.dataURL = s.dataURL || null;
    return o;
  }

  function buildPackage() {
    return {
      schema: SCHEMA,
      schema_base: SCHEMA_BASE,
      collector_version: COLLECTOR_VERSION,
      config: samplingConfig(),
      session_id: R.sessionId,
      page: {
        url: location.href, title: document.title,
        userAgent: navigator.userAgent,
      },
      viewer: { detected: !!R.viewer },
      time: {
        start_wall_ms: R.t0, end_wall_ms: wall(),
        tz_offset_min: new Date().getTimezoneOffset(),
      },
      collector: {
        version: COLLECTOR_VERSION,
        capabilities: capabilities(),
        gaps: R.gaps.slice(),
        overflow: R.overflow ? JSON.parse(JSON.stringify(R.overflow)) : null,
        pending_writes: R.pendingWrites.slice(),
        initial_checkpoint: R.initialCheckpoint
          ? { state_id: R.initialCheckpoint.state_id,
              t_wall: R.initialCheckpoint.t_wall }
          : null,
        n_state_changes: R.stateId,
      },
      counts: Object.assign({}, R.counters),
      events: R.events,
      notes: R.notes,
      shots: R.shots.map(s => shotOut(s, false)),
      errors: R.errors,
    };
  }

  // 含图像的全量导出（交给 cave_session_pack 落成目录契约）。
  function buildPackageWithImages() {
    const p = buildPackage();
    p.shots = R.shots.map(s => shotOut(s, true));
    p.initial_checkpoint_state = R.initialCheckpoint
      ? R.initialCheckpoint.state : null;
    return p;
  }

  function download() {
    try {
      const blob = new Blob([JSON.stringify(buildPackage(), null, 1)],
        { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (R.sessionId || 'session') + '.json';
      document.body.appendChild(a); a.click(); a.remove();
    } catch (e) { R.errors.push('download:' + e); }
  }

  /* ---------- 小面板 ---------- */
  let ui = null;
  function updateUI() {
    if (!ui) return;
    if (ui.head) {
      ui.head.textContent = 'CAVE Recorder v' + COLLECTOR_VERSION +
        ' · ' + (R.config.move_sample_ms <= 0
          ? '全量' : R.config.move_sample_ms + 'ms');
    }
    const drop = (R.counters.dropped || 0) +
      (R.counters.checkpoint_dropped || 0);
    ui.count.textContent = '事件 ' + R.events.length +
      (drop ? ' · 丢弃 ' + drop : '') +
      (R.recording ? ' · 录制中' : ' · 未录制');
    ui.dot.style.background = R.recording ? '#e33'
      : (R.viewer ? '#3c3' : '#999');
    ui.btnStart.disabled = R.recording;
    ui.btnStop.disabled = !R.recording;
  }
  function makeUI() {
    try {
      ui = {};
      const d = document.createElement('div');
      // 自己的 UI 必须带 data-cave-ui：否则点「备注/截图」会被当成画布点击录进去
      // （isOwnUI 只认这个属性），回放时就会凭空多点一下。
      d.id = 'cave-recorder-panel';
      d.setAttribute('data-cave-ui', '1');
      d.style.cssText = 'position:fixed;right:10px;bottom:10px;' +
        'z-index:2147483647;background:rgba(20,20,20,.85);color:#eee;' +
        'font:12px/1.5 Consolas,monospace;padding:8px 10px;' +
        'border-radius:8px;min-width:190px;';
      ui.dot = document.createElement('span');
      ui.dot.style.cssText = 'display:inline-block;width:8px;height:8px;' +
        'border-radius:50%;background:#999;margin-right:6px;';
      const head = document.createElement('span');
      ui.head = head;
      head.textContent = 'CAVE Recorder v' + COLLECTOR_VERSION;
      ui.count = document.createElement('div');
      ui.count.textContent = '等待检测 viewer…';
      const row = document.createElement('div');
      row.style.marginTop = '6px';
      function mkBtn(label, fn) {
        const b = document.createElement('button');
        b.textContent = label;
        b.style.cssText = 'margin:0 4px 0 0;font:11px monospace;';
        b.onclick = fn; row.appendChild(b); return b;
      }
      ui.btnStart = mkBtn('开始', () => start());
      ui.btnStop = mkBtn('停止', () => stop());
      ui.btnNote = mkBtn('备注', () => addNote());
      ui.btnShot = mkBtn('截图', () => {
        captureShot().then((r) => {
          ui.count.textContent = '截图: ' + JSON.stringify(r);
          // 图像数据与会话包分开：成功后立即下载 PNG 落盘
          // （导出包只含 meta；截图为"所见"的唯一直接证据，不可丢失）
          const last = R.shots[R.shots.length - 1];
          if (last && last.dataURL) {
            try {
              const a = document.createElement('a');
              a.href = last.dataURL;
              a.download = (R.sessionId || 'session') +
                '_shot_' + last.t_wall + '.png';
              document.body.appendChild(a); a.click(); a.remove();
            } catch (e) { R.errors.push('shot-dl:' + e); }
          }
        });
      });
      ui.btnDl = mkBtn('导出', () => download());
      d.appendChild(ui.dot); d.appendChild(head);
      d.appendChild(ui.count); d.appendChild(row);
      document.documentElement.appendChild(d);
      updateUI();
    } catch (e) { R.errors.push('ui:' + e); }
  }

  hookFetch();
  hookXHR();
  tryViewer();
  makeUI();
  R.start = start; R.stop = stop;
  R.export = buildPackage;
  R.exportJSON = function () { return JSON.stringify(buildPackage()); };
  R.exportWithImages = buildPackageWithImages;
  R.exportWithImagesJSON = function () {
    return JSON.stringify(buildPackageWithImages());
  };
  // 对外 API 默认算"手动"：面板按钮、控制台调用都是人点的；
  // 内部（maybeCheckpoint）自己传 'periodic' / 'state_change'。
  R.checkpoint = function (reason) { return checkpoint(reason || 'manual'); };
  R.captureShot = captureShot;
  R.addNote = addNote;
  // 外部前端模块（如 cave_workbench.user.js）的唯一写入口。
  // 只暴露 emit，不暴露 R.events 直写：保证 seq/计数/UI 一致。
  R.emit = function (type, data) { emit(type, data); };
  R.isRecording = function () { return !!R.recording; };
  // 外部模块需要"当前看到哪"时统一走这里，不要自己去读 viewer：
  // 字段语义（单位换算/朝向字段名）只在 recorder 里定义一次。
  R.viewpoint = function () {
    const r = extractViewpoint();
    return r ? r.vp : null;
  };
  // RF13：外部（工作台 / 自动化）主动取关键帧。返回 Promise，
  // 结果里带 capture_status 与 readback_via，调用方必须看这两个字段，
  // 不能只看“有没有 shot_id”。
  R.keyframe = function (moment) {
    if (!R.recording) return Promise.resolve({ ok: false, note: 'not recording' });
    return captureShot({ moment: moment || 'manual' });
  };
  // 外部要求“现在存一个 state 快照”。demo/回放自检靠它把 checkpoint
  // 钉在人关心的时间点（而不是等 30s 周期）。
  R.checkpointNow = function (reason) {
    return checkpoint(reason || 'manual');
  };
  // P2-* 的唯一入口：谁能读到私有状态，谁就注册一个**只读**函数。
  // 约定见 docs/私有扩展接入.md §3.1；采不到就不注册，零开销。
  R.registerProvider = registerProvider;
  R.unregisterProvider = unregisterProvider;
  // 注意：不要叫 R.providers —— 那是内部 Map（name → {fn,...}），改名会覆盖它
  R.listProviders = function () {
    const o = {};
    for (const k in R.providers) {
      o[k] = { n: R.providers[k].n, err: R.providers[k].err };
    }
    return o;
  };
  // D10 日志粒度：运行时改采样。
  // 已录制会话里改就发 collector.config，**不假装整段同粒度**；
  // 下游看到多个 config 就必须分段读。
  R.setConfig = function (patch) {
    const before = Object.assign({}, R.config);
    const next = Object.assign({}, R.config);
    if (patch && typeof patch === 'object') {
      const m = clampInt(patch.move_sample_ms, 0, 1000);
      if (m !== null) next.move_sample_ms = m;
      const w = clampInt(patch.wheel_burst_gap_ms, 50, 5000);
      if (w !== null) next.wheel_burst_gap_ms = w;
      const si = clampInt(patch.state_interval_ms, 0, 5000);
      if (si !== null) next.state_interval_ms = si;
      const sd = clampInt(patch.semantic_debounce_ms, 0, 1000);
      if (sd !== null) next.semantic_debounce_ms = sd;
      const me = clampInt(patch.max_events, 1000, 1000000);
      if (me !== null) next.max_events = me;
      if ('semantic_watch' in patch) {
        next.semantic_watch = !!patch.semantic_watch;
      }
      if ('semantic_keyframe' in patch) {
        next.semantic_keyframe = !!patch.semantic_keyframe;
      }
      const dm = clampInt(patch.dwell_ms, 0, 600000);
      if (dm !== null) next.dwell_ms = dm;
      const dfm = clampInt(patch.draft_ms, 0, 600000);
      if (dfm !== null) next.draft_ms = dfm;
      if ('keyframe_on_checkpoint' in patch) {
        next.keyframe_on_checkpoint = !!patch.keyframe_on_checkpoint;
      }
      if ('full_state_on_change' in patch) {
        next.full_state_on_change = !!patch.full_state_on_change;
      }
      if ('ui_state' in patch) {
        next.ui_state = !!patch.ui_state;
      }
      if ('private_journal' in patch) {
        next.private_journal = !!patch.private_journal;
      }
      if ('session_name' in patch) {
        next.session_name = patch.session_name
          ? String(patch.session_name) : null;
      }
      const kpm = clampInt(patch.keyframe_paint_ms, 0, 10000);
      if (kpm !== null) next.keyframe_paint_ms = kpm;
      if (patch.keyframe_paint_colored !== undefined) {
        const c = Number(patch.keyframe_paint_colored);
        if (isFinite(c)) next.keyframe_paint_colored = Math.max(0, Math.min(1, c));
      }
      if (patch.keyframe_paint_stable_eps !== undefined) {
        const e = Number(patch.keyframe_paint_stable_eps);
        if (isFinite(e)) next.keyframe_paint_stable_eps = Math.max(0, Math.min(0.5, e));
      }
    }
    R.config = next;
    // 运行中生效：上限、轮询间隔、订阅开关
    R.MAX_EVENTS = next.max_events;
    if (before.state_interval_ms !== next.state_interval_ms) resetSampleTimer();
    if (before.semantic_watch !== next.semantic_watch) {
      if (next.semantic_watch) attachStateWatch(); else detachStateWatch();
    }
    const changed = before.move_sample_ms !== next.move_sample_ms
      || before.wheel_burst_gap_ms !== next.wheel_burst_gap_ms
      || before.state_interval_ms !== next.state_interval_ms
      || before.semantic_watch !== next.semantic_watch
      || before.semantic_debounce_ms !== next.semantic_debounce_ms
      || before.semantic_keyframe !== next.semantic_keyframe
      || before.dwell_ms !== next.dwell_ms
      || before.draft_ms !== next.draft_ms
      || before.max_events !== next.max_events
      || before.keyframe_on_checkpoint !== next.keyframe_on_checkpoint
      || before.full_state_on_change !== next.full_state_on_change
      || before.keyframe_paint_ms !== next.keyframe_paint_ms
      || before.keyframe_paint_colored !== next.keyframe_paint_colored;
    if (changed && R.recording) {
      emit('collector_status', {
        config_change: true,
        from: before, to: Object.assign({}, next),
        state_id: R.stateId,
        gap_reason: null,
      });
    }
    updateUI();
    return Object.assign({}, R.config);
  };
  R.status = function () {
    return {
      recording: R.recording, viewer: !!R.viewer,
      events: R.events.length, session: R.sessionId,
      state_id: R.stateId, collector_version: COLLECTOR_VERSION,
      gaps: R.gaps.length,
      shots: (R.shots || []).length,
      shots_by_moment: (function () {
        const m = {};
        (R.shots || []).forEach(function (x) { m[x.moment] = (m[x.moment] || 0) + 1; });
        return m;
      })(),
      dropped: R.counters.dropped || 0,
      checkpoint_dropped: R.counters.checkpoint_dropped || 0,
      overflow: R.overflow ? JSON.parse(JSON.stringify(R.overflow)) : null,
      providers: R.listProviders(),
    };
  };
})();
