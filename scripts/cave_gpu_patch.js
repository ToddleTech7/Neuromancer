/* CAVE 采集器 · 启动期 GPU 补丁（必须在页面/策略脚本之前执行）
 *
 * 解决唯一一件"插件形态做不到"的 R2 前置条件：
 * Neuroglancer 创建 WebGL2 上下文时用的是默认属性，其中
 * `preserveDrawingBuffer: false`。浏览器在每次合成后会清空 drawing buffer，
 * 所以事后 `canvas.toDataURL()` 拿到的是**全透明**（实测 1400×900 的
 * dataURL 有 37 KB，但 64×64 缩略图像素全是 alpha=0）。
 * 结果就是"关键帧看起来截成功了，其实一张白图"。
 *
 * 这个补丁在 getContext 时把 preserveDrawingBuffer 强制成 true，
 * 之后 toDataURL 随时可用，R2（按时间点还原当时画面）才成立。
 *
 * 代价：GPU 不再丢弃帧缓冲，显存/带宽略升。只影响本页，不影响别处。
 * 用法：必须 document-start 注入（Tampermonkey 的 @run-at document-start，
 * 或 Playwright 的 add_init_script）。注入晚了（ng 已经建好上下文）改不动，
 * 此时 recorder 会在 capabilities 里报 needs_startup_patch，不假装能截。
 */
(function () {
  if (window.__caveGLPatched) return;
  window.__caveGLPatched = true;
  const orig = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, attrs) {
    if (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl') {
      const a = Object.assign({}, attrs || {});
      a.preserveDrawingBuffer = true;   // 关键：保住帧缓冲，才能事后读像素
      try { return orig.call(this, type, a); } catch (e) { /* 落到下面 */ }
    }
    return orig.call(this, type, attrs);
  };
})();
