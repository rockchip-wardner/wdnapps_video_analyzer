(function () {
  "use strict";
  const API_PREFIX = "/api";

  /* ========================================================================
   * 视频实时深度估计
   *
   * 左栏 = 多路实时视频流（二进制帧通道，与 vtl/vd 同一条路）；
   * 右栏 = 逐路深度热力图（**事件通道** —— 深度图随 depth 事件一起下发）。
   *
   * 为什么深度图走事件通道而不是二进制帧通道
   * ----------------------------------------
   * 二进制帧通道是「视频流」：每路每帧一张显示尺寸 JPEG，由解码线程广播。
   * 深度图不是视频流 —— 它是**推理产物**，频率低一个量级（DA3 单 view 约
   * 116 ms/次，且受轮转节奏限制），而且必须与深度数值（色标窗口、统计量）
   * 严格配对。走事件通道可以顺带把 stats / infer_ms / 通道号一起带上，
   * 前端画右栏时不必再猜「这张热力图是哪一路、什么时候的」。
   *
   * 深度图**不进** WS 的二进制帧（那会让每帧多一张图，32 路时带宽直接爆），
   * 也**不改** multi_api.py（共享代码）—— 卡片自己消费事件即可。
   * ======================================================================== */

const VDP = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  lastSnap: null,
  detectorSnap: null,
  boot: null,
};

const VDPDepth = {
  // 每路最近一次的深度摘要（给右栏瓦片的角标/标题用）。
  // **事件历史不再进页面** —— UI 上的「深度事件历史」列表已移除，
  // 历史改由后端日志承担（开关见 ``WDNWEBUI_DEPTH_DEBUG``）。页面只保留
  // 「当前每路是什么深度」这一层信息。
  latest: new Map(),
};

// 宫格实现（建格 / 分列 / 每路单飞解码上屏）在共享组件里。
const GRID_EL = "vdp-grid";
const DEPTH_GRID_EL = "vdp-depth-grid";
const PICK_ID = "vdp-model-pick";
const CAPABILITY = "swcap_vision_depth";
const chNo = window.MultiGrid.chNo;

function setVdpOverlay(text, isError) {
  const el = document.getElementById("vdp-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function setVdpDepthOverlay(text, isError) {
  const el = document.getElementById("vdp-depth-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function setVdpMeta(session) {
  const el = document.getElementById("vdp-meta");
  VDP.lastSnap = session;
  if (el) {
    const n = ((session && session.channel_list) || []).length;
    const parts = [];
    if (n) parts.push(`${n} 路`);
    if (session && session.state) parts.push(`[${session.state}]`);
    if (session && session.loop) parts.push("循环");
    el.textContent = parts.join(" · ") || "--";
  }
  reportDemoGuard();
}

function setVdpProgress(session) {
  const el = document.getElementById("vdp-progress");
  if (!el) return;
  // 多路下「播放进度」没有单一含义（每路各自 frame_pos），改成
  // 「已经出画面的路数 / 总路数」。
  const list = (session && session.channel_list) || [];
  const live = list.filter((c) => c.has_frame).length;
  const total = list.length;
  el.classList.toggle("live", total > 0 && live === total);
  el.style.width = total ? `${Math.round((live / total) * 100)}%` : "0%";
}

/** 上报给 DemoGuard —— 离开本页时要停后台运行。 */
function reportDemoGuard() {
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-depth", {
      session: VDP.lastSnap,
      detector: VDP.detectorSnap,
    });
  }
}

/** 右栏（深度）宫格：建格 + 角标。
 *
 *  刻意**不调** ``MultiGrid.status()`` —— 它会把瓦片标签写成
 *  「#1 · playing」这类**视频**状态，而右栏要显示的是深度摘要
 *  （深度范围 / 推理耗时）。只借它的 ``ensure()`` 建格，标签自己管。 */
function ensureDepthGrid(n) {
  window.MultiGrid.ensure(DEPTH_GRID_EL, n);
  const tiles = window.MultiGrid.tiles(DEPTH_GRID_EL);
  for (const [i, t] of tiles) {
    const info = VDPDepth.latest.get(i);
    if (!info) {
      t.label.textContent = "#" + chNo(i);
      t.label.title = "";
      continue;
    }
    t.label.textContent = "#" + chNo(i) + " · " + info.range;
    t.label.title = info.title;
  }
}

/** 把一张深度热力图画进右栏对应瓦片。 */
function paintVdpDepth(ch, jpegBytes) {
  if (!jpegBytes) return;
  const tiles = window.MultiGrid.tiles(DEPTH_GRID_EL);
  if (!tiles.has(ch)) return;              // 还没建格（路数还没对上）
  window.MultiGrid.paint(DEPTH_GRID_EL, ch, jpegBytes);
}

function updateVdpTiles(session) {
  if (!session) return;
  const list = session.channel_list || [];
  const vlm = session.vlm || {};
  const { total, ok, eof } = window.MultiGrid.status(
    GRID_EL, list, window.MultiCard.activeChannels(vlm));
  ensureDepthGrid(list.length);
  const hint = document.getElementById("vdp-channel-hint");
  if (hint) {
    if (!total) hint.textContent = "尚未播放视频";
    else if (ok === total) hint.textContent = `${total} 路视频 · 全部正常`;
    else hint.textContent = `${total} 路视频 · ${ok} 路正常`;
  }
  window.ZoomFrame?.tick(GRID_EL, session, total);

  // 左栏提示层。文件源播完（loop 未勾）会停在 eof 不再产帧，画面定格在
  // 最后一帧 —— 不说的话就是「画面卡住 + 提示条报 0 路正常」。
  if (total && eof === total) {
    setVdpOverlay("视频已播完\n\n勾选「循环播放」可自动重播，或点 播放 继续", false);
  } else if (eof) {
    setVdpOverlay(`${eof} 路视频已播完`, false);
  } else if (total && ok === total) {
    setVdpOverlay("", false);
  } else if (total) {
    setVdpOverlay("部分视频尚未出画面…", false);
  } else {
    setVdpOverlay("尚未播放视频\n\n在下方填入视频地址后点击「播放视频」", false);
  }

  // 右栏提示层：深度图只在推理出结果后才有。
  const depthMeta = document.getElementById("vdp-depth-meta");
  if (depthMeta) {
    const parts = [];
    if (total) parts.push(`${total} 路`);
    const infer = VDP.detectorSnap && VDP.detectorSnap.last_infer_ms;
    if (infer) parts.push(`最近 ${Number(infer).toFixed(0)} ms`);
    depthMeta.textContent = parts.join(" · ") || "--";
  }
  if (!total) {
    setVdpDepthOverlay("等待视频源", false);
  } else if (!VDPDepth.latest.size) {
    setVdpDepthOverlay("等待深度结果…\n\n启动深度估计后这里显示每一路的深度热力图", false);
  } else {
    setVdpDepthOverlay("", false);
  }
}

// 宫格放大的接线。attach 必须在 IIFE 内：onError 要用私有的 setVdpOverlay。
window.ZoomFrame?.attach({
  gridEl: GRID_EL,
  apiBase: API_PREFIX + "/video-depth",
  onError: (msg) => setVdpOverlay(msg, true),
});

function connectVdpWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-depth/ws`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  VDP.ws = ws;

  ws.onopen = () => {
    VDP.wsRetry = 0;
  };
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_) { return; }
      if (snap.type === "event_history") {
        // 连接建立时服务端一次性打包发来的历史事件。**只用来补画右栏**
        // （每路最后一次的深度图），不再建事件列表 —— 列表已移除。
        // 服务端按 id 升序（旧→新）发。
        applyDepthToGrid(Array.isArray(snap.events) ? snap.events : []);
        return;
      } else if (snap.type === "event") {
        applyDepthEvent(snap);
        return;
      } else if (snap.type === "status") {
        setVdpMeta(snap.session);
        setVdpProgress(snap.session);
        updateVdpTiles(snap.session);
        refreshVdpButtons(snap.session && snap.session.state);
        if (snap.detector) {
          VDP.detectorSnap = snap.detector;
          updateVdpDetectorStatus(snap.detector);
        }
        window.FrameJudge?.update("vdp", snap.session && snap.session.judge);
        const payload = document.getElementById("data-video-depth");
        if (payload) payload.textContent = JSON.stringify(snap, null, 2);
        return;
      }
      setVdpOverlay("", false);
      return;
    }
    try {
      // 通道号在二进制头第 1 字节（见 multi_api 的 stream_ws）。ArrayBuffer
      // 没有 getUint8（那是 DataView 的），要开 1 字节视图才零拷贝。
      const buf = ev.data;
      const idx = buf.byteLength ? new Uint8Array(buf, 0, 1)[0] : 0;
      window.MultiGrid.paint(GRID_EL, idx, new Uint8Array(buf, 1));
    } catch (e) {
      setVdpOverlay("帧渲染失败: " + e, true);
    }
  };
  ws.onclose = () => {
    VDP.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VDP.wsRetry++));
    VDP.wsTimer = setTimeout(connectVdpWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

/* ------------------------------------------------------------------ *
 * 深度事件 → 右栏
 * ------------------------------------------------------------------ */

/** base64 → Uint8Array（交给 MultiGrid.paint 走 createImageBitmap）。 */
function b64ToBytes(b64) {
  const bin = atob(b64);
  const len = bin.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

/** 把深度事件里的热力图画到右栏，并更新该路的摘要。 */
function applyDepthEvent(ev) {
  if (!ev) return;
  const ch = typeof ev.channel === "number" ? ev.channel : 0;
  const st = ev.stats || {};
  const range = (st.depth_min != null && st.depth_max != null)
    ? `${(+st.depth_min).toFixed(2)}~${(+st.depth_max).toFixed(2)}` : "--";
  VDPDepth.latest.set(ch, {
    range,
    title: `深度 ${range} · 推理 ${ev.infer_ms ?? "--"} ms`,
  });
  if (ev.depth_b64) {
    try {
      paintVdpDepth(ch, b64ToBytes(ev.depth_b64));
    } catch (e) {
      console.warn("[vdp] 深度图解码失败", e);
    }
  }
  const tiles = window.MultiGrid.tiles(DEPTH_GRID_EL);
  const t = tiles.get(ch);
  if (t) {
    t.label.textContent = "#" + chNo(ch) + " · " + range;
    t.label.title = `深度 ${range} · 推理 ${ev.infer_ms ?? "--"} ms`;
  }
}

/** 历史事件回放：只画**每路最后一次**（右栏显示当前深度，不是历史）。 */
function applyDepthToGrid(events) {
  const last = new Map();
  for (const ev of events || []) {
    const ch = typeof ev.channel === "number" ? ev.channel : 0;
    last.set(ch, ev);
  }
  for (const [ch, ev] of last) applyDepthEvent(ev);
}

/* ------------------------------------------------------------------ *
 * 后端调用
 * ------------------------------------------------------------------ */

async function vdpOpen(payload) {
  const r = await fetch(API_PREFIX + "/video-depth/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    throw new Error("HTTP " + r.status + " " + detail);
  }
  return r.json();
}

async function vdpAction(action) {
  const r = await fetch(API_PREFIX + "/video-depth/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function vdpStatus() {
  const r = await fetch(API_PREFIX + "/video-depth", { cache: "no-store" });
  if (!r.ok) return null;
  const snap = await r.json();
  const session = snap.session || {};
  setVdpMeta(session);
  setVdpProgress(session);
  updateVdpTiles(session);
  refreshVdpButtons(session.state);
  if (snap.detector) {
    VDP.detectorSnap = snap.detector;
    updateVdpDetectorStatus(snap.detector);
  }
  const loopEl = document.getElementById("vdp-loop");
  if (loopEl && session.loop != null) loopEl.checked = !!session.loop;
  const payload = document.getElementById("data-video-depth");
  if (payload) payload.textContent = JSON.stringify(snap, null, 2);
  return snap;
}

async function vdpClose() {
  const r = await fetch(API_PREFIX + "/video-depth/close", { method: "POST" });
  return r.ok ? r.json() : null;
}

function refreshVdpButtons(state) {
  const playBtn = document.getElementById("vdp-play");
  const pauseBtn = document.getElementById("vdp-pause");
  const stopBtn = document.getElementById("vdp-stop");
  if (!playBtn || !pauseBtn || !stopBtn) return;
  const active = state === "playing" || state === "paused" || state === "eof";
  stopBtn.disabled = !active;
  if (state === "playing") {
    playBtn.disabled = true;
    pauseBtn.disabled = false;
  } else if (state === "paused") {
    playBtn.disabled = false;
    pauseBtn.disabled = true;
  } else {
    playBtn.disabled = !active;
    pauseBtn.disabled = !active;
  }
}

/* ------------------------------------------------------------------ *
 * detector 状态 / 启停
 * ------------------------------------------------------------------ */

function updateVdpDetectorStatus(snap) {
  const statusEl = document.getElementById("vdp-detector-status");
  const startBtn = document.getElementById("vdp-detector-start");
  const stopBtn  = document.getElementById("vdp-detector-stop");
  if (!snap || !statusEl) return;
  // tick **先**跑：它推进等待状态机，可能在这一步把 pending 清掉。
  // 下面的 booting 必须读 tick **之后**的 pending。
  const boot = window.BootRestore?.tick(VDP.boot, snap);
  const booting = !!(VDP.boot && VDP.boot.pending);
  const capReady = !!snap.session_ready;
  const capLoading = !!snap.model_loading;
  if (!snap.available) {
    statusEl.textContent = "不可用: " + (snap.init_error || "能力未注册");
  } else if (capLoading) {
    statusEl.textContent = booting
      ? "正在等待模型加载…（开机自启，完成后自动开始）"
      : "正在等待模型加载…";
  } else if (snap.enabled && capReady) {
    const slots = snap.slot_count > 1 ? ` · ${snap.slot_count} 张卡并行` : "";
    const ms = snap.last_infer_ms ? ` · ${Number(snap.last_infer_ms).toFixed(0)} ms/次` : "";
    // 深度事件历史**不在页面上**（列表已移除），改由后端日志承担。
    // 这里把开关状态显出来，运维一眼就知道历史到底有没有在打。
    const dbg = snap.debug_log ? " · 事件日志:开" : "";
    statusEl.textContent = `运行中 · 已推理 ${snap.infers ?? 0} 次${ms}${slots}${dbg}`;
  } else if (snap.enabled) {
    statusEl.textContent = `已启动，加载模型… (首次约 30s，队列 ${snap.queue_size})`;
  } else if (capReady) {
    statusEl.textContent = "已加载";
  } else if (snap.init_error && !/功能未启用/.test(snap.init_error)) {
    statusEl.textContent = "模型加载失败: " + snap.init_error;
  } else {
    statusEl.textContent =
      "功能未启用：请先在算力配置启用 [Depth] 视觉深度信息 v3";
    if (snap.init_error) console.warn("[vdp] 能力未就绪:", snap.init_error);
  }
  if (startBtn) {
    startBtn.disabled = !snap.available || !capReady || capLoading
      || !!snap.enabled || booting;
    startBtn.title = capReady
      ? (booting ? "正在等待开机自启恢复…" : "")
      : (capLoading
          ? "模型加载中，请稍候…"
          : "功能未启用：请先在算力配置启用 [Depth] 视觉深度信息 v3");
  }
  if (stopBtn)  stopBtn.disabled  = !snap.enabled;
  VDP.detectorSnap = snap;
  reportDemoGuard();
  if (boot && boot.startNow) vdpBootAutoStart();
}

async function refreshVdpDetector() {
  try {
    // 用 ``GET /prompt``（detector 快照）而不是 ``GET /events``：后者会
    // 把整段事件历史连同热力图 base64 一起吐回来（几百 KB），而页面已经
    // 不再渲染事件列表了 —— 历史要看就开日志开关。
    const r = await fetch(API_PREFIX + "/video-depth/prompt", { cache: "no-store" });
    if (!r.ok) return null;
    const snap = await r.json();
    updateVdpDetectorStatus(snap || {});
    return snap;
  } catch (_) { return null; }
}

/** 开机自启恢复：记住「上次跑的是什么」，重启后照着复原。 */
function vdpCollectLastRun() {
  return {
    sources: readVdpSources(),
    loop: !!document.getElementById("vdp-loop")?.checked,
    // 深度估计没有文本输入。这个键只是为了满足 BootRestore 的
    // 「有输入才起得来」闸门（见 boot-restore.js 的 hasPrompt 判定）——
    // 深度页不需要任何提示词，给一个占位值就能让开机自启自动启动。
    prompt: "depth",
    saved_at: Date.now(),
  };
}

function vdpApplyLastRun(s) {
  if (!s) return;
  if (Array.isArray(s.sources) && s.sources.length) {
    const ta = document.getElementById("vdp-sources");
    if (ta) ta.value = s.sources.join("\n");
  }
  const loopEl = document.getElementById("vdp-loop");
  if (loopEl && s.loop != null) loopEl.checked = !!s.loop;
}

async function vdpBootRestore(saved, h) {
  vdpApplyLastRun(saved);
  window.BootRestore.arm(h);
  const sources = Array.isArray(saved.sources) ? saved.sources.slice() : [];
  if (sources.length) {
    const r = await openVdpFromTextarea();
    if (!r || !r.ok) {
      setVdpOverlay("自动恢复视频源失败", true);
    }
  }
}

/** 模型就绪后的自动启动（由 updateVdpDetectorStatus 的 tick 触发）。 */
async function vdpBootAutoStart() {
  const h = VDP.boot;
  if (!h || !h.saved) return;
  setVdpOverlay("模型已就绪，正在自动启动深度估计…", false);
  // 必须先重拉勾选器：页面初始化时模型还没起，instances-by-capability
  // 返回空 → 勾选器是空的 → selectedModels 返回 null → 启动被静默中止。
  try {
    await window.loadInstancePickMulti?.(PICK_ID, CAPABILITY, "vdp.instances");
  } catch (e) {
    setVdpOverlay("自动启动失败: 重拉模型列表出错 " + (e?.message || e), true);
    return;
  }
  await vdpDetectorStart();
}

async function vdpDetectorStart() {
  // 轮转间隔（秒/路）。DA3 单 view 推理约 116 ms，多路轮转本身就够慢，
  // 这里不再额外停留 —— 留 0.5s 间隔会让 4 路的刷新明显发涩。
  const rr_interval = 0;
  try {
    const models = window.MultiCard.selectedModels(PICK_ID);
    if (!models) {
      setVdpOverlay("请先勾选至少一个模型实例", true);
      return;
    }
    const r = await fetch(API_PREFIX + "/video-depth/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rr_interval, models }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      setVdpOverlay("启动深度估计失败: " + detail, true);
      return;
    }
    const snap = await r.json();
    updateVdpDetectorStatus(snap);
    // 只在成功启动后记账 —— 失败（没勾模型 / 503）不写。
    window.BootRestore?.save("video-depth", vdpCollectLastRun());
    setVdpOverlay("", false);
  } catch (e) {
    setVdpOverlay("启动深度估计失败: " + e.message, true);
  }
}

async function vdpDetectorStop() {
  try {
    const r = await fetch(API_PREFIX + "/video-depth/prompt/stop", { method: "POST" });
    if (r.ok) updateVdpDetectorStatus(await r.json());
  } catch (e) {
    setVdpOverlay("停止深度估计失败: " + e.message, true);
  }
}

function readVdpSources() {
  const ta = document.getElementById("vdp-sources");
  if (!ta) return [];
  const out = [];
  for (const raw of (ta.value || "").split(/\r?\n/)) {
    const s = raw.trim();
    if (!s) continue;
    out.push(s);
  }
  return out;
}

/** 打开视频的唯一入口 —— 「播放视频」按钮与向导的 applyWizardSnapshot 共用。
 *
 *  必须先按行拆成列表再走 `sources`（复数）：`source`（单数）是老前端的
 *  兼容写法，等价于 `sources: [source]`，于是多行文本会被当成**一路带
 *  换行的假路径**，解码必然失败。 */
async function openVdpFromTextarea() {
  const srcs = readVdpSources();
  if (!srcs.length) {
    setVdpOverlay("请在文本框里输入至少一路视频源（每行一路）", true);
    return { ok: false, reason: "empty" };
  }
  if (srcs.length > 32) {
    setVdpOverlay(`最多 32 路，当前 ${srcs.length} 路`, true);
    return { ok: false, reason: "too_many" };
  }
  const loopEl = document.getElementById("vdp-loop");
  const openBtn = document.getElementById("vdp-open");
  if (openBtn) openBtn.disabled = true;
  window.MultiGrid.ensure(GRID_EL, srcs.length);
  ensureDepthGrid(srcs.length);
  setVdpOverlay(`正在打开 ${srcs.length} 路 …`, false);
  try {
    const snap = await vdpOpen({
      sources: srcs,
      loop: !!(loopEl && loopEl.checked),
    });
    const session = snap.session || snap;
    setVdpMeta(session);
    setVdpProgress(session);
    updateVdpTiles(session);
    if (loopEl && session.loop != null) loopEl.checked = !!session.loop;
    return { ok: true, snap };
  } catch (e) {
    setVdpOverlay("打开失败: " + e.message, true);
    return { ok: false, reason: String((e && e.message) || e) };
  } finally {
    if (openBtn) openBtn.disabled = false;
  }
}

/* ------------------------------------------------------------------ *
 * 初始化
 * ------------------------------------------------------------------ */

function initVideoDepth() {
  // 判据浮层（角标 + 曲线）锚在**画面容器**上。多路宫格里没有单一
  // canvas，锚到 grid 元素才是对的矩形。
  window.FrameJudge?.mount({ prefix: "vdp", canvasId: GRID_EL,
                             apiBase: API_PREFIX + "/video-depth/judge" });
  if (window.__vdpInit) return;
  window.__vdpInit = true;

  loadInstancePickMulti(PICK_ID, CAPABILITY, "vdp.instances");
  VDP.boot = window.BootRestore?.begin("video-depth", {
    startBtn: "vdp-detector-start",
    restore: (saved, handle) => vdpBootRestore(saved, handle),
  });

  const sourceEl = document.getElementById("vdp-sources");
  const loopEl   = document.getElementById("vdp-loop");
  const openBtn = document.getElementById("vdp-open");
  const closeBtn = document.getElementById("vdp-close");
  const browseBtn = document.getElementById("vdp-browse");
  const playBtn = document.getElementById("vdp-play");
  const pauseBtn = document.getElementById("vdp-pause");
  const stopBtn = document.getElementById("vdp-stop");

  if (openBtn) {
    openBtn.addEventListener("click", openVdpFromTextarea);
  }

  // 打开共享文件选择器（选到的是**服务器所在设备**上的路径）。
  // 目标是 textarea 时，fs-modal 是**追加一行**而不是整体替换 —— 正是
  // 多路要的语义。
  if (browseBtn) {
    browseBtn.addEventListener("click", () => {
      window.VdFsModal._sourceTarget = sourceEl;
      window.VdFsModal.open("");
    });
  }

  if (closeBtn) {
    closeBtn.addEventListener("click", async () => {
      try { await vdpClose(); } catch (_) {}
      await vdpStatus();
      setVdpOverlay("已关闭", false);
    });
  }

  if (playBtn) {
    playBtn.addEventListener("click", async () => {
      try {
        const r = await vdpAction("play");
        await vdpStatus();
        if (r && r.ok === false) setVdpOverlay("播放失败: " + (r.reason || r.state), true);
        else setVdpOverlay("", false);
      } catch (e) { setVdpOverlay("播放失败: " + e.message, true); }
    });
  }
  if (pauseBtn) {
    pauseBtn.addEventListener("click", async () => {
      try {
        const r = await vdpAction("pause");
        await vdpStatus();
        if (r && r.ok === false) setVdpOverlay("暂停失败: " + (r.reason || r.state), true);
        else setVdpOverlay("", false);
      } catch (e) { setVdpOverlay("暂停失败: " + e.message, true); }
    });
  }
  if (stopBtn) {
    stopBtn.addEventListener("click", async () => {
      try {
        const r = await vdpAction("stop");
        await vdpStatus();
        if (r && r.ok === false) setVdpOverlay("停止失败: " + (r.reason || r.state), true);
        else setVdpOverlay("已停止", false);
      } catch (e) { setVdpOverlay("停止失败: " + e.message, true); }
    });
  }

  const detStartBtn = document.getElementById("vdp-detector-start");
  const detStopBtn  = document.getElementById("vdp-detector-stop");
  if (detStartBtn) detStartBtn.addEventListener("click", vdpDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vdpDetectorStop);

  refreshVdpDetector();
  if (!VDP.ws && !VDP.wsTimer) connectVdpWs();
}

initVideoDepth();

/* ------------------------------------------------------------------ *
 * 应用向导模式 API — wizard 框架 finish 时调用。
 * 契约：opts 由 wizard.json 的 steps[].fields[].ui_key 决定
 *   vdp-sources  → opts.source
 *   vdp-loop     → opts.loop
 * source 非空 → 写回 #vdp-sources 后调 openVdpFromTextarea；空字段不触发
 * 对应动作。全局名约定 ImApp_<app.id 去掉 hyphen>。
 * ------------------------------------------------------------------ */

/** 向导启动前的「能力就绪」探测 —— **直接问后端快照**，不等 1Hz WS 那一拍。
 *
 *  为什么不能靠 WS：WS 快照比 registry 慢最多 1s，模型刚起来的那一秒
 *  快照还是「未就绪」，启动按钮因此是灰的，而 **disabled 按钮的
 *  click() 被浏览器静默丢弃**（不报错、不打日志）。 */
async function vdpCapabilitySnapshot() {
  const data = await refreshVdpDetector();
  return (data && data.snapshot) || VDP.detectorSnap || null;
}

/** 调宿主共享的启动器（core/wizard-start.js）。宿主版本太老、没有这个
 *  模块时给一句人话错误，而不是 "Cannot read properties of undefined"。 */
function _wizardStart(opts) {
  const ws = window.WizardStart;
  if (!ws || typeof ws.run !== "function") {
    throw new Error("WizardSnapshotError:helper_missing:宿主缺少 /static/core/wizard-start.js,请更新 wdnwebui 后重试");
  }
  return ws.run(opts);
}

window.ImApp_video_depth = window.ImApp_video_depth || {};
window.ImApp_video_depth.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el != null) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el != null) el.checked = !!v; };

  if (opts.source)      setVal("vdp-sources", opts.source);
  if (opts.loop != null) setChk("vdp-loop", opts.loop);
  // 帧判据 —— 必须在 open 之前落地：open 一开视频，判据已经在解码热路径上跑。
  const judge = await window.FrameJudge?.fromWizard("vdp", opts) ?? null;

  const result = { source_opened: false, detector_started: false,
                   judge_applied: !!judge };

  if (opts.source) {
    try {
      // 必须走 openVdpFromTextarea：opts.source 是整个多行文本，单数 key
      // 会被后端当成一路假路径。
      const r = await openVdpFromTextarea();
      if (!r.ok) throw new Error(r.reason || "open failed");
      result.source_opened = true;
    } catch (e) {
      throw new Error("WizardSnapshotError:open_source_failed:" + (e?.message || e));
    }
  }

  // 深度估计没有文本输入，向导**总是**启动检测（不像 vtl 要先有查询词）。
  await _wizardStart({
    label: "启动深度估计",
    probe: vdpCapabilitySnapshot,
    start: async () => {
      // 必须在**就绪之后**重拉勾选器：等就绪前模型还没起，列表是空的，
      // 直接点会因「一个都没勾」静默中止。
      await window.loadInstancePickMulti?.(PICK_ID, CAPABILITY, "vdp.instances");
      await vdpDetectorStart();
    },
  });
  result.detector_started = true;

  return result;
};

/* v0.23 多路判据 UI 接线 —— 与 vtl/vd/vtd 同一套（逐路独立判据配置）。 */
function _paintSelectedChannelUi(pfx, idx, per_channel) {
  const idxEl = document.getElementById(pfx + "-judge-selected-idx");
  const noEl = document.getElementById(pfx + "-judge-selected-no");
  const modeEl = document.getElementById(pfx + "-judge-selected-mode");
  const thrEl = document.getElementById(pfx + "-judge-selected-thr");
  const everyEl = document.getElementById(pfx + "-judge-selected-every-n");
  if (!idxEl) return;
  if (idx == null) {
    idxEl.textContent = "--";
    if (noEl) noEl.textContent = "--";
    if (modeEl) modeEl.textContent = "--";
    if (thrEl) thrEl.textContent = "--";
    if (everyEl) everyEl.textContent = "--";
    return;
  }
  // [x, y] 坐标写法：x = 0 基 idx（程序员语境），y = 1 基通道号（用户语境）。
  idxEl.textContent = "[" + idx + ", " + (idx + 1) + "]";
  if (noEl) noEl.textContent = String(idx + 1);
  const per = (per_channel || []).find(p => p.channel === idx);
  if (per) {
    if (modeEl) modeEl.textContent = per.mode || "--";
    if (thrEl) thrEl.textContent = per.threshold != null ? (+per.threshold).toFixed(2) : "--";
    if (everyEl) everyEl.textContent = per.every_n != null ? "每 " + per.every_n + " 帧" : "--";
  } else {
    if (modeEl) modeEl.textContent = "--";
    if (thrEl) thrEl.textContent = "--";
    if (everyEl) everyEl.textContent = "--";
  }
}

function _bindCardPerChannelJudge(pfx, gridId, apiBase) {
  const MG = window.MultiGrid;
  if (!MG) return;
  MG.bindSelect(gridId, async (idx) => {
    // 必须在 setSelectedChannel 之前：它同步写 .selected，之后再判二次点击恒真
    window.ZoomFrame?.onTileClick(gridId, idx);
    if (window.FrameJudge) window.FrameJudge.setSelectedChannel(pfx, idx);
    // **不要等下一条 WS tick**：用户点了 tile 就想立刻看见这路当前的
    // mode / threshold / every_n。
    if (idx == null || !apiBase) {
      _paintSelectedChannelUi(pfx, idx, null);
      return;
    }
    let per_channel = null;
    try {
      const j = await fetch(apiBase + "/judge", { cache: "no-store" })
                    .then(r => r.json());
      per_channel = j.per_channel || [];
      _paintSelectedChannelUi(pfx, idx, per_channel);
    } catch (_) {
      _paintSelectedChannelUi(pfx, idx, null);
    }
    if (per_channel && window.FrameJudge) {
      const per = per_channel.find((p) => p.channel === idx);
      if (per && per.mode != null) {
        window.FrameJudge.setConfig(pfx, {
          mode: per.mode, every_n: per.every_n, threshold: per.threshold,
        });
      }
    }
  });
  _paintSelectedChannelUi(pfx, null, null);
}
_bindCardPerChannelJudge("vdp", "vdp-grid", "/api/video-depth");

})();
