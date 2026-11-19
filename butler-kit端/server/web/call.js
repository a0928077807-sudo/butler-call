// 網頁版打電話／視訊（2026-11-18）。
// 為什麼有它：布丁 12:40「網頁版怎麼不能聽也不能打電話」。App 0.9.40 起每條對話都能撥，網頁版沒有。
// 照 App 的 CallViewModel／CallClient 同一套協定 v1（`說明.md`），
// 純邏輯（輪次過濾、送圖節流、降頻、切包、解封包）在 logic.js，有 node 測試。
//
// 跟 App 不一樣的地方：
// - 瀏覽器 WebSocket 帶不了 Authorization 標頭，token 放子協定 "bearer.<token>"（伺服器 webaccess._bearer）。
// - 收音用 AudioWorklet（call-worklet.js）拿到裝置原本的取樣率（多半 48k），自己降到 16k。
// - 麥克風只在 https（或 localhost）開得了，用 Tailscale 的 http 位址開網頁版會直接講原因。
// - AudioContext 一定要在按下撥號鍵的那一下建，晚了瀏覽器會擋聲音。
"use strict";
(function (g) {
  const L = g.Logic;
  const STATE_TEXT = {
    connecting: "連線中…", reconnecting: "重新連線中…", ended: "通話結束",
    listening: "聆聽中，請說話", thinking: "思考中…", speaking: "說話中",
  };
  let opts = null, call = null;

  function uuid() {
    if (g.crypto && crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  // ── 畫面 ──────────────────────────────────────────────────────────────────
  function buildUi(mode, title) {
    const ov = document.createElement("div");
    ov.className = "callov" + (mode === "video" ? " video" : "");
    ov.innerHTML =
      '<div class="cvhead"><span class="cvmode"></span><span class="cvconv"></span></div>' +
      '<video class="cvvid" muted playsinline autoplay></video>' +
      '<div class="cvmain"><div class="cvstate"></div><div class="cvlvl"><i></i></div>' +
      '<div class="cvyou"></div><div class="cvreply"></div><div class="cverr"></div></div>' +
      '<div class="cvbtns">' +
      '<button type="button" class="ghost cvmute"></button>' +
      '<button type="button" class="ghost cvint" disabled>' + ic("pan_tool") + ' 打斷</button>' +
      '<button type="button" class="ghost cvflip" hidden>' + ic("flip_camera_android") + ' 鏡頭</button>' +
      '<button type="button" class="danger cvhang">掛斷</button></div>';
    const q = (s) => ov.querySelector(s);
    q(".cvmode").innerHTML = mode === "video" ? ic("videocam") + " 視訊" : ic("call") + " 通話";
    q(".cvconv").textContent = title || "";
    document.body.appendChild(ov);
    return {
      ov, vid: q(".cvvid"), state: q(".cvstate"), lvl: q(".cvlvl i"), you: q(".cvyou"), reply: q(".cvreply"),
      err: q(".cverr"), mute: q(".cvmute"), int: q(".cvint"), flip: q(".cvflip"), hang: q(".cvhang"),
    };
  }
  function render(c) {
    const st = L.callState(c.link, c.playing, c.asrEnded);
    let t = STATE_TEXT[st] || st;
    if (st === "reconnecting") t = `重新連線中（第 ${c.attempts} 次）…`;
    if (st === "listening" && c.muted) t = "已靜音（按「取消靜音」再說話）";
    c.ui.state.textContent = t;
    c.ui.ov.dataset.state = st;
    c.ui.int.disabled = !(st === "thinking" || st === "speaking");
    c.ui.mute.innerHTML = c.muted ? ic("mic_off") + " 取消靜音" : ic("mic") + " 靜音";
    c.ui.hang.textContent = st === "ended" ? "關閉" : "掛斷";
    c.ui.you.textContent = c.you ? "你：" + c.you : "";
    c.ui.reply.textContent = c.reply.trim();
  }
  function setErr(c, msg) { c.ui.err.textContent = msg || ""; }

  // ── 撥出 ──────────────────────────────────────────────────────────────────
  async function start(mode) {
    if (call || !opts) return;
    const convId = opts.cur();
    const c = call = {
      mode, convId, callId: uuid(), link: "connecting", playing: false, asrEnded: false, muted: false,
      you: "", reply: "", attempts: 0, ws: null, closed: false, opened: false, lastPong: 0,
      filter: L.turnFilter(), gate: L.frameGate(), chunker: L.pcmChunker(),
      sources: new Set(), nextT: 0, pendingDone: -1, timers: [], facing: "user",
    };
    c.ui = buildUi(mode, opts.title ? opts.title(convId) : "");
    c.ui.hang.addEventListener("click", hangup);
    c.ui.mute.addEventListener("click", () => toggleMute(c));
    c.ui.int.addEventListener("click", () => interrupt(c));
    c.ui.flip.addEventListener("click", () => flipCamera(c).catch((e) => setErr(c, "換鏡頭失敗：" + e.message)));
    render(c);
    // iPhone：收音時聲音預設會被切去聽筒，指定成通話模式才會從喇叭出來（Safari 16.4 起有，其他瀏覽器沒有這個物件）
    try { if (navigator.audioSession) navigator.audioSession.type = "play-and-record"; } catch { /* 不支援就算了 */ }
    // 在按鈕的那一下建，晚了瀏覽器會把聲音擋掉
    try {
      c.playCtx = new AudioContext();
      c.micCtx = new AudioContext();
    } catch (e) { return fail(c, "這個瀏覽器不能播放通話聲音：" + e.message); }
    if (!g.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return fail(c, "這個網址不能用麥克風：瀏覽器只准 https 開的網頁收音，請改用 https 的網址開網頁版。");
    }
    try {
      c.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: mode === "video" ? { facingMode: c.facing, width: { ideal: 1280 }, height: { ideal: 720 } } : false,
      });
    } catch (e) {
      const what = mode === "video" ? "麥克風或鏡頭" : "麥克風";
      return fail(c, e && e.name === "NotAllowedError" ? `瀏覽器沒有允許使用${what}，到網址列左邊的鎖頭開權限再打一次。`
        : `${what}開不起來：${e && e.message || e}`);
    }
    if (c.closed) return stopMedia(c);
    try {
      await c.micCtx.audioWorklet.addModule("call-worklet.js?v=20261118f");
      const src = c.micCtx.createMediaStreamSource(c.stream);
      const node = new AudioWorkletNode(c.micCtx, "call-mic");
      const ds = L.downsampler(c.micCtx.sampleRate);
      node.port.onmessage = (e) => onMic(c, e.data, ds(e.data));
      // 處理器要接到輸出端才會一直被叫；中間墊一個音量 0，自己的聲音不會從喇叭出來
      const sink = c.micCtx.createGain(); sink.gain.value = 0;
      src.connect(node); node.connect(sink); sink.connect(c.micCtx.destination);
      c.micNode = node;
    } catch (e) { return fail(c, "收音處理開不起來：" + e.message); }
    if (c.closed) return stopMedia(c);
    if (mode === "video") {
      c.ui.vid.srcObject = c.stream;
      c.ui.vid.play().catch(() => {});
      c.canvas = document.createElement("canvas");
      c.timers.push(setInterval(() => sendFrame(c), 250));
      navigator.mediaDevices.enumerateDevices().then((ds) => {
        if (ds.filter((d) => d.kind === "videoinput").length > 1) c.ui.flip.hidden = false;
      }).catch(() => {});
    }
    c.playCtx.resume().catch(() => {});
    c.micCtx.resume().catch(() => {});
    connect(c);
  }

  function fail(c, msg) {
    c.link = "ended";
    setErr(c, msg);
    render(c);
    teardown(c, false);
  }

  // ── 連線 ──────────────────────────────────────────────────────────────────
  function connect(c) {
    if (c.closed) return;
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/web/api/v1/call";
    let ws;
    try { ws = new WebSocket(url, ["butler-call", "bearer." + opts.token()]); }
    catch (e) { return fail(c, "連不上通話：" + e.message); }
    ws.binaryType = "arraybuffer";
    c.ws = ws;
    c.link = c.attempts ? "reconnecting" : "connecting";
    render(c);
    ws.onopen = () => {
      if (ws !== c.ws || c.closed) return;
      c.opened = true;
      c.lastPong = Date.now();
      heartbeat(c);
      const m = { type: "start", mode: c.mode, call_id: c.callId };
      if (c.convId) m.conv_id = c.convId;
      ws.send(JSON.stringify(m));
    };
    ws.onmessage = (e) => {
      if (ws !== c.ws || c.closed) return;
      if (typeof e.data === "string") {
        let o; try { o = JSON.parse(e.data); } catch { return; }
        onText(c, o);
      } else onTts(c, e.data);
    };
    ws.onclose = (e) => {
      if (ws !== c.ws || c.closed) return;
      // 第一次就沒連上：多半是伺服器拒絕（授權失效、還沒重新啟動），重試十次也一樣，直接講
      if (!c.opened) return fail(c, "連不上通話。可能是伺服器還沒更新（要到工具頁按重新啟動），或這台的授權失效了。");
      dropped(c, `連線中斷（${e.code}）`);
    };
  }
  function heartbeat(c) {
    if (c.hb) clearInterval(c.hb);
    c.hb = setInterval(() => {
      const ws = c.ws;
      if (!ws || c.closed) return;
      if (Date.now() - c.lastPong > 16000) { c.ws = null; try { ws.close(); } catch { /* 已關 */ } dropped(c, "16 秒沒有回應"); return; }
      try { ws.send('{"type":"ping"}'); } catch { /* 下一輪會發現 */ }
    }, 8000);
  }
  function dropped(c, why) {
    c.ws = null;
    if (c.hb) { clearInterval(c.hb); c.hb = null; }
    stopPlayback(c);
    if (c.attempts >= L.CALL_MAX_RETRIES) return fail(c, "通話連線已中斷：" + why);
    c.attempts++;
    c.link = "reconnecting";
    setErr(c, `${why}，第 ${c.attempts} 次重連`);
    render(c);
    c.timers.push(setTimeout(() => connect(c), L.retryDelay(c.attempts)));
  }

  function send(c, obj) {
    if (c.ws && c.ws.readyState === 1) { c.ws.send(typeof obj === "string" || obj instanceof Uint8Array ? obj : JSON.stringify(obj)); return true; }
    return false;
  }

  function onText(c, o) {
    const turn = typeof o.turn === "number" ? o.turn : -1;
    switch (o.type) {
      case "ready":
        c.attempts = 0; c.filter.reset(); c.gate.reset(); stopPlayback(c);
        c.link = "ready"; setErr(c, ""); c.lastPong = Date.now();
        break;
      case "pong": c.lastPong = Date.now(); return;
      case "asr_ended":
        if (!c.filter.onAsrEnded(turn)) return;
        c.asrEnded = true; c.you = o.text || ""; c.reply = "";
        break;
      case "reply_delta":
        if (!c.filter.accept(turn)) return;
        c.reply += o.text || "";
        break;
      case "tts_end":
        if (!c.filter.accept(turn)) return;
        // 空辨識那輪沒有語音，補 250 ms 靜音，播完一樣送 playback_done（同 App）
        c.nextT = Math.max(c.nextT, c.playCtx.currentTime + 0.25);
        c.pendingDone = turn;
        c.doneDeadline = Date.now() + (c.nextT - c.playCtx.currentTime) * 1000 + 2000;
        waitDone(c);
        return;
      case "image_ack": c.gate.onAck(); return;
      case "error": setErr(c, o.message || "通話出了點狀況"); break;
      default: return;
    }
    render(c);
  }

  // ── 播放 24 kHz 語音 ────────────────────────────────────────────────────────
  function onTts(c, ab) {
    const p = L.parseTts(ab);
    if (!p || !c.filter.accept(p.turn) || !p.pcm.length) return;
    const ctx = c.playCtx;
    const buf = ctx.createBuffer(1, p.pcm.length, L.CALL_TTS_RATE);
    buf.copyToChannel(p.pcm, 0);
    const s = ctx.createBufferSource();
    s.buffer = buf; s.connect(ctx.destination);
    const t = Math.max(ctx.currentTime + 0.05, c.nextT);
    s.start(t);
    c.nextT = t + buf.duration;
    c.sources.add(s);
    s.onended = () => c.sources.delete(s);
    if (!c.playing) { c.playing = true; render(c); }
  }
  function waitDone(c) {
    if (c.donePoll) clearInterval(c.donePoll);
    c.donePoll = setInterval(() => {
      if (c.pendingDone < 0 || c.closed) { clearInterval(c.donePoll); c.donePoll = null; return; }
      if (c.playCtx.state !== "running") c.playCtx.resume().catch(() => {});
      if (c.playCtx.currentTime + 0.01 < c.nextT && Date.now() < c.doneDeadline) return;
      send(c, { type: "playback_done", turn: c.pendingDone });
      c.pendingDone = -1;
      c.playing = false; c.asrEnded = false;
      clearInterval(c.donePoll); c.donePoll = null;
      render(c);
    }, 100);
  }
  function stopPlayback(c) {
    for (const s of c.sources) { try { s.stop(); } catch { /* 還沒開始也沒關係 */ } }
    c.sources.clear();
    c.nextT = 0; c.pendingDone = -1;
    if (c.donePoll) { clearInterval(c.donePoll); c.donePoll = null; }
    c.playing = false; c.asrEnded = false;
  }

  // ── 收音、送圖 ──────────────────────────────────────────────────────────────
  function onMic(c, raw, pcm) {
    c.micN = (c.micN || 0) + 1;
    let sum = 0;
    for (let i = 0; i < raw.length; i++) sum += raw[i] * raw[i];
    const lv = Math.min(1, Math.sqrt(sum / (raw.length || 1)) * 4);
    c.ui.lvl.style.width = (c.muted ? 0 : Math.round(lv * 100)) + "%";
    const can = c.link === "ready" && !c.playing && !c.asrEnded && !c.muted;
    if (!can) { c.chunker.clear(); return; }
    for (const pkt of c.chunker.feed(pcm)) send(c, pkt);
  }
  function sendFrame(c) {
    const v = c.ui.vid;
    const listening = L.callState(c.link, c.playing, c.asrEnded) === "listening" && !c.muted;
    const now = Date.now();
    if (!v.videoWidth || !c.gate.shouldSend(listening, now)) return;
    const k = Math.min(1, 768 / Math.max(v.videoWidth, v.videoHeight));
    c.canvas.width = Math.round(v.videoWidth * k); c.canvas.height = Math.round(v.videoHeight * k);
    c.canvas.getContext("2d").drawImage(v, 0, 0, c.canvas.width, c.canvas.height);
    c.gate.onSent(now);
    c.canvas.toBlob(async (b) => {
      if (!b || c.closed) return;
      if (L.callState(c.link, c.playing, c.asrEnded) !== "listening" || c.muted) return;
      const body = new Uint8Array(await b.arrayBuffer());
      const pkt = new Uint8Array(body.length + 1);
      pkt[0] = 0x02; pkt.set(body, 1);
      send(c, pkt);
    }, "image/jpeg", 0.6);
  }
  async function flipCamera(c) {
    if (c.flipping || !c.stream) return;
    c.flipping = true;
    const want = c.facing === "user" ? "environment" : "user";
    const open = (facing) => navigator.mediaDevices.getUserMedia(
      { video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 720 } } });
    for (const t of c.stream.getVideoTracks()) { c.stream.removeTrack(t); t.stop(); }
    let ns, err = null;
    try { ns = await open(want); c.facing = want; }
    catch (e) { err = e; ns = await open(c.facing).catch(() => null); }
    c.flipping = false;
    // 等鏡頭的時候已經掛斷：新開的要自己關，不然鏡頭燈一直亮
    if (c.closed || !c.stream) { if (ns) ns.getTracks().forEach((t) => t.stop()); return; }
    if (ns) {
      c.stream.addTrack(ns.getVideoTracks()[0]);
      c.ui.vid.srcObject = null; c.ui.vid.srcObject = c.stream;
      c.ui.vid.play().catch(() => {});
    }
    if (err) throw err;
  }

  // ── 按鈕 ──────────────────────────────────────────────────────────────────
  function interrupt(c) {
    const st = L.callState(c.link, c.playing, c.asrEnded);
    if (st !== "thinking" && st !== "speaking") return;
    send(c, { type: "interrupt" });
    c.filter.onInterrupt();
    stopPlayback(c);
    render(c);
  }
  function toggleMute(c) {
    if (c.link === "ended") return;
    c.muted = !c.muted;
    send(c, { type: "mute", on: c.muted });
    render(c);
  }

  function stopMedia(c) {
    if (c.stream) for (const t of c.stream.getTracks()) t.stop();
    c.stream = null;
  }
  // 收掉連線與裝置；removeUi＝連畫面一起拿掉（掛斷）。失敗時留著畫面讓人看原因，按「關閉」才拿掉
  function teardown(c, removeUi) {
    c.closed = true;
    for (const t of c.timers) { clearInterval(t); clearTimeout(t); }
    c.timers = [];
    if (c.hb) clearInterval(c.hb);
    if (c.donePoll) clearInterval(c.donePoll);
    const ws = c.ws; c.ws = null;
    if (ws) {
      try { if (ws.readyState === 1) ws.send('{"type":"hangup"}'); ws.close(1000, "hangup"); } catch { /* 已關 */ }
    }
    for (const s of c.sources) { try { s.stop(); } catch { /* 沒開始 */ } }
    c.sources.clear();
    if (c.micNode) c.micNode.port.onmessage = null;
    stopMedia(c);
    try { if (navigator.audioSession) navigator.audioSession.type = "auto"; } catch { /* 不支援 */ }
    for (const ctx of [c.micCtx, c.playCtx]) if (ctx && ctx.state !== "closed") ctx.close().catch(() => {});
    if (removeUi) c.ui.ov.remove();
  }
  function hangup() {
    const c = call;
    if (!c) return;
    call = null;
    teardown(c, true);
  }

  function pick(anchor) {
    if (document.querySelector(".callpick")) return;
    const m = document.createElement("div");
    m.className = "callpick";
    m.innerHTML = '<button type="button" class="ghost" data-m="voice">' + ic("call") + ' 語音通話</button>' +
      '<button type="button" class="ghost" data-m="video">' + ic("videocam") + ' 視訊通話</button>';
    const r = anchor.getBoundingClientRect();
    m.style.top = Math.round(r.bottom + 6) + "px";
    m.style.right = Math.max(8, Math.round(document.documentElement.clientWidth - r.right)) + "px";
    document.body.appendChild(m);
    const off = (e) => { if (!m.contains(e.target)) close(); };
    const close = () => { m.remove(); document.removeEventListener("pointerdown", off, true); };
    setTimeout(() => document.addEventListener("pointerdown", off, true), 0);
    // 選的這一下就是使用者手勢，start 裡建 AudioContext 才不會被擋
    m.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-m]");
      if (!b) return;
      close();
      start(b.dataset.m);
    });
  }

  function init(o) {
    const first = !opts;
    opts = o;
    if (!first) return;
    o.voiceBtn.addEventListener("click", () => {
      // 窄螢幕標題列放不下兩顆（360 寬時登出會被擠出去），只留 📞，按了再選語音或視訊
      if (!o.videoBtn.offsetParent) return pick(o.voiceBtn);
      start("voice");
    });
    o.videoBtn.addEventListener("click", () => start("video"));
    // 關分頁、重新整理時告訴伺服器掛斷，別讓引擎那邊空等
    g.addEventListener("pagehide", hangup);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && call && call.link === "ended") hangup(); });
  }

  g.WebCall = { init, start, hangup, active: () => !!call, _state: () => call && {
    link: call.link, playing: call.playing, asrEnded: call.asrEnded, muted: call.muted, you: call.you, reply: call.reply,
    attempts: call.attempts, micN: call.micN || 0, mic: call.micCtx && call.micCtx.state,
    play: call.playCtx && call.playCtx.state, state: L.callState(call.link, call.playing, call.asrEnded), err: call.ui.err.textContent,
  } };
})(window);
