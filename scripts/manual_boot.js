/* CAVE 采集器 · 手动会话面板（由 build_manual_userscript.py 打进油猴脚本）
 *
 * 前提：`window.__caveRecorder` 已存在（recorder 本体在同目录的
 * cave_recorder.user.js，构建时内联）。
 *
 * 只做三件事：
 *   1. 配置：全量 move（回放要完整输入流）+ 每个检查点拍关键帧；
 *   2. 显示：事件数 / 检查点数 / 缺口数（`R.status()` 的真实读数）；
 *   3. 保存：`R.exportWithImagesJSON()` → POST 到 `window.__caveSaveUrl`，
 *      落盘由 cave_manual_server.py 的 `/save` 端点负责。
 *
 * 为什么不用相对 URL：油猴跑在 neuroglancer 自己的源上（可能是 CAVE 内网地址），
 * 相对路径会打到那个源，而不是我们的保存端点。所以保存地址必须是绝对 URL，
 * 且由构建脚本注入。
 */
(function () {
  if (window.__caveManual) return;
  window.__caveManual = true;

  const SAVE_URL = window.__caveSaveUrl || null;

  const bar = document.createElement('div');
  // 标记是采集器自己的 UI：recorder 会跳过落在它里面的事件，
  // 否则点「打检查点」会被当成一次画布点击录进去、回放时去点画布。
  bar.setAttribute('data-cave-ui', '1');
  bar.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;'
    + 'background:rgba(17,20,26,.93);color:#e8eaed;'
    + 'font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;'
    + 'padding:10px 12px;border-radius:10px;box-shadow:0 6px 22px rgba(0,0,0,.5);'
    + 'min-width:250px;'
    // 面板要能选文字复制：宿主页面常带 user-select:none，这里强制覆盖
    + 'user-select:text!important;-webkit-user-select:text!important;'
  bar.innerHTML = '<div style="font-weight:700;margin-bottom:4px">CAVE 采集</div>'
    + '<div id="cs" style="white-space:pre-wrap">等待页面…</div>'
    + '<div style="margin-top:8px;display:flex;gap:6px">'
    + '<button id="ck">打检查点</button>'
    + '<button id="nt">备注</button>'
    + '<button id="sv">结束并保存</button></div>'
    + '<div id="cm" style="margin-top:6px;color:#9aa0a6;max-width:330px;'
    + 'white-space:pre-wrap;user-select:text!important;'
    + '-webkit-user-select:text!important;cursor:text"></div>'
    // 设置：采集粒度/订阅/上限。改完写 localStorage，下次打开自动生效。
    + '<div style="margin-top:8px;display:flex;gap:6px">'
    + '<button id="cfg">设置</button>'
    + '<button id="diag">诊断</button>'
    + '<button id="copy">复制</button></div>'
    + '<div id="cfgbox" style="display:none;margin-top:6px;color:#c8ccd2">'
    + '移动采样 <select id="c_move">'
    + '<option value="0">全量</option><option value="50">50ms</option>'
    + '<option value="100">100ms</option><option value="200">200ms</option>'
    + '<option value="600">600ms</option></select><br>'
    + '状态轮询 <select id="c_state">'
    + '<option value="0">只靠订阅</option><option value="200">200ms</option>'
    + '<option value="600">600ms</option><option value="1000">1000ms</option>'
    + '<option value="2000">2000ms</option></select><br>'
    + '<label><input type="checkbox" id="c_watch"> 选中集/图层变化即时记（订阅）</label><br>'
    + '<label><input type="checkbox" id="c_kf"> 每个检查点拍照</label><br>'
    + '<label><input type="checkbox" id="c_skf"> 语义关键帧（缩放/图层/选中/停留）</label><br>'
    + '停留 <select id="c_dwell">'
    + '<option value="0">关</option><option value="3000">3s</option>'
    + '<option value="5000">5s</option><option value="10000">10s</option></select><br>'
    + '事件上限 <select id="c_max">'
    + '<option value="20000">2 万</option><option value="50000">5 万</option>'
    + '<option value="200000">20 万</option>'
    + '<option value="500000">50 万</option></select><br>'
    + '<button id="c_save" style="margin-top:4px">保存并应用</button>'
    + '</div>'
    // 备注用页面内输入框，不用 window.prompt：内嵌/自动化浏览器会把 prompt
    // 默认 dismiss，人看到的就是“点了一下又没了”，文本永远存不进去。
    + '<div id="nw" style="display:none;margin-top:6px">'
    + '<input id="ni" placeholder="一句话理由，回车记下" '
    + 'style="box-sizing:border-box;width:100%;font:12px ui-monospace,monospace;'
    + 'background:#0f1216;color:#e8eaed;border:1px solid #3a4048;border-radius:6px;'
    + 'padding:4px 6px">'
    + '<div style="margin-top:4px;display:flex;gap:6px">'
    + '<button id="nok">记下</button><button id="nno">取消</button></div></div>';
  document.body.appendChild(bar);
  const cs = bar.querySelector('#cs');
  const cfgBox = bar.querySelector('#cfgbox');
  const cm = bar.querySelector('#cm');

  // 面板内部的事件不要冒泡出去：视口那边会在 document 上拦 pointerdown/mousedown，
  // 不挡住的话在面板里拖选文字会变成拖画布，选不上也就复制不了。
  // 注意用冒泡阶段（第三个参数 false）：capture 阶段停会连自己按钮都收不到事件
  ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click',
   'dblclick', 'selectstart'].forEach(function (ev) {
    bar.addEventListener(ev, function (e) { e.stopPropagation(); }, false);
  });

  function copyText(t) {
    if (!t) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () {
        alert('已复制到剪贴板，直接粘贴即可');
      }, function () { legacyCopy(t); });
      return;
    }
    legacyCopy(t);
  }
  function legacyCopy(t) {
    // http:// 内网地址不是 secure context，clipboard API 不可用，退回 textarea
    try {
      const ta = document.createElement('textarea');
      ta.value = t;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      alert('已复制到剪贴板，直接粘贴即可');
    } catch (e) {
      alert('复制失败：请用鼠标选中面板里的文字再按 Cmd+C');
    }
  }
  bar.querySelector('#copy').addEventListener('click', function () {
    copyText(cm.textContent || '');
  });

  bar.querySelector('#diag').addEventListener('click', function () {
    const R = window.__caveRecorder;
    if (!R || !R.diag) { cm.textContent = 'recorder 还没加载好'; return; }
    let d = null;
    try { d = R.diag(); } catch (e) { cm.textContent = '诊断失败: ' + e; return; }
    try { console.log('[diag]', d); } catch (e) {}
    const trig = Object.keys(d.semantic_triggered || {})
      .map(k => k + '=' + d.semantic_triggered[k]).join(' ') || '（还没触发过）';
    const shots = Object.keys(d.shots_by_moment || {})
      .map(k => k + '=' + d.shots_by_moment[k]).join(' ') || '（0 张）';
    cm.textContent = [
      '缩放来源: ' + (d.scale_source || 'null'),
      'cross / proj: ' + d.crossSectionScale + ' / ' + d.projectionScale,
      '语义关键帧: ' + (d.semantic_keyframe ? '开' : '关')
        + '（检查点拍照 ' + (d.keyframe_on_checkpoint ? '开' : '关') + '）',
      '停留阈值: ' + d.dwell_ms + ' ms',
      '最近缩放: ' + ((d.scale_history || []).slice(-3)
        .map(x => x.t + 's:' + x.cross + '/' + x.proj).join('  ') || '（无）'),
      '最后一次判断: ' + (d.last_eval
        ? ('缩放 ' + d.last_eval.cross + '→' + d.last_eval.prev_cross
           + '（' + d.last_eval.ratio_cross + 'x）'
           + ' 移动 ' + (d.last_eval.moved_nm == null ? '-' : d.last_eval.moved_nm)
           + '/' + (d.last_eval.view_nm == null ? '-' : d.last_eval.view_nm) + 'nm'
           + ' 转向 ' + (d.last_eval.turn_deg == null ? '-' : d.last_eval.turn_deg) + '°'
           + ' 开关=' + (d.last_eval.semantic_on ? '开' : '关'))
        : '（还没采样过）'),
      '触发次数: ' + trig,
      '已出图: ' + shots + '（共 ' + d.shots_total + '）',
      '事件数: ' + d.events,
      '完整 JSON 已打到控制台',
    ].join('\n');
  });
  const nw = bar.querySelector('#nw');
  const ni = bar.querySelector('#ni');
  let nck = 0, started = false, saved = false, saving = false;
  const svBtn = bar.querySelector('#sv');

  function setSaving(on, text) {
    saving = !!on;
    if (text != null) cm.textContent = text;
    svBtn.disabled = saving || saved;
  }

  function ready() {
    const R = window.__caveRecorder;
    return (R && R.loaded) ? R : null;
  }

  function tick() {
    const R = ready();
    if (!R) { cs.textContent = '等待 recorder…'; return setTimeout(tick, 400); }
    if (!started) {
      // 动作回放要**完整**输入流：50ms 采样会把拖动抽稀，重放必然漂。
      // 每个检查点顺手拍一张关键帧（带 state 绑定），回放时才有像素可比。
      // 但如果用户在设置面板里存过值，就以存的为准（不要每次启动又盖回去）。
      try {
        const saved = JSON.parse(localStorage.getItem('cave_settings_v1') || '{}');
        R.setConfig(saved.move_sample_ms !== undefined
          ? {move_sample_ms: saved.move_sample_ms,
             keyframe_on_checkpoint: saved.keyframe_on_checkpoint !== undefined
               ? saved.keyframe_on_checkpoint : true,
             keyframe_paint_ms: 3000}
          : {move_sample_ms: 0, keyframe_on_checkpoint: true,
             keyframe_paint_ms: 3000});
      } catch (e) {}
      R.start();
      try { configUI(R); } catch (e) {}
      started = true;
      // recorder 自带的右下角面板与本面板功能重叠（开始/停止/截图/导出），
      // 两套按钮同时存在容易误点：这里收拢成左下角一套。
      // 它的「停止」会把会话截断、「开始」会清空已记事件，最危险。
      const rp = document.getElementById('cave-recorder-panel');
      if (rp) rp.style.display = 'none';
      setTimeout(function () {
        try { R.checkpointNow('session_start'); nck++; } catch (e) {}
      }, 1500);
    }
    let st = {};
    try { st = R.status() || {}; } catch (e) {}
    if (svBtn) svBtn.disabled = saved || saving;
    const ndrop = (st.dropped || 0) + (st.checkpoint_dropped || 0);
    const bm = st.shots_by_moment || {};
    const ckTotal = bm.checkpoint || 0;
    const ckAuto = Math.max(0, ckTotal - nck);   // 开场基线 + 自动检查点
    cs.textContent = (saved ? '已保存' : (st.recording ? '录制中' : '已停止'))
      + ' ｜ 事件 ' + (st.events == null ? '?' : st.events)
      + ' ｜ 缺口 ' + (st.gaps == null ? '?' : st.gaps)
      + (ndrop ? ' ｜ 丢弃 ' + ndrop : '')
      + '\n图 ' + (st.shots == null ? '?' : st.shots) + ' 张：'
      + '检查点 ' + nck + (ckAuto ? '（自动 ' + ckAuto + '）' : '')
      + ' · 缩放 ' + (bm.scale_change || 0)
      + ' · 视角 ' + (bm.view_change || 0)
      + ' · 选中 ' + (bm.selection_change || 0)
      + ' · 图层 ' + (bm.layer_change || 0)
      + ' · 停留 ' + (bm.dwell || 0);
    if (!saved) setTimeout(tick, 600);
  }

  function configUI(R) {
    const q = id => bar.querySelector(id);
    q('#c_move').value = String(R.config.move_sample_ms);
    q('#c_state').value = String(R.config.state_interval_ms);
    q('#c_max').value = String(R.config.max_events);
    q('#c_watch').checked = !!R.config.semantic_watch;
    q('#c_kf').checked = !!R.config.keyframe_on_checkpoint;
    q('#c_skf').checked = !!R.config.semantic_keyframe;
    q('#c_dwell').value = String(R.config.dwell_ms);
    q('#cfg').onclick = () => {
      cfgBox.style.display = cfgBox.style.display === 'none' ? 'block' : 'none';
    };
    q('#c_save').onclick = () => {
      const patch = {
        move_sample_ms: Number(q('#c_move').value),
        state_interval_ms: Number(q('#c_state').value),
        max_events: Number(q('#c_max').value),
        semantic_watch: q('#c_watch').checked,
        keyframe_on_checkpoint: q('#c_kf').checked,
        semantic_keyframe: q('#c_skf').checked,
        dwell_ms: Number(q('#c_dwell').value),
      };
      if (!patch.semantic_watch && patch.state_interval_ms === 0) {
        cm.textContent = '订阅和轮询都关了，就再也记不到状态了；至少留一个。';
        return;
      }
      try {
        localStorage.setItem('cave_settings_v1', JSON.stringify(patch));
      } catch (e) { /* 隐私模式可能禁止写入，忽略 */ }
      R.setConfig(patch);
      cm.textContent = '设置已保存并生效（下次打开自动应用）';
    };
  }

  function noteShow(on) {
    nw.style.display = on ? 'block' : 'none';
    if (on) { ni.value = ''; try { ni.focus(); } catch (e) {} }
  }

  function noteCommit() {
    const R = ready(); if (!R) return;
    const t = (ni.value || '').trim();
    if (!t) { cm.textContent = '备注为空，没记'; return; }
    try {
      R.addNote(t);
      cm.textContent = '备注已记（绑定当前视角）：' + t.slice(0, 60);
      noteShow(false);
    } catch (e) { cm.textContent = '备注失败: ' + e; }
  }

  bar.querySelector('#nt').onclick = function () {
    const R = ready(); if (!R) return;
    noteShow(nw.style.display === 'none');
  };
  bar.querySelector('#nok').onclick = noteCommit;
  bar.querySelector('#nno').onclick = function () { noteShow(false); };
  ni.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); noteCommit(); }
    if (ev.key === 'Escape') { noteShow(false); }
  });

  bar.querySelector('#ck').onclick = function () {
    const R = ready(); if (!R) return;
    try {
      R.checkpointNow('manual_' + (nck + 1)); nck++;
      cm.textContent = '已打检查点 manual_' + nck + '（含关键帧）';
    } catch (e) { cm.textContent = '打点失败: ' + e; }
  };

  function waitHostSave() {
    let n = 0;
    const t = setInterval(function () {
      n += 1;
      if (window.__caveSavedDir) {
        clearInterval(t);
        saved = true;
        setSaving(false, '已保存（宿主写入）\n' + window.__caveSavedDir);
        cs.textContent = '已保存（可关页面）';
        return;
      }
      if (n > 240) {           // 2 分钟
        clearInterval(t);
        setSaving(false, '宿主也没能保存，看终端的 [host] 行；可再点一次');
      }
    }, 500);
  }

  function post(body, cb) {
    // 油猴里优先用 GM_xmlhttpRequest（不受页面 CORS 限制）；
    // 浏览器/Playwright 里退回 fetch（保存端点带 CORS 头）。
    if (typeof GM_xmlhttpRequest === 'function') {
      GM_xmlhttpRequest({
        method: 'POST', url: SAVE_URL,
        headers: {'Content-Type': 'application/json'},
        data: body,
        onload: function (r) { cb(null, r.responseText); },
        onerror: function (e) { cb(String(e)); }
      });
      return;
    }
    // 没有超时的话，端点被别的程序接走时（实测 IDE 占 127.0.0.1:8899）
    // fetch 会永远挂着，人只看到“正在保存…”一直不结束。
    const ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    // 20s 对大包不够：几张 1MB 的 PNG base64 内嵌后，上传 + 打包会超。
    const timer = ctl ? setTimeout(function () { ctl.abort(); }, 120000) : null;
    fetch(SAVE_URL, {method: 'POST',
                     headers: {'Content-Type': 'application/json'},
                     signal: ctl ? ctl.signal : undefined,
                     body: body})
      .then(function (r) {
        clearTimeout(timer);
        return r.text().then(function (t) { cb(null, t); });
      })
      .catch(function (e) {
        clearTimeout(timer);
        cb(String(e));
      });
  }

  bar.querySelector('#sv').onclick = function () {
    const R = ready(); if (!R || saved || saving) return;
    if (!SAVE_URL) { setSaving(false, '没有保存端点（未注入 __caveSaveUrl）'); return; }
    setSaving(true, '正在打包…');
    // 先让“正在打包”渲染出来：exportWithImagesJSON 是同步的，图多时会阻塞页面
    setTimeout(function () {
      let pack = null;
      let lastShot = null;
      try {
        R.checkpointNow('session_end');
        nck++;
        // 收尾关键帧是异步的：先留住 promise，再 stop，最后等它落进 R.shots，
        // 否则“结束并保存”这个检查点会没有图（或图来不及进包）。
        lastShot = R.lastCheckpointShot;
        R.stop();
      } catch (e) { setSaving(false, '导出失败: ' + e); return; }
      const finish = function () {
        try { pack = JSON.parse(R.exportWithImagesJSON()); }
        catch (e) { setSaving(false, '导出失败: ' + e); return; }
        setSaving(true, '正在保存…（' + (pack.shots || []).length + ' 张图）');
        post(JSON.stringify(pack), function (err, text) {
          if (err) {
            // 页面存不了（最常见：页面是 https、保存端点是 http://127.0.0.1，
            // 浏览器按混合内容拦掉，只报 TypeError: Failed to fetch）。
            // 数据还在内存里——交给宿主进程用 playwright 直接取。
            window.__caveSaveRequested = {at: Date.now()};
            setSaving(true, '页面直接存不了（' + err + '）\n'
              + '已交给宿主进程，等它写盘…（终端会打 [host] 行）');
            waitHostSave();
            return;
          }
          let d = {};
          try { d = JSON.parse(text); } catch (e) {}
          if (d.error) {
            // 413（包太大）/ 500（落盘失败）也走宿主直写；只显示错误等于
            // 让长会话的数据留在内存里等丢。
            window.__caveSaveRequested = {at: Date.now()};
            setSaving(true, '服务端保存失败（' + d.error + '）\n'
              + '已交给宿主进程直写，等它落盘…（终端会打 [host] 行）');
            waitHostSave();
            return;
          }
          saved = true;
          setSaving(false);
          cs.textContent = '已保存（可关页面）';
          cm.textContent = '已保存\n' + (d.dir || '') + '\n事件 ' + (d.n_events || 0)
            + ' / 图 ' + (d.n_shots || 0);
        });
      };
      if (lastShot && typeof lastShot.then === 'function') {
        cm.textContent = '正在等最后一张关键帧…';
        lastShot.then(finish, finish);
      } else {
        finish();
      }
    }, 40);
  };

  tick();
})();
