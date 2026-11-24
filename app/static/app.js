/* Twin — single-page frontend (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, ms = 4000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(path, o);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${r.status})`);
  return data;
}

let ME = null;
async function loadMe() { ME = await api("/api/me"); renderNav(); return ME; }

function renderNav() {
  const nav = $("#nav");
  if (!ME?.user) { nav.innerHTML = ""; return; }
  nav.innerHTML = ME.user.is_guest
    ? `<span class="pill">Guest · deleted in ${ME.user.guest_days_left} days unless saved</span>
       <button onclick="saveTwin()">Save my twin</button>
       <button class="ghost" onclick="go('/')">My twin</button>`
    : `<span class="status">${esc(ME.user.email)}</span>
       <button class="ghost" onclick="go('/')">My twin</button>
       <button class="ghost" onclick="logout()">Log out</button>`;
}
function saveTwin() {
  const d = document.createElement("div"); d.className = "modal";
  d.innerHTML = `<div class="card auth"><h2>Save your twin</h2>
    <p class="sub">Keep everything you've built. Saved twins can also be shared with a public link.</p>
    <form id="cf"><label>Email</label><input name="email" type="email" required>
    <label>Password</label><input name="password" type="password" minlength="8" required>
    <div class="row" style="margin-top:16px"><button>Save</button><button type="button" class="ghost" id="cx">Cancel</button></div></form></div>`;
  document.body.appendChild(d);
  $("#cx", d).onclick = () => d.remove();
  $("#cf", d).onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/claim", { method: "POST", json: Object.fromEntries(new FormData(e.target)) }); d.remove(); toast("Saved! Your twin is yours to keep."); route(); }
    catch (err) { toast(err.message); }
  };
}
async function logout() { await api("/api/logout", { method: "POST" }); go("/"); }
function go(path) { history.pushState({}, "", path); route(); }
window.addEventListener("popstate", route);

async function route() {
  const m = location.pathname.match(/^\/t\/([\w-]+)/);
  await loadMe().catch(() => (ME = { user: null }));
  if (m) return renderChatPage(m[1]);
  if (!ME.user) return renderAuth();
  if (!ME.status.ready || location.hash === "#setup") return renderOnboarding();
  return renderDashboard();
}

/* ---------------- auth ---------------- */
function renderAuth(mode = "register") {
  view.innerHTML = `
  <div class="hero"><h1>Your AI twin.</h1>
  <p class="sub">Add a photo, read a few lines out loud, and tell it about yourself.<br>
  Your twin looks like you, sounds like you, thinks like you, and stands in for you.</p></div>
  <div class="card auth">
    <h2>${mode === "register" ? "Create your twin" : "Welcome back"}</h2>
    <form id="f">
      ${mode === "register" ? `<label>Your name</label><input name="name" required placeholder="What people call you">` : ""}
      <label>Email</label><input name="email" type="email" required>
      <label>Password</label><input name="password" type="password" minlength="8" required>
      <div class="row" style="margin-top:18px"><button>${mode === "register" ? "Start" : "Log in"}</button>
      <a href="#" id="sw">${mode === "register" ? "I already have an account" : "Create an account"}</a></div>
    </form>
    <div class="or"><span>or</span></div>
    <button class="ghost" id="guest" style="width:100%;justify-content:center">Try it without signing up →</button>
    <p class="hint" style="text-align:center;margin-top:8px">No email needed. Save your twin later if you like it.</p></div>`;
  $("#guest").onclick = async () => {
    const name = (document.querySelector('input[name=name]')?.value || "").trim() || prompt("What should your twin call you?", "") || "";
    try { await api("/api/guest", { method: "POST", json: { name } }); route(); } catch (err) { toast(err.message); }
  };
  $("#sw").onclick = (e) => { e.preventDefault(); renderAuth(mode === "register" ? "login" : "register"); };
  $("#f").onsubmit = async (e) => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.target));
    try { await api(mode === "register" ? "/api/register" : "/api/login", { method: "POST", json: fd }); route(); }
    catch (err) { toast(err.message); }
  };
}

/* ---------------- recording helper ---------------- */
class Recorder {
  async start(onLevel) {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    this.chunks = [];
    this.mr = new MediaRecorder(this.stream);
    this.mr.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.mr.start(250);
    this.ctx = new AudioContext();
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.an = this.ctx.createAnalyser(); this.an.fftSize = 1024; src.connect(this.an);
    const buf = new Uint8Array(this.an.fftSize);
    const tick = () => {
      if (!this.mr || this.mr.state !== "recording") return;
      this.an.getByteTimeDomainData(buf);
      let s = 0; for (const v of buf) s += (v - 128) ** 2;
      onLevel?.(Math.sqrt(s / buf.length) / 128);
      this.raf = requestAnimationFrame(tick);
    };
    tick();
  }
  stop() {
    return new Promise((res) => {
      this.mr.onstop = () => {
        this.stream.getTracks().forEach((t) => t.stop()); this.ctx.close(); cancelAnimationFrame(this.raf);
        res(new Blob(this.chunks, { type: this.mr.mimeType || "audio/webm" }));
      };
      this.mr.stop();
    });
  }
}
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

async function upload(path, blob, name = "file") {
  const fd = new FormData(); fd.append("file", blob, name);
  return api(path, { method: "POST", body: fd });
}

/* ---------------- onboarding ---------------- */
const STEPS = ["Your face", "Your voice", "About you", "Your writing"];
let stepIdx = 0;

function renderOnboarding() {
  const s = ME.status;
  const done = [s.headshot, s.voice, s.profile, !!ME.writing_samples];
  if (stepIdx === 0 && !location.hash) stepIdx = Math.max(0, done.indexOf(false));
  view.innerHTML = `<h1>Build your twin</h1>
    <p class="sub">Four quick steps. The more you give it, the more it sounds and thinks like you.</p>
    <div class="steps">${STEPS.map((n, i) => `<span class="step ${i === stepIdx ? "on" : ""} ${done[i] ? "done" : ""}" data-i="${i}">${done[i] ? "✓ " : ""}${i + 1}. ${n}</span>`).join("")}
    ${s.ready ? `<span class="step" onclick="location.hash='';go('/')">→ Talk to my twin</span>` : ""}</div>
    <div class="card" id="stepBody"></div>`;
  view.querySelectorAll(".step[data-i]").forEach((el) => (el.onclick = () => { stepIdx = +el.dataset.i; renderOnboarding(); }));
  [stepFace, stepVoice, stepQuestions, stepWriting][stepIdx]($("#stepBody"));
}
async function nextStep() { await loadMe(); stepIdx = Math.min(stepIdx + 1, STEPS.length - 1); renderOnboarding(); }

/* Step 1: photo → stylized avatar options → pick one */
let designOpen = false, CATALOG = null, DESIGN = { style: "3d", outfit: "keep", accessories: [], background: "plain", extra: "" };
async function mountDesigner(box, busy, onStarted) {
  CATALOG ||= await api("/api/avatar/catalog");
  const chips = (group, items, multi) => `<div class="chips" data-g="${group}">${items.map((i) => {
    const on = multi ? DESIGN[group].includes(i.id) : DESIGN[group] === i.id;
    return `<button type="button" class="chip ${on ? "on" : ""}" data-id="${i.id}">${esc(i.label)}</button>`; }).join("")}</div>`;
  box.innerHTML = `
    <label>Style</label>${chips("style", CATALOG.styles)}
    <label>Outfit</label>${chips("outfit", CATALOG.outfits)}
    <label>Accessories <span class="hint">· up to 4</span></label>${chips("accessories", CATALOG.accessories, true)}
    <label>Background</label>${chips("background", CATALOG.backgrounds)}
    <label>Anything else? <span class="hint">· optional, e.g. "navy hoodie with a small rocket logo"</span></label>
    <input id="extra" maxlength="160" value="${esc(DESIGN.extra)}">
    <div class="row" style="margin-top:14px"><button type="button" id="gen" ${busy ? "disabled" : ""}>✨ Create this avatar</button>
      <span class="hint">${busy ? "Available as soon as the current avatars finish." : "Made from your original photo, so it still looks like you. About 30–60 seconds."}</span></div>`;
  box.querySelectorAll(".chips").forEach((g) => g.querySelectorAll(".chip").forEach((c) => (c.onclick = () => {
    const k = g.dataset.g, id = c.dataset.id;
    if (k === "accessories") {
      const a = DESIGN.accessories;
      if (a.includes(id)) a.splice(a.indexOf(id), 1); else if (a.length < 4) a.push(id); else return toast("Pick up to 4 accessories");
    } else DESIGN[k] = id;
    DESIGN.extra = $("#extra", box).value; mountDesigner(box, busy, onStarted);
  })));
  $("#extra", box).oninput = (e) => (DESIGN.extra = e.target.value);
  $("#gen", box).onclick = async () => {
    try { onStarted(await api("/api/twin/avatar/design", { method: "POST", json: DESIGN })); } catch (err) { toast(err.message); }
  };
}

function stepFace(el) {
  let poll;
  const render = (av) => {
    clearTimeout(poll);
    const gen = av.status === "generating", failed = (av.status || "").startsWith("failed");
    const tiles = [...av.options.map((o) => ({ key: o.style, label: o.label, url: o.url }))];
    if (av.has_photo) tiles.push({ key: "photo", label: "My photo", url: "/api/twin/photo?t=" + Date.now() });
    el.innerHTML = `<h2>1. Your face</h2>
      <p class="sub">Upload a clear, front-facing photo. ${av.available ? "We turn it into avatars that look like you, and you pick the one that talks for you. Your original photo stays private." : "It becomes your twin's talking face."}</p>
      <div class="row"><button id="cam">📷 Take a selfie</button><span class="hint">or</span><input type="file" id="img" accept="image/*" style="width:auto">
        ${av.has_photo && av.available && !gen ? `<button class="ghost" id="regen">↻ New variations</button>` : ""}</div>
      ${gen ? `<div class="gen"><div class="spinner"></div><div><b>Creating your avatars…</b><br><span class="hint">This takes 1–2 minutes the first time. You can keep this page open.</span></div></div>` : ""}
      ${failed ? `<p class="bad">Avatar generation failed. You can use your photo, or try new variations.</p>` : ""}
      ${tiles.length ? `<div class="avatars">${tiles.map((t) => `
        <button class="avatar-opt ${av.chosen_key === t.key ? "picked" : ""}" data-k="${t.key}">
          <img src="${t.url}" alt=""><span>${esc(t.label)}</span></button>`).join("")}</div>` : ""}
      ${av.has_photo && av.available ? `<details class="designer" ${designOpen ? "open" : ""}><summary>🎨 Design your avatar: style, outfit, accessories</summary><div id="dz"></div></details>` : ""}
      <div class="row" style="margin-top:16px"><button id="nx" ${ME.status.headshot || av.has_photo ? "" : "disabled"}>Continue →</button>
        ${av.has_photo ? `<span class="hint">Tap any look to make it your twin's face. You can change it later.</span>` : ""}</div>`;
    if ($("#dz", el)) mountDesigner($("#dz", el), gen, (r) => { designOpen = true; render(r); });
    $(".designer", el)?.addEventListener("toggle", (e) => (designOpen = e.target.open));
    $("#img", el).onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      if (!(await vetUploadedPhoto(f))) { e.target.value = ""; return; }
      try { render(await upload("/api/twin/headshot", f, f.name)); } catch (err) { toast(err.message); }
    };
    $("#cam", el).onclick = async () => {
      const blob = await takeSelfie(); if (!blob) return;
      try { render(await upload("/api/twin/headshot", blob, "selfie.jpg")); } catch (err) { toast(err.message); }
    };
    $("#regen", el) && ($("#regen", el).onclick = async () => { try { render(await api("/api/twin/avatar/regenerate", { method: "POST" })); } catch (err) { toast(err.message); } });
    el.querySelectorAll(".avatar-opt").forEach((b) => (b.onclick = async () => {
      const r = await api("/api/twin/avatar/choose", { method: "POST", json: { style: b.dataset.k } });
      r.chosen_key = b.dataset.k; ME.status.headshot = true; render(r);
    }));
    $("#nx", el).onclick = nextStep;
    if (gen) poll = setTimeout(async () => { if (el.isConnected) render(await api("/api/twin/avatar")); }, 4000);
  };
  render(ME.avatar);
}

/* ---------- photo quality: runs fully in the browser (MediaPipe face detector, self-hosted) ---------- */
let _fd = null, _fdFailed = false;
async function faceDetector() {
  if (_fd || _fdFailed) return _fd;
  try {
    const { FilesetResolver, FaceDetector } = await import("/static/vendor/mediapipe/vision_bundle.mjs");
    const files = await FilesetResolver.forVisionTasks("/static/vendor/mediapipe/wasm");
    _fd = await FaceDetector.createFromOptions(files, {
      baseOptions: { modelAssetPath: "/static/vendor/mediapipe/blaze_face_short_range.tflite", delegate: "CPU" },
      runningMode: "IMAGE", minDetectionConfidence: 0.5 });
  } catch (e) { console.warn("face detector unavailable", e); _fdFailed = true; }
  return _fd;
}

/* Returns [{label, ok, tip}] for a frame drawn on `canvas`. */
async function checkPhoto(canvas) {
  const W = canvas.width, H = canvas.height, g = canvas.getContext("2d", { willReadFrequently: true });
  const checks = [], add = (label, ok, tip) => checks.push({ label, ok, tip });
  const fd = await faceDetector();
  let box = { x: W * 0.25, y: H * 0.15, w: W * 0.5, h: H * 0.6 }; // fallback region if no detector
  if (fd) {
    const dets = fd.detect(canvas).detections || [];
    add("One face", dets.length === 1, dets.length ? "Only you in the photo" : "We can't see your face: face the camera and move closer");
    if (dets.length !== 1) return checks;
    const d = dets[0], bb = d.boundingBox, k = d.keypoints; // k: right eye, left eye, nose, mouth, ears
    box = { x: bb.originX, y: bb.originY, w: bb.width, h: bb.height };
    const fw = bb.width / W, cx = (bb.originX + bb.width / 2) / W, cy = (bb.originY + bb.height / 2) / H;
    add("Close enough", fw >= 0.28 && fw <= 0.75, fw < 0.28 ? "Move closer: fill the oval with your face" : "Move back a little");
    add("Centered", cx > 0.35 && cx < 0.65 && cy > 0.28 && cy < 0.66, "Center your face in the oval");
    if (k && k.length >= 4) {
      const [re, le, nose, mouth] = k, eyeDist = Math.hypot(le.x - re.x, le.y - re.y) || 1e-6;
      const roll = Math.abs(Math.atan2(le.y - re.y, le.x - re.x) * 180 / Math.PI);
      const level = Math.min(roll, Math.abs(180 - roll));
      add("Head level", level < 10, "Straighten your head: don't tilt it");
      const mid = { x: (le.x + re.x) / 2, y: (le.y + re.y) / 2 };
      const yaw = Math.abs(nose.x - mid.x) / eyeDist;
      const pitch = (nose.y - mid.y) / ((mouth.y - mid.y) || 1e-6);
      add("Facing the camera", yaw < 0.2 && pitch > 0.3 && pitch < 0.8,
        yaw >= 0.2 ? "Turn to look straight at the camera" : "Hold the camera at eye level (not above or below)");
    }
  }
  // light, measured on the face itself
  const x0 = Math.max(0, box.x | 0), y0 = Math.max(0, box.y | 0);
  const w = Math.max(1, Math.min(W - x0, box.w | 0)), h = Math.max(1, Math.min(H - y0, box.h | 0));
  const px = g.getImageData(x0, y0, w, h).data;
  let sum = 0, hot = 0, n = 0, left = 0, right = 0, nl = 0, nr = 0;
  for (let i = 0; i < px.length; i += 16) {
    const L = 0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2];
    sum += L; n++; if (L > 245) hot++;
    const col = (i / 4) % w;
    if (col < w / 2) { left += L; nl++; } else { right += L; nr++; }
  }
  const mean = sum / n, lr = Math.abs(left / nl - right / nr) / (mean || 1);
  add("Enough light", mean >= 80 && mean <= 210, mean < 80 ? "Too dark: face a window or a lamp" : "Too bright: step out of direct light");
  add("No glare", hot / n < 0.06, "Harsh light or glare on your face: move away from direct light");
  add("Even light", lr < 0.35, "One side of your face is in shadow: turn toward the light");
  return checks;
}

function frameToCanvas(src, maxSide = 640, mirror = false) {
  const sw = src.videoWidth || src.naturalWidth || src.width, sh = src.videoHeight || src.naturalHeight || src.height;
  const k = Math.min(1, maxSide / Math.max(sw, sh)), c = document.createElement("canvas");
  c.width = Math.round(sw * k); c.height = Math.round(sh * k);
  const g = c.getContext("2d", { willReadFrequently: true });
  if (mirror) { g.translate(c.width, 0); g.scale(-1, 1); }
  g.drawImage(src, 0, 0, c.width, c.height);
  return c;
}

/* For uploaded files: same checks, then ask before spending GPU time on a weak photo. */
async function vetUploadedPhoto(file) {
  try {
    const img = new Image(); img.src = URL.createObjectURL(file); await img.decode();
    const bad = (await checkPhoto(frameToCanvas(img))).filter((c) => !c.ok);
    if (!bad.length) return true;
    return confirm("This photo may not look much like you once it's turned into an avatar:\n\n• " +
      bad.map((c) => c.tip).join("\n• ") + "\n\nTip: 📷 Take a selfie coaches you to a great shot.\n\nUse this photo anyway?");
  } catch { return true; }
}

/* Webcam selfie with live coaching: the oval turns green and Capture unlocks when the shot is good. */
async function takeSelfie() {
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 1280 } } }); }
  catch { toast("Camera permission is needed to take a selfie"); return null; }
  const d = document.createElement("div"); d.className = "modal";
  d.innerHTML = `<div class="card cam"><h2>Take a selfie</h2>
    <div class="camwrap"><div class="camview"><video autoplay playsinline muted></video><div class="oval"></div><div class="count"></div><img hidden alt=""></div>
      <ul class="checks"><li class="hint">Starting face check…</li></ul></div>
    <p class="hint" id="tip">Camera at eye level, face a window or lamp, mouth closed.</p>
    <div class="row" style="margin-top:10px"><button id="snap" disabled>📸 Capture</button><button id="use" hidden>Use this photo</button>
      <button class="ghost" id="retake" hidden>Retake</button><button class="ghost" id="anyway" hidden>Capture anyway</button>
      <button class="ghost" id="cx">Cancel</button></div></div>`;
  document.body.appendChild(d);
  const video = $("video", d), img = $("img", d), count = $(".count", d), oval = $(".oval", d), list = $(".checks", d);
  video.srcObject = stream;
  let shot = null, live = true, goodStreak = 0, started = Date.now();
  const loop = async () => {
    if (!live || !d.isConnected) return;
    if (video.videoWidth) {
      const checks = await checkPhoto(frameToCanvas(video, 480));
      const ok = checks.length > 1 && checks.every((c) => c.ok);
      goodStreak = ok ? goodStreak + 1 : 0;
      list.innerHTML = checks.map((c) => `<li class="${c.ok ? "y" : "n"}">${c.ok ? "✓" : "✗"} ${esc(c.label)}</li>`).join("");
      const firstBad = checks.find((c) => !c.ok);
      $("#tip", d).textContent = firstBad ? "👉 " + firstBad.tip : "✨ Looks great! Hold still and capture.";
      oval.classList.toggle("good", goodStreak >= 2);
      $("#snap", d).disabled = goodStreak < 2;
      $("#anyway", d).hidden = goodStreak >= 2 || Date.now() - started < 8000;
    }
    setTimeout(loop, 220);
  };
  loop();
  return new Promise((resolve) => {
    const close = (v) => { live = false; stream.getTracks().forEach((t) => t.stop()); d.remove(); resolve(v); };
    const capture = async () => {
      live = false; $("#snap", d).disabled = true; $("#anyway", d).hidden = true;
      for (const n of [3, 2, 1]) { count.textContent = n; await new Promise((r) => setTimeout(r, 650)); }
      count.textContent = "";
      const side = Math.min(video.videoWidth, video.videoHeight), c = document.createElement("canvas");
      c.width = c.height = Math.min(1024, side);
      const g = c.getContext("2d");
      g.translate(c.width, 0); g.scale(-1, 1); // un-mirror so the selfie looks natural
      g.drawImage(video, (video.videoWidth - side) / 2, (video.videoHeight - side) / 2, side, side, 0, 0, c.width, c.height);
      shot = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.92));
      img.src = URL.createObjectURL(shot); img.hidden = false; video.hidden = true; oval.hidden = true;
      $("#snap", d).hidden = true; $("#use", d).hidden = false; $("#retake", d).hidden = false;
    };
    $("#cx", d).onclick = () => close(null);
    $("#snap", d).onclick = capture;
    $("#anyway", d).onclick = capture;
    $("#retake", d).onclick = () => {
      img.hidden = true; video.hidden = false; oval.hidden = false; shot = null; goodStreak = 0; started = Date.now();
      $("#snap", d).hidden = false; $("#use", d).hidden = true; $("#retake", d).hidden = true; live = true; loop();
    };
    $("#use", d).onclick = () => close(shot);
  });
}

/* Step 2: one short read-aloud = consent + voice sample, then the twin greets you in your own voice */
async function stepVoice(el) {
  const s = ME.status;
  fetch("/api/warm", { method: "POST" }).catch(() => {}); // boot the GPU while they read
  const { script } = await api("/api/twin/consent-script");
  el.innerHTML = `<h2>2. Your voice</h2>
    <p class="sub">Read this out loud in your normal voice, in a quiet room (about 15 seconds). The code words prove it's really your voice, and they change every time.
      ${s.voice ? `<br><span class="pill ok">✓ Voice saved (${s.voice_seconds}s). Record again any time to improve it.</span>` : ""}</p>
    <div class="script">${esc(script)}</div>
    <div class="rec"><button id="rb">🎙 Start reading</button><b id="tm">0:00</b><div class="meter"><i id="lv"></i></div></div>
    <p class="hint">Tip: after the script, keep talking about your day for a bit. More speech makes the clone closer to you.</p>
    <div id="welcome" hidden></div>
    <div style="margin-top:14px"><button id="nx" ${s.voice ? "" : "disabled"}>Continue →</button></div>`;
  let rec = null, t0 = 0, timer = null;
  $("#nx", el).onclick = nextStep;
  $("#rb", el).onclick = async () => {
    if (!rec) {
      rec = new Recorder();
      try { await rec.start((l) => ($("#lv", el).style.width = Math.min(100, l * 400) + "%")); } catch { toast("Microphone permission is needed"); rec = null; return; }
      t0 = Date.now(); $("#rb", el).textContent = "■ Done";
      timer = setInterval(() => ($("#tm", el).textContent = fmt((Date.now() - t0) / 1000)), 250);
      return;
    }
    clearInterval(timer); const blob = await rec.stop(); rec = null;
    $("#rb", el).disabled = true; $("#tm", el).textContent = "Learning your voice…";
    try {
      await upload("/api/twin/voice", blob, "voice.webm");
      let r;
      for (let i = 0; ; i++) {  // GPU work runs in the background; wait for it
        await new Promise((ok) => setTimeout(ok, 2500));
        r = await api("/api/twin/voice/status");
        if (r.status === "ready") break;
        if (r.status === "failed") throw new Error(r.error);
        $("#tm", el).textContent = i < 6 ? "Learning your voice…" : "Waking up the GPU… (first time takes a minute)";
      }
      await loadMe();
      const w = $("#welcome", el); w.hidden = false;
      w.innerHTML = `<div class="welcome"><div class="avatar small"><img id="wface" src="${ME.twin.headshot_url || ""}" alt=""><div class="ring" id="wring"></div></div>
        <div><b>Your twin says:</b><p>"${esc(r.welcome_text)}"</p>
        ${r.welcome_audio_url ? `<button class="ghost" id="replay">▶ Play again</button>` : `<span class="hint">(Couldn't generate the greeting audio this time.)</span>`}</div></div>`;
      $("#tm", el).textContent = `✓ ${r.seconds}s saved`;
      $("#nx", el).disabled = false; $("#rb", el).disabled = false; $("#rb", el).textContent = "🎙 Record again";
      if (r.welcome_audio_url) {
        const playIt = () => speakWithFace(r.welcome_audio_url, $("#wface", el), $("#wring", el));
        $("#replay", el).onclick = playIt; playIt();
      }
    } catch (err) {
      toast(err.message, 8000); $("#tm", el).textContent = "0:00";
      $("#rb", el).disabled = false; $("#rb", el).textContent = "🎙 Try again";
      // the code words are single-use; get a fresh script
      setTimeout(() => stepVoice(el), 1500);
    }
  };
}

/* play audio and make a face image "talk" (pulse with the voice) */
let _actx;
function speakWithFace(url, faceEl, ringEl) {
  return new Promise((resolve) => {
    const a = new Audio(url); a.crossOrigin = "anonymous";
    _actx ||= new AudioContext();
    const src = _actx.createMediaElementSource(a), an = _actx.createAnalyser(); an.fftSize = 512;
    src.connect(an); an.connect(_actx.destination);
    const buf = new Uint8Array(an.fftSize);
    const loop = () => {
      if (a.paused || a.ended) { if (faceEl) faceEl.style.transform = ""; if (ringEl) ringEl.style.boxShadow = ""; return; }
      an.getByteTimeDomainData(buf); let s = 0; for (const v of buf) s += (v - 128) ** 2;
      const lvl = Math.min(1, Math.sqrt(s / buf.length) / 40);
      if (faceEl) faceEl.style.transform = `scale(${1 + lvl * 0.025}) translateY(${-lvl * 3}px)`;
      if (ringEl) ringEl.style.boxShadow = `inset 0 0 0 ${2 + lvl * 6}px rgba(124,156,255,${0.3 + lvl * 0.6})`;
      requestAnimationFrame(loop);
    };
    a.onended = a.onerror = () => resolve();
    a.play().then(loop).catch(() => resolve());
  });
}

async function stepQuestions(el) {
  const qs = await api("/api/questions");
  const answers = { ...(ME.answers || {}) };
  const sections = [...new Set(qs.map((q) => q.section))];
  el.innerHTML = `<h2>4. About you</h2>
    <p class="sub">Answer the way you'd say it out loud: your words become your twin's words. Skip anything you like; only the two starred questions are required. Saves automatically.</p>
    <span class="pill" id="cnt"></span>
    ${sections.map((sec) => `<h3>${esc(sec)}</h3>${qs.filter((q) => q.section === sec).map((q) => `
      <div class="qsec"><label>${esc(q.question)}${q.required ? " *" : ""} ${q.hint ? `<span class="hint">· ${esc(q.hint)}</span>` : ""}</label>
      <textarea data-q="${q.id}">${esc(answers[q.id] || "")}</textarea></div>`).join("")}`).join("")}
    <div class="row" style="margin-top:18px"><button id="nx">Save & continue →</button></div>`;
  const count = () => ($("#cnt").textContent = `${Object.values(answers).filter((v) => v.trim()).length} / ${qs.length} answered`);
  count();
  let saveT;
  const save = () => api("/api/twin/profile", { method: "PUT", json: { answers } });
  el.querySelectorAll("textarea[data-q]").forEach((ta) => (ta.oninput = () => {
    answers[ta.dataset.q] = ta.value; count(); clearTimeout(saveT); saveT = setTimeout(save, 800);
  }));
  $("#nx").onclick = async () => {
    const r = await save();
    if (!r.status.profile) return toast("Please answer the starred questions (your name, and how you talk)");
    nextStep();
  };
}

function stepWriting(el) {
  el.innerHTML = `<h2>5. Things you've written</h2>
    <p class="sub">Paste real messages, emails, posts or chats you've written (remove other people's private info). This is the best way to capture your texting style. Optional, but it helps a lot.</p>
    <textarea id="ws" style="min-height:260px" placeholder="e.g. paste 20 of your recent WhatsApp/Slack messages…">${esc(ME.writing_samples)}</textarea>
    <div class="row" style="margin-top:14px"><button id="nx">${ME.status.ready ? "Save & meet my twin →" : "Save"}</button></div>`;
  $("#nx").onclick = async () => {
    const r = await api("/api/twin/profile", { method: "PUT", json: { writing_samples: $("#ws").value } });
    await loadMe();
    if (r.status.ready) { location.hash = ""; go("/"); } else { toast("Saved. Finish the other steps to activate your twin."); renderOnboarding(); }
  };
}

/* ---------------- dashboard (owner) ---------------- */
async function renderDashboard() {
  const t = ME.twin;
  const share = `${location.origin}/t/${t.slug}`;
  view.innerHTML = `<div class="grid2">
    <div>
      <div class="card">
        <h2>${esc(t.display_name)}'s twin</h2>
        <ul class="check" style="padding-left:18px">
          <li class="y">✓ Avatar</li><li class="y">✓ Voice clone (${ME.status.voice_seconds}s)</li>
          <li class="${ME.status.answered > 15 ? "y" : "n"}">${ME.status.answered}/${ME.status.total_questions} questions answered</li>
          <li class="${ME.writing_samples ? "y" : "n"}">${ME.writing_samples ? "✓" : "○"} Writing samples</li></ul>
        <button class="ghost" onclick="location.hash='setup';stepIdx=2;route()">Edit profile</button>
        <h3>Share</h3>
        ${ME.user.is_guest ? `<p class="hint">Save your twin (top right) to share it with a public link.</p>` : `<label class="row" style="font-weight:400"><input type="checkbox" id="pub" style="width:auto" ${t.public ? "checked" : ""}> Let anyone with the link talk to my twin</label>`}
        <div class="row" style="margin-top:8px"><input readonly value="${share}" id="sl"><button class="ghost" id="cp">Copy</button></div>
        <h3>Teach your twin</h3>
        <div class="row"><input id="fact" placeholder="A new fact, e.g. 'I just started learning tennis'"><button class="ghost" id="addf">Add</button></div>
        <p class="hint">Tip: in the chat, click "not how I'd say it" under any reply to correct your twin. It learns from that.</p>
        <div id="mems"></div>
        <h3>Danger zone</h3>
        <button class="danger" id="del">Delete my twin and all my data</button>
      </div>
    </div>
    <div><div class="tabs"><button class="tab on" data-tab="talk">💬 Talk to my twin</button><button class="tab" data-tab="dream">✨ Dream Life</button></div>
      <div id="chatHost"></div><div id="dreamHost" hidden></div></div></div>`;
  view.querySelectorAll(".tab").forEach((b) => (b.onclick = () => {
    view.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === b));
    $("#chatHost").hidden = b.dataset.tab !== "talk"; $("#dreamHost").hidden = b.dataset.tab !== "dream";
    if (b.dataset.tab === "dream" && !$("#dreamHost").dataset.ready) { $("#dreamHost").dataset.ready = 1; mountDreams($("#dreamHost")); }
  }));
  $("#pub") && ($("#pub").onchange = async (e) => { await api("/api/twin/profile", { method: "PUT", json: { public: e.target.checked } }); toast(e.target.checked ? "Your twin is now shareable" : "Your twin is private"); });
  $("#cp").onclick = () => { navigator.clipboard.writeText(share); toast("Link copied"); };
  $("#addf").onclick = async () => { const v = $("#fact").value.trim(); if (!v) return; await api("/api/twin/memories", { method: "POST", json: { kind: "fact", content: v } }); $("#fact").value = ""; loadMems(); };
  $("#del").onclick = async () => {
    if (!confirm("Permanently delete your twin: voice, face, answers, memories and chats? This cannot be undone.")) return;
    await api("/api/account", { method: "DELETE" }); go("/");
  };
  loadMems();
  mountChat($("#chatHost"), t.slug, { isOwner: true, twin: t });
}
async function loadMems() {
  const ms = await api("/api/twin/memories");
  $("#mems").innerHTML = ms.slice(0, 30).map((m) => `<div class="mem"><span>${m.kind === "correction" ? `✎ <i>"${esc(m.prompt)}"</i> → ${esc(m.content)}` : esc(m.content)}</span><button class="ghost" data-id="${m.id}">✕</button></div>`).join("");
  $("#mems").querySelectorAll("button").forEach((b) => (b.onclick = async () => { await api(`/api/twin/memories/${b.dataset.id}`, { method: "DELETE" }); loadMems(); }));
}

/* ---------------- Dream Life reels ---------------- */
let dreamStyle = "realistic";
async function mountDreams(host) {
  const meta = await api("/api/dreams/ideas");
  host.innerHTML = `<div class="card">
    <h2>✨ Dream Life</h2>
    <p class="sub">Describe a day you dream of. Your twin films a 15-second reel of <b>you</b> living it, narrated in your voice.</p>
    ${meta.available ? "" : `<p class="bad">Dream reels need the cloud GPU backend.</p>`}
    <textarea id="dreamText" maxlength="500" placeholder="e.g. I'm in New York: morning coffee, a jog by the Hudson, then work downtown in a sharp suit"></textarea>
    <div class="chips" style="margin-top:8px">${meta.ideas.map((i) => `<button type="button" class="chip idea">${esc(i.split(":")[0].split(",")[0])}…</button>`).join("")}</div>
    <label>Look</label><div class="chips" id="dstyle">${meta.styles.map((st) => `<button type="button" class="chip ${st.id === dreamStyle ? "on" : ""}" data-id="${st.id}">${esc(st.label)}</button>`).join("")}</div>
    <div class="row" style="margin-top:14px"><button id="mk" ${meta.available ? "" : "disabled"}>🎬 Film my dream</button>
      <span class="hint">About 5–8 minutes. Real people other than you aren't shown; reels are labelled AI-generated.</span></div>
    <div id="reels" class="reels"></div></div>`;
  host.querySelectorAll(".idea").forEach((b, i) => (b.onclick = () => ($("#dreamText", host).value = meta.ideas[i])));
  host.querySelectorAll("#dstyle .chip").forEach((b) => (b.onclick = () => {
    dreamStyle = b.dataset.id; host.querySelectorAll("#dstyle .chip").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#mk", host).onclick = async () => {
    const dream = $("#dreamText", host).value.trim();
    if (!dream) return toast("Describe your dream first");
    try { await api("/api/twin/dreams", { method: "POST", json: { dream, style: dreamStyle } }); $("#dreamText", host).value = ""; loadReels(host); }
    catch (err) { toast(err.message, 6000); }
  };
  loadReels(host);
}
async function loadReels(host) {
  clearTimeout(loadReels._t);
  if (!host.isConnected) return;
  const reels = await api("/api/twin/dreams").catch(() => []);
  const box = $("#reels", host);
  box.innerHTML = reels.map((r) => `<div class="reel">
    ${r.status === "ready" ? `<video src="${r.video_url}" controls playsinline preload="metadata"></video>`
      : r.status === "rendering" ? `<div class="reel-wait"><div class="spinner"></div><b>Filming your dream…</b><span class="hint">${Math.max(0, Math.round((Date.now() / 1000 - r.created) / 60))} min so far · about 5–8 min</span></div>`
      : `<div class="reel-wait"><b class="bad">Couldn't film this one</b><span class="hint">${esc(r.error || "")}</span></div>`}
    <div class="reel-meta"><b>${esc(r.title || "Your dream")}</b><p class="hint">${esc(r.narration || r.prompt)}</p>
      <div class="row">${r.video_url ? `<a class="btn" href="${r.video_url}" download="dream-reel.mp4">⬇ Download</a>` : ""}
      <button class="ghost" data-del="${r.id}">Delete</button></div></div></div>`).join("");
  box.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Delete this reel?")) return;
    await api(`/api/twin/dreams/${b.dataset.del}`, { method: "DELETE" }); loadReels(host);
  }));
  if (reels.some((r) => r.status === "rendering")) loadReels._t = setTimeout(() => loadReels(host), 8000);
}

/* ---------------- public chat page ---------------- */
async function renderChatPage(slug) {
  try {
    const t = await api(`/api/twins/${slug}`);
    view.innerHTML = `<div class="grid2"><div></div><div id="chatHost"></div></div>`;
    mountChat($("#chatHost"), slug, { isOwner: t.is_owner, twin: t, standalone: true });
  } catch (err) { view.innerHTML = `<div class="card auth"><h2>${esc(err.message)}</h2><p><a href="/">Make your own twin →</a></p></div>`; }
}

/* ---------------- chat + live voice ---------------- */
function mountChat(host, slug, { isOwner, twin, standalone }) {
  const conv = (isOwner ? "owner-" : "v-") + (localStorage["conv_" + slug] ||= Math.random().toString(36).slice(2, 12));
  const avatarHtml = `<div class="avatar" id="stage"><img id="face" src="${twin.headshot_url || ""}" alt=""><div class="ring" id="ring"></div><span class="badge" id="badge">${esc(twin.display_name)}</span></div>`;
  if (standalone) host.previousElementSibling.innerHTML = `<div class="card">${avatarHtml}<p class="hint" style="margin-top:12px">You're talking to ${esc(twin.display_name)}'s AI twin. It speaks as them but is not them, and it can't make commitments for them.</p></div>`;
  host.innerHTML = `<div class="card chat">
    ${standalone ? "" : `<div style="width:160px;margin:0 auto 10px">${avatarHtml}</div>`}
    <div class="msgs" id="msgs"><div class="status" style="text-align:center">Say hi to ${esc(twin.display_name)} 👋</div></div>
    <div class="composer"><button class="mic" id="mic" title="Talk">🎙</button><input id="inp" placeholder="Message ${esc(twin.display_name)}…"><button id="send">Send</button></div>
    <div class="toggles">
      <label><input type="checkbox" id="tVoice" checked> Reply in voice</label>
      <label><input type="checkbox" id="tVideo" ${twin.lipsync_available ? "checked" : "disabled"}> Lip-sync video${twin.lipsync_available ? "" : " (unavailable)"}</label>
      <label><input type="checkbox" id="tWait"> Wait for lip-sync before playing</label>
      <label><input type="checkbox" id="tLive"> Hands-free conversation</label>
    </div></div>`;
  const msgs = $("#msgs", host);
  fetch("/api/warm", { method: "POST" }).catch(() => {}); // boot the GPU while the person types
  let lastUser = "";

  const addMsg = (role, text, extra = {}) => {
    if (msgs.firstElementChild?.classList.contains("status")) msgs.innerHTML = "";
    const d = document.createElement("div"); d.className = `msg ${role}`; d.textContent = text;
    if (role === "assistant" && !extra.pending) {
      const meta = document.createElement("div"); meta.className = "meta"; d.appendChild(meta);
      if (extra.audio_url) { const b = document.createElement("button"); b.className = "fix"; b.textContent = "▶ replay"; b.onclick = () => play(extra.audio_url); meta.appendChild(b); }
      if (extra.video_status) { const s = document.createElement("span"); s.className = "vstat"; s.textContent = "🎬 rendering lip-sync…"; meta.appendChild(s); }
      if (isOwner) {
        const prompt = lastUser; const b = document.createElement("button"); b.className = "fix"; b.textContent = "✎ not how I'd say it";
        b.onclick = async () => {
          const fix = window.prompt(`How would YOU answer "${prompt}"?`, text); if (!fix || fix === text) return;
          await api("/api/twin/memories", { method: "POST", json: { kind: "correction", prompt, content: fix } });
          toast("Got it. Your twin will answer more like that."); if ($("#mems")) loadMems();
        };
        meta.appendChild(b);
      }
    }
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight; return d;
  };

  // --- avatar animation driven by the voice's loudness ---
  const face = () => $("#face"), ring = () => $("#ring");
  let actx, playing = null;
  function play(url) {
    return new Promise((resolve) => {
      playing?.pause();
      const a = new Audio(url); playing = a; a.crossOrigin = "anonymous";
      actx ||= new AudioContext();
      const src = actx.createMediaElementSource(a), an = actx.createAnalyser(); an.fftSize = 512;
      src.connect(an); an.connect(actx.destination);
      const buf = new Uint8Array(an.fftSize);
      const loop = () => {
        if (a.paused || a.ended) { if (face()) face().style.transform = ""; if (ring()) ring().style.boxShadow = ""; return; }
        an.getByteTimeDomainData(buf); let s = 0; for (const v of buf) s += (v - 128) ** 2;
        const lvl = Math.min(1, Math.sqrt(s / buf.length) / 40);
        if (face()) face().style.transform = `scale(${1 + lvl * 0.025}) translateY(${-lvl * 3}px)`;
        if (ring()) ring().style.boxShadow = `inset 0 0 0 ${2 + lvl * 6}px rgba(124,156,255,${0.3 + lvl * 0.6})`;
        requestAnimationFrame(loop);
      };
      a.onended = a.onerror = () => resolve();
      a.play().then(loop).catch(() => resolve());
    });
  }
  function playVideo(url) {
    return new Promise((resolve) => {
      const stage = $("#stage"); if (!stage) return resolve();
      playing?.pause();
      const v = document.createElement("video"); v.src = url; v.playsInline = true; v.autoplay = true;
      const img = face(); img.style.display = "none"; stage.insertBefore(v, img);
      const done = () => { v.remove(); img.style.display = ""; resolve(); };
      v.onended = v.onerror = done; v.play().catch(done);
    });
  }
  async function pollVideo(id, bubble) {
    for (let i = 0; i < 360; i++) {
      const r = await api(`/api/messages/${id}/video`).catch(() => null);
      const st = bubble.querySelector(".vstat");
      if (r?.status === "ready") {
        if (st) st.remove();
        const v = document.createElement("video"); v.src = r.url; v.controls = true; v.playsInline = true; bubble.appendChild(v);
        return r.url;
      }
      if (r?.status === "failed") { if (st) st.textContent = "lip-sync failed"; return null; }
      if (st) st.textContent = `🎬 rendering lip-sync… ${i * 5}s`;
      await new Promise((s) => setTimeout(s, 5000));
    }
  }

  async function handle(resPromise, userText) {
    const thinking = addMsg("assistant", "…", { pending: true });
    try {
      const r = await resPromise;
      thinking.remove();
      if (r.heard) addMsg("user", r.heard);
      lastUser = r.heard || userText;
      const bubble = addMsg("assistant", r.reply, r);
      if (r.video_status && $("#tWait", host).checked) {
        const url = await pollVideo(r.message_id, bubble);
        if (url) await playVideo(url); else if (r.audio_url) await play(r.audio_url);
      } else {
        if (r.audio_url) await play(r.audio_url);
        if (r.video_status) pollVideo(r.message_id, bubble);
      }
    } catch (err) { thinking.remove(); toast(err.message); }
    if ($("#tLive", host).checked) listen();
  }

  const sendText = () => {
    const text = $("#inp", host).value.trim(); if (!text) return;
    $("#inp", host).value = ""; addMsg("user", text);
    handle(api(`/api/twins/${slug}/chat`, { method: "POST", json: { text, conversation: conv, voice: $("#tVoice", host).checked, video: $("#tVideo", host).checked } }), text);
  };
  $("#send", host).onclick = sendText;
  $("#inp", host).onkeydown = (e) => e.key === "Enter" && sendText();

  // --- voice input with automatic end-of-speech detection ---
  let rec = null;
  async function listen() {
    if (rec) return;
    rec = new Recorder(); const mic = $("#mic", host);
    let spoke = false, quietSince = Date.now();
    const started = Date.now();
    try {
      await rec.start((lvl) => {
        if (lvl > 0.04) { spoke = true; quietSince = Date.now(); }
        if ((spoke && Date.now() - quietSince > 1300) || Date.now() - started > 30000) stopListening();
      });
    } catch { toast("Microphone permission is needed"); rec = null; return; }
    mic.classList.add("live"); mic.textContent = "■";
    async function stopListening() {
      if (!rec) return; const r = rec; rec = null;
      mic.classList.remove("live"); mic.textContent = "🎙";
      const blob = await r.stop();
      if (!spoke) { if ($("#tLive", host).checked) toast("Didn't hear anything. Hands-free paused."); $("#tLive", host).checked = false; return; }
      const fd = new FormData(); fd.append("file", blob, "talk.webm"); fd.append("conversation", conv); fd.append("video", $("#tVideo", host).checked);
      handle(api(`/api/twins/${slug}/talk`, { method: "POST", body: fd }), "");
    }
    listen.stop = stopListening;
  }
  $("#mic", host).onclick = () => (rec ? listen.stop() : listen());
  $("#tLive", host).onchange = (e) => e.target.checked && !rec && listen();
}

route();
