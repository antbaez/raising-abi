// Flow: (take photo | pick saved faces) -> check faces -> names + gender -> baby portrait
// -> then for each pair of ages: a minute of baby monitor readings (live from the Arduino toy), with a
// parenting challenge to answer, picks a good or bad path -> caption + image for both ages.

const $ = (id) => document.getElementById(id);

const video = $("video");
const canvas = $("canvas");
const views = {
  live: $("view-live"),
  saved: $("view-saved"),
  check: $("view-check"),
  confirm: $("view-confirm"),
  baby: $("view-baby"),
  sensors: $("view-sensors"),
  choices: $("view-choices"),
  result: $("view-result"),
  keepsake: $("view-keepsake"),
};
const btn = {
  shutter: $("btn-shutter"),
  back: $("btn-back"),
  facesOk: $("btn-faces-ok"),
  detailsBack: $("btn-details-back"),
  confirm: $("btn-confirm"),
  rebaby: $("btn-rebaby"),
  monitor: $("btn-monitor"),
  grow: $("btn-grow"),
  restart: $("btn-restart"),
  next: $("btn-next"),
  keepsake: $("btn-keepsake"),
};
const tabs = { camera: $("tab-camera"), saved: $("tab-saved") };

// Developer mode adds a bar at the top for jumping to any page.
const DEV_MODE = true;

const COUNTDOWN_SECONDS = 3;
// Story mode: a new age's caption types out in the middle of the frame, with
// the progress bar below it. Once the image is ready the caption slides down to
// its place and the image appears.
const TYPE_MS_PER_LETTER = 38;
const INTRO_MOVE_MS = 1600;

const state = {
  mode: "camera",   // "camera" | "saved"
  view: "live",
  step: 1,          // 1 capture, 2 confirm, 3 meet baby, 4 choose path, 5 grow up
  session: null,    // { id, photo, faces: [a, b], details }
  ages: [1, 4, 8, 12, 16, 18],
  details: { names: { a: "", b: "", child: "" }, gender: "any" },
  baby: { run: null, key: null, url: null, status: null }, // baby portrait
  monitors: null,   // { baby, stages: { stage -> { level, samples, polls, start, done, outcome, situation, reply } } }
  choices: {},      // age -> "good" | "bad"
  stage: 0,         // index of the age pair whose path is being picked
  run: null,        // id for one generation of all ages
  tokens: {},       // age -> { choice } of its latest request; older replies are ignored
  results: {},      // age -> { url, caption, choice }
  captions: {},     // age -> caption, known before its image is ready
  intro: null,      // { age, key } while a caption plays its intro
  status: {},       // age -> "loading" | "done" | "error"
  shown: 0,         // index into ages currently displayed
  reached: 0,       // furthest age index seen; later ages stay locked
};

// --- UI helpers -------------------------------------------------------------

function show(view, { step, buttons = [] }) {
  state.view = view;
  state.step = step;
  for (const [name, el] of Object.entries(views)) el.classList.toggle("hidden", name !== view);
  for (const [name, el] of Object.entries(btn)) el.classList.toggle("hidden", !buttons.includes(name));
  // The camera / saved faces tabs only matter while picking faces.
  $("card-head").classList.toggle("hidden", view !== "live" && view !== "saved");
  renderDevBar();
}

function busy(text) {
  $("busy").classList.toggle("hidden", !text);
  if (text) restartAnimation($("busy-progress"), "go");
  $("busy-text").textContent = text || "";
}

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 6000);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Only touch an <img> when its picture actually changes. Re-assigning the same
// src makes the browser reload it, which can cause a visible flicker.
function setImage(img, url) {
  if (!url) img.removeAttribute("src");
  else if (img.getAttribute("src") !== url) img.src = url;
}

function restartAnimation(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth; // force reflow so the animation replays
  el.classList.add(cls);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function sessionDate(id) {
  // booth_YYYYMMDD_HHMMSS_mmm -> readable local date
  const m = id.match(/booth_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
  if (!m) return id;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const childName = () => state.details.names.child;
const stageName = (age) => (age <= 1 ? "Baby" : age < 13 ? "Child" : age < 18 ? "Teen" : "Adult");

// Ages are picked in pairs: one good/bad choice covers both ages of a pair.
const AGES_PER_PICK = 2;
const stageOf = (index) => Math.floor(index / AGES_PER_PICK);
const stageAges = (stage) => state.ages.slice(stage * AGES_PER_PICK, (stage + 1) * AGES_PER_PICK);

// --- Camera -----------------------------------------------------------------

async function startCamera() {
  try {
    video.srcObject = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch (e) {
    toast(`Camera unavailable: ${e.message}. You can still use saved faces.`);
  }
}

function grabFrame() {
  // Draw mirrored so the saved photo matches what people saw in the preview.
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const ctx = canvas.getContext("2d");
  ctx.translate(canvas.width, 0);
  ctx.scale(-1, 1);
  ctx.drawImage(video, 0, 0);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.95));
}

async function countdown() {
  const number = $("countdown");
  $("hint").classList.add("hidden");
  for (let i = COUNTDOWN_SECONDS; i > 0; i--) {
    number.textContent = i;
    restartAnimation(number, "tick");
    await sleep(1000);
  }
  number.textContent = "";
  $("hint").classList.remove("hidden");
}

// --- Step 1: capture or pick saved faces ------------------------------------

function showLive() {
  setTabs("camera");
  show("live", {
    step: 1,
    buttons: ["shutter"],
  });
}

async function deleteSession(session, card, couple) {
  const label = couple || sessionDate(session.id);
  if (!confirm(`Delete the saved faces for ${label}? This can't be undone.`)) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    card.remove();
    $("saved-empty").classList.toggle("hidden", $("saved-grid").children.length > 0);
  } catch (e) {
    toast(e.message);
  }
}

async function showSaved() {
  setTabs("saved");
  show("saved", { step: 1 });
  const grid = $("saved-grid");
  grid.replaceChildren();
  try {
    const { sessions } = await api("/api/sessions");
    $("saved-empty").classList.toggle("hidden", sessions.length > 0);
    for (const s of sessions) {
      const card = document.createElement("div");
      card.className = "pair";
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.innerHTML = `
        <button type="button" class="pair-delete" aria-label="Delete this pair" title="Delete">×</button>
        <span class="pair-faces"><img alt="Person A"><img alt="Person B"></span><span class="pair-date"></span>`;
      const [a, b] = card.querySelectorAll("img");
      a.src = s.faces[0];
      b.src = s.faces[1];
      const names = s.details?.names;
      const couple = [names?.a, names?.b].filter(Boolean).join(" + ");
      card.querySelector(".pair-date").textContent = couple ? `${couple} · ${sessionDate(s.id)}` : sessionDate(s.id);
      card.addEventListener("click", () => selectSession(s));
      card.addEventListener("keydown", (e) => {
        if (e.target === card && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          selectSession(s);
        }
      });
      card.querySelector(".pair-delete").addEventListener("click", (e) => {
        e.stopPropagation(); // don't also select the pair
        deleteSession(s, card, couple);
      });
      grid.append(card);
    }
  } catch (e) {
    toast(e.message);
  }
}

function setTabs(mode) {
  state.mode = mode;
  tabs.camera.classList.toggle("active", mode === "camera");
  tabs.saved.classList.toggle("active", mode === "saved");
}

function lockTabs(locked) {
  tabs.camera.disabled = tabs.saved.disabled = locked;
}

async function takePhoto() {
  if (state.view !== "live" || btn.shutter.disabled) return;
  if (!video.videoWidth) return toast("Camera isn't ready yet.");
  btn.shutter.disabled = true;
  lockTabs(true);
  try {
    await countdown();
    restartAnimation($("flash"), "go");
    const blob = await grabFrame();
    busy("Finding your faces…");
    const form = new FormData();
    form.append("photo", blob, "photo.jpg");
    selectSession(await api("/api/capture", { method: "POST", body: form }));
  } catch (e) {
    toast(`${e.message}. Make sure both faces are in view and try again.`);
  } finally {
    busy(null);
    btn.shutter.disabled = false;
    lockTabs(false);
  }
}

function selectSession(session) {
  state.session = session;
  state.details = session.details || { names: { a: "", b: "", child: "" }, gender: "any" };
  state.choices = {};
  state.run = null;
  state.baby = { run: null, key: null, url: null, status: null };
  showFaceCheck();
}

// --- Step 2: check the captured faces, then add names and gender ------------

function showFaceCheck() {
  const { session } = state;
  const backdrop = $("captured-check");
  if (session.photo) backdrop.src = session.photo;
  else backdrop.removeAttribute("src");
  backdrop.classList.toggle("hidden", !session.photo);
  $("check-a").src = session.faces[0];
  $("check-b").src = session.faces[1];
  btn.back.textContent = state.mode === "camera" ? "Retake" : "Choose another";
  show("check", { step: 2, buttons: ["back", "facesOk"] });
}

function showConfirm() {
  const { session, details } = state;
  $("face-a").src = session.faces[0];
  $("face-b").src = session.faces[1];
  $("name-a").value = details.names.a || "";
  $("name-b").value = details.names.b || "";
  $("name-child").value = details.names.child || "";
  for (const radio of document.querySelectorAll('input[name="gender"]')) radio.checked = radio.value === details.gender;
  resetGenderKnob();
  show("confirm", { step: 2, buttons: ["detailsBack", "confirm"] });
}

// Puts the gender knob back on the picked word (after it was dragged elsewhere).
function resetGenderKnob() {
  document.querySelector(".choice .knob").style.left = "";
}

function readDetails() {
  return {
    names: {
      a: $("name-a").value.trim(),
      b: $("name-b").value.trim(),
      child: $("name-child").value.trim(),
    },
    gender: document.querySelector('input[name="gender"]:checked')?.value || "",
  };
}

async function surpriseMe() {
  const button = $("btn-surprise");
  button.disabled = true;
  button.classList.add("loading");
  try {
    const { name, gender } = await api("/api/surprise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: { a: $("name-a").value.trim(), b: $("name-b").value.trim() } }),
    });
    $("name-child").value = name;
    for (const radio of document.querySelectorAll('input[name="gender"]')) radio.checked = radio.value === gender;
    resetGenderKnob();
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
}

function confirmDetails() {
  if (state.view !== "confirm") return;
  const details = readDetails();
  const missing = [
    [details.names.a, "name-a"],
    [details.names.b, "name-b"],
    [details.names.child, "name-child"],
  ].find(([value]) => !value);
  if (missing) {
    $(missing[1]).focus();
    return toast("Add both of your names and a name for your child, or press Surprise me.");
  }
  if (!details.gender) return toast("Pick Girl, Either or Boy, or press Surprise me.");
  state.details = details;
  state.session.details = details;
  showBaby();
}

// --- Step 3: meet the baby --------------------------------------------------

function showBaby() {
  const key = JSON.stringify([state.session.id, state.details]);
  // Coming back without changing the names or gender keeps the same baby.
  if (state.baby.key !== key || state.baby.status === "error") generateBaby(key);
  show("baby", { step: 3, buttons: ["rebaby", "monitor"] });
  renderBaby();
}

async function generateBaby(key = state.baby.key) {
  const run = String(Date.now());
  state.baby = { run, key, url: null, status: "loading" };
  state.choices = {};
  state.run = null;
  renderBaby();
  restartAnimation($("baby-progress"), "go");
  try {
    const { url, gender } = await api("/api/baby", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: state.session.id, run, ...state.details }),
    });
    if (run !== state.baby.run) return; // replaced meanwhile
    state.baby.url = url;
    state.baby.gender = gender; // "Either" is settled as girl or boy for this baby
    state.baby.status = "done";
  } catch (e) {
    if (run !== state.baby.run) return;
    state.baby.status = "error";
    toast(e.message);
  }
  renderBaby();
}

// While the baby is generated, a new word pops up every few seconds, Claude
// Code style: a mix of pregnancy and AI training.
const BABY_WORDS = [
  "Gestating", "Incubating", "Backpropagating", "Nesting", "Fine-tuning", "Kicking",
  "Tokenizing", "Swaddling", "Gradient descending", "Trimestering", "Embedding",
  "Craving pickles", "Overfitting", "Cooing", "Pretraining", "Folding chromosomes",
  "Sampling", "Burping", "Converging", "Epoching", "Cradling", "Distilling",
  "Teething", "Attention-heading", "Conceiving", "Regularizing", "Lullabying",
  "Inferencing", "Expecting", "Denoising", "Bonding", "Tuning hyperparameters",
];
const BABY_WORD_MS = 3000;
let babyWordTimer = null;

function showBabyWord() {
  if (state.view !== "baby" || state.baby.status !== "loading") return stopBabyWords();
  const el = $("baby-generating-text");
  const current = el.textContent.replace("…", "");
  const options = BABY_WORDS.filter((w) => w !== current);
  el.textContent = `${options[Math.floor(Math.random() * options.length)]}…`;
  restartAnimation(el, "word-pop");
}

function startBabyWords() {
  if (babyWordTimer) return;
  showBabyWord();
  babyWordTimer = setInterval(showBabyWord, BABY_WORD_MS);
}

function stopBabyWords() {
  clearInterval(babyWordTimer);
  babyWordTimer = null;
}

function renderBaby() {
  if (state.view !== "baby") return;
  const { status, url } = state.baby;
  const img = $("baby-img");
  setImage(img, status === "done" ? url : null);
  $("baby-title").textContent = status === "done" ? `Meet ${childName()}!` : `Meeting ${childName()}…`;
  $("baby-generating").classList.toggle("hidden", status === "done");
  if (status === "loading") startBabyWords();
  else stopBabyWords();
  if (status === "error") $("baby-generating-text").textContent = "Something went wrong. Try another baby.";
  $("baby-progress").classList.toggle("hidden", status === "error");
  btn.monitor.disabled = status !== "done";
  btn.rebaby.disabled = status === "loading";
}

// --- Baby monitor ------------------------------------------------------------

// Before each pair of ages the parents watch the baby monitor for a minute.
// Normal mode shows the Arduino toy's live readings (ABI_Face_Feedback.ino),
// streamed from /api/arduino. The toy decides, with its own thresholds and
// hysteresis, whether each one is good: the IR temperature between COLD_ON_C
// and HOT_ON_C (temp_ok), and the motion a gentle rock, neither still nor
// shaken hard (rocked). Developer mode instead moves both readings with the up
// / down arrow keys, good above the middle of the chart.
// The baby is happy while both readings are good and crying otherwise. Every
// second the baby is polled; if happy seconds >= crying seconds, the path for
// that pair of ages is good.
const SENSOR_SECONDS = 60;
const SENSOR_WINDOW_SECONDS = 20; // how much history each chart shows
const SENSOR_RATE = 0.5;          // developer mode: level change per second while a key is held
const SENSOR_STALE_MS = 1000;     // a live reading older than this counts as missing
const SENSOR_GOOD = "#4ade80";
const SENSOR_BAD = "#f87171";
// Chart ranges. `lines` are drawn for reference: in normal mode the sketch's
// COLD_ON_C / HOT_ON_C and HAPPY_ACCEL_ON / CRY_ACCEL. The motion chart plots
// the toy's dyn (acceleration beyond gravity); a gentle spin counts as rocking
// too, so its color can be green below the line.
const SENSORS = DEV_MODE
  ? {
      temp: { range: [35.5, 39.5], lines: [37.5], format: (v) => `${v.toFixed(1)}°C` },
      motion: { range: [0, 100], lines: [50], format: (v) => `${Math.round(v)}%` },
    }
  : {
      temp: { range: [10, 42], lines: [18, 34], format: (v) => `${v.toFixed(1)}°C` },
      motion: { range: [0, 8], lines: [1.2, 5], format: (v) => `${v.toFixed(1)} m/s²` },
    };
const sensorKeys = { up: false, down: false };
let sensorFrame = null;

const sensorValue = (name, level) => {
  const [lo, hi] = SENSORS[name].range;
  return lo + level * (hi - lo);
};

// The latest message from the toy; the page listens for the whole visit.
const arduino = { connected: false, reading: null, at: 0 };
if (!DEV_MODE) {
  const source = new EventSource("/api/arduino");
  source.onmessage = (e) => {
    const message = JSON.parse(e.data);
    arduino.connected = message.connected;
    if (message.reading) {
      arduino.reading = message.reading;
      arduino.at = performance.now();
    }
  };
  source.onerror = () => (arduino.connected = false); // EventSource reconnects by itself
}

// A reading is { temp, motion, tempOk, motionOk }, or null when the toy isn't sending.
function liveReading(now) {
  const r = arduino.reading;
  if (!arduino.connected || !r || now - arduino.at > SENSOR_STALE_MS) return null;
  return { temp: r.obj, motion: r.dyn, tempOk: r.temp_ok, motionOk: r.rocked };
}
const devReading = (level) => ({
  temp: sensorValue("temp", level), motion: sensorValue("motion", level),
  tempOk: level >= 0.5, motionOk: level >= 0.5,
});
const currentReading = (m, now) => (DEV_MODE ? devReading(m.level) : liveReading(now));
const isHappy = (reading) => Boolean(reading?.tempOk && reading?.motionOk);

// While the minute runs, every SOUND_EVERY_MS the baby plays the first
// SOUND_CLIP_MS of a laugh if happy right then, or of a cry otherwise.
const SOUND_EVERY_MS = 10000;
const SOUND_CLIP_MS = 3000;
const sounds = {
  happy: new Audio("/static/sounds/baby-laughing.mp3"),
  crying: new Audio("/static/sounds/baby-crying.mp3"),
};
let soundStop;

function stopSounds() {
  clearTimeout(soundStop);
  for (const sound of Object.values(sounds)) sound.pause();
}

function playMoodSound(m, now) {
  if (m.start === null || m.done) return;
  if (m.soundAt !== undefined && now - m.soundAt < SOUND_EVERY_MS) return;
  m.soundAt = now;
  stopSounds();
  const sound = sounds[isHappy(currentReading(m, now)) ? "happy" : "crying"];
  sound.currentTime = 0;
  sound.play().catch(() => {}); // the browser blocks sound until the page has been clicked
  soundStop = setTimeout(stopSounds, SOUND_CLIP_MS);
}

// Sets up a run of age images, if there isn't one yet.
function ensureRun() {
  if (state.run) return;
  state.run = String(Date.now());
  state.results = {};
  state.captions = {};
  state.status = {};
  state.tokens = {};
  state.shown = 0;
  state.reached = 0;
}

function showSensors(stage = 0) {
  if (state.baby.status !== "done" && !DEV_MODE) return;
  ensureRun();
  state.stage = stage;
  // Readings belong to one baby; coming back to a finished minute shows it again.
  if (state.monitors?.baby !== state.baby.run) state.monitors = { baby: state.baby.run, stages: {} };
  if (!state.monitors.stages[stage]) {
    state.monitors.stages[stage] = { level: 0.5, samples: [], polls: [], start: null, done: false, reply: "" };
  }
  showSituation(state.monitors.stages[stage], stageAges(stage)[0]);
  const ages = stageAges(stage);
  $("sensors-title").textContent = `${childName() ? `${childName()}'s` : "Baby"} monitor · Ages ${ages.join(" & ")}`;
  show("sensors", { step: 4, buttons: ["grow"] });
  cancelAnimationFrame(sensorFrame);
  let last = performance.now();
  const tick = (now) => {
    if (state.view !== "sensors") return stopSounds(); // stops when leaving the page
    stepSensors(now, (now - last) / 1000);
    last = now;
    playMoodSound(monitor(), now);
    renderSensors(now);
    sensorFrame = requestAnimationFrame(tick);
  };
  sensorFrame = requestAnimationFrame(tick);
}

const monitor = () => state.monitors.stages[state.stage];

// Parenting challenges by age, from situations.json (re-read on each visit, so
// edits show up on the next monitor). An age without its own list uses the
// closest younger age's.
let situations = {};
async function loadSituations() {
  try {
    situations = await api(`/static/situations.json?v=${Date.now()}`);
  } catch {
    situations = {};
  }
}

function situationFor(age) {
  const key = Object.keys(situations).map(Number).filter((a) => a <= age).sort((a, b) => b - a)[0];
  const list = situations[key] || [];
  return list.length ? list[Math.floor(Math.random() * list.length)] : "";
}

async function showSituation(m, age) {
  if (!m.situation) {
    await loadSituations();
    m.situation = situationFor(age);
  }
  if (monitor() !== m) return; // moved on meanwhile
  $("situation-text").textContent = m.situation.replaceAll("{child}", childName() || "your child");
  const reply = $("situation-reply");
  reply.value = m.reply;
  reply.disabled = m.done || m.submitted;
}

function stepSensors(now, dt) {
  const m = monitor();
  if (m.done) return;
  const dir = (sensorKeys.up ? 1 : 0) - (sensorKeys.down ? 1 : 0);
  if (m.start === null) {
    // The minute starts with the first key press, or in normal mode the first live reading.
    if (DEV_MODE ? !dir : !liveReading(now)) return;
    m.start = now;
  }
  m.level = Math.min(1, Math.max(0, m.level + dir * SENSOR_RATE * dt));
  const t = Math.min(SENSOR_SECONDS, (now - m.start) / 1000);
  const reading = currentReading(m, now);
  m.samples.push({ t, ...reading });
  // Poll once per elapsed second; a missing reading counts as crying.
  while (m.polls.length < Math.floor(t)) m.polls.push(isHappy(reading));
  if (t >= SENSOR_SECONDS) finishMonitor(m);
}

// Happy seconds, after the response bonus.
const JUDGE_BONUS_SECONDS = 10;
const goodSeconds = (m) => m.polls.filter(Boolean).length + (m.bonus || 0);
const badSeconds = (m) => m.polls.filter((p) => !p).length;

// Enter locks the response and sends it to be judged straight away; no
// changes after that.
function lockReply(m) {
  if (m.judgement) return;
  $("situation-reply").disabled = true;
  m.submitted = true;
  m.judgement = api("/api/judge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      situation: $("situation-text").textContent, reply: m.reply, age: stageAges(state.stage)[0],
      names: state.details.names,
    }),
  }).then(
    ({ verdict }) => {
      m.verdict = verdict;
      m.bonus = verdict === "good" ? JUDGE_BONUS_SECONDS : -JUDGE_BONUS_SECONDS;
    },
    (e) => {
      m.bonus = 0; // decide on the monitor alone
      toast(e.message);
    }
  );
}

// When the minute is up, the submitted response's verdict counts: a good one
// adds 10 good seconds, a bad one takes 10 away, and no submission changes
// nothing. Then good seconds >= bad seconds picks the good path.
async function finishMonitor(m) {
  m.done = true;
  m.judging = true;
  const ages = stageAges(state.stage);
  // A response not submitted in time gets no bonus or penalty.
  $("situation-reply").disabled = true;
  if (m.judgement) await m.judgement;
  else m.bonus = 0;
  m.judging = false;
  m.outcome = goodSeconds(m) >= badSeconds(m) ? "good" : "bad";
  for (const age of ages) state.choices[age] = m.outcome;
}

function renderSensors(now) {
  const m = monitor();
  const elapsed = m.start === null ? 0 : Math.min(SENSOR_SECONDS, (now - m.start) / 1000);
  const t = m.done ? SENSOR_SECONDS : elapsed;
  const left = Math.ceil(SENSOR_SECONDS - t);
  const timer = $("sensor-timer");
  timer.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  timer.classList.toggle("done", m.done);

  const tally = `${goodSeconds(m)}s happy · ${badSeconds(m)}s crying`;
  const bonus = m.verdict
    ? ` (${m.verdict === "good" ? "+" : "−"}${JUDGE_BONUS_SECONDS}s for a ${m.verdict} response)`
    : m.submitted ? "" : " (no response submitted)";
  const hint = $("sensor-hint");
  hint.textContent = m.judging
    ? "Judging your response…"
    : m.outcome
      ? `${m.outcome === "good" ? "Good" : "Bad"} parenting: ${tally}${bonus}`
      : sensorPrompt(m, now, tally);
  hint.className = `sensor-hint${m.outcome ? ` result ${m.outcome}` : ""}`;
  // Developer mode can press Grow up early to end the minute.
  btn.grow.disabled = m.judging || (!m.outcome && !DEV_MODE);

  // A finished minute keeps showing its last reading.
  const reading = m.done ? m.samples[m.samples.length - 1] : currentReading(m, now);
  for (const name of Object.keys(SENSORS)) {
    const v = reading?.[name];
    const ok = reading?.[`${name}Ok`];
    const value = $(`${name}-value`);
    value.textContent = v == null ? "—" : SENSORS[name].format(v);
    value.style.color = reading ? (ok ? SENSOR_GOOD : SENSOR_BAD) : "";
    $(`panel-${name}`).classList.toggle("good", Boolean(reading) && ok);
    $(`panel-${name}`).classList.toggle("bad", Boolean(reading) && !ok);
    drawSensorChart($(`${name}-chart`), name, m.samples, t);
  }
  const mood = $("sensor-mood");
  mood.textContent = !reading ? "No signal" : isHappy(reading) ? "😊 Happy" : "😢 Crying";
  mood.className = `sensor-mood${reading ? (isHappy(reading) ? " good" : " bad") : ""}`;
}

function sensorPrompt(m, now, tally) {
  if (DEV_MODE) return m.start === null ? "Hold ↑ or ↓ to start the monitor" : `Hold ↑ or ↓ to change the readings · ${tally}`;
  if (!liveReading(now)) {
    const problem = arduino.connected ? "Waiting for the baby monitor…" : "Baby monitor not connected: plug in the toy";
    return m.start === null ? problem : `${problem} (counts as crying) · ${tally}`;
  }
  return `Rock the baby gently and keep them at a comfortable temperature · ${tally}`;
}

// Newest reading at the right edge; older ones scroll off to the left. Each
// stretch of line is green while that reading is good and red while it isn't.
function drawSensorChart(canvas, name, samples, now) {
  const { range, lines, format } = SENSORS[name];
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pad = { top: 12, bottom: 12, left: 64, right: 14 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const bottom = pad.top + plotH;
  const x = (t) => pad.left + plotW - ((now - t) / SENSOR_WINDOW_SECONDS) * plotW;
  const clamped = (v) => Math.min(range[1], Math.max(range[0], v));
  const y = (v) => bottom - ((clamped(v) - range[0]) / (range[1] - range[0])) * plotH;
  const hline = (v) => {
    ctx.beginPath();
    ctx.moveTo(pad.left, y(v));
    ctx.lineTo(w - pad.right, y(v));
    ctx.stroke();
  };

  // Grid and value labels, then the threshold lines.
  ctx.font = "12px Inter, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.strokeStyle = "rgba(255, 255, 255, .07)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = range[0] + (i / 4) * (range[1] - range[0]);
    hline(v);
    ctx.fillStyle = "rgba(255, 255, 255, .4)";
    ctx.fillText(format(v), pad.left - 10, y(v));
  }
  ctx.strokeStyle = "rgba(255, 255, 255, .35)";
  ctx.lineWidth = 1.5;
  for (const v of lines) hline(v);

  const visible = samples.filter((p) => p.t >= now - SENSOR_WINDOW_SECONDS - 1);
  const paths = { [SENSOR_GOOD]: [new Path2D(), new Path2D()], [SENSOR_BAD]: [new Path2D(), new Path2D()] };
  for (let i = 1; i < visible.length; i++) {
    const a = visible[i - 1];
    const b = visible[i];
    if (a[name] == null || b[name] == null) continue; // a gap while the toy wasn't sending
    const [line, area] = paths[b[`${name}Ok`] ? SENSOR_GOOD : SENSOR_BAD];
    const [x0, y0, x1, y1] = [x(a.t), y(a[name]), x(b.t), y(b[name])];
    line.moveTo(x0, y0);
    line.lineTo(x1, y1);
    area.moveTo(x0, bottom);
    area.lineTo(x0, y0);
    area.lineTo(x1, y1);
    area.lineTo(x1, bottom);
    area.closePath();
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(pad.left, 0, plotW, h);
  ctx.clip();
  for (const [color, [line, area]] of Object.entries(paths)) {
    ctx.globalAlpha = 0.15;
    ctx.fillStyle = color;
    ctx.fill(area);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.stroke(line);
  }
  ctx.restore();
  const last = visible[visible.length - 1];
  if (last?.[name] != null) {
    ctx.beginPath();
    ctx.arc(x(last.t), y(last[name]), 5, 0, Math.PI * 2);
    ctx.fillStyle = last[`${name}Ok`] ? SENSOR_GOOD : SENSOR_BAD;
    ctx.fill();
  }
}

document.addEventListener("keydown", (e) => {
  if (!DEV_MODE || state.view !== "sensors" || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
  e.preventDefault();
  sensorKeys[e.key === "ArrowUp" ? "up" : "down"] = true;
});
document.addEventListener("keyup", (e) => {
  if (e.key === "ArrowUp") sensorKeys.up = false;
  if (e.key === "ArrowDown") sensorKeys.down = false;
});
window.addEventListener("blur", () => (sensorKeys.up = sensorKeys.down = false));
$("situation-reply").addEventListener("input", (e) => (monitor().reply = e.target.value));
// Enter submits the response (Shift+Enter for a new line).
$("situation-reply").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  if (monitor().reply.trim()) lockReply(monitor());
});

// --- Step 4: good or bad path for the next pair of ages ----------------------

function showChoices(stage = 0) {
  if (state.baby.status !== "done" && !DEV_MODE) return;
  ensureRun();
  state.stage = stage;
  const ages = stageAges(stage);
  const card = document.createElement("div");
  card.className = "age-card";
  card.innerHTML = `
    <span class="age-label">Ages</span>
    <span class="age-num">${ages.join(" & ")}</span>
    <span class="age-stage">${[...new Set(ages.map(stageName))].join(" · ")}</span>
    <div class="path" role="radiogroup" aria-label="Ages ${ages.join(" and ")}">
      <button type="button" class="path-btn good" data-choice="good">Good</button>
      <button type="button" class="path-btn bad" data-choice="bad">Bad</button>
    </div>`;
  for (const b of card.querySelectorAll(".path-btn")) {
    b.addEventListener("click", () => {
      for (const age of ages) state.choices[age] = b.dataset.choice;
      renderChoices();
    });
  }
  $("choices-grid").replaceChildren(card);
  show("choices", { step: 4, buttons: ["grow"] });
  renderChoices();
}

function renderChoices() {
  const choice = state.choices[stageAges(state.stage)[0]];
  for (const b of $("choices-grid").querySelectorAll(".path-btn")) {
    const on = b.dataset.choice === choice;
    b.classList.toggle("selected", on);
    b.setAttribute("aria-pressed", on);
  }
  btn.grow.disabled = !choice;
}

// --- Step 5: grow up ----------------------------------------------------------

// Grows up the current pair of ages, on the path the baby monitor picked (or,
// in developer mode, the one picked by hand on the path page).
function startGrowing() {
  if ((state.view !== "sensors" && state.view !== "choices") || btn.grow.disabled) return;
  // Developer mode can end the minute early; Grow up again once it's judged.
  if (state.view === "sensors" && !monitor().done) return finishMonitor(monitor());
  const first = state.stage * AGES_PER_PICK;
  // Generate ages that haven't been made (or failed) with the picked path.
  const changed = stageAges(state.stage).filter(
    (age) => state.status[age] === "error" || state.tokens[age]?.choice !== state.choices[age]
  );
  if (changed.length) {
    // A new path for these ages makes everything after them out of date.
    for (const age of state.ages.slice(first + AGES_PER_PICK)) {
      delete state.choices[age];
      delete state.results[age];
      delete state.captions[age];
      delete state.status[age];
      delete state.tokens[age];
    }
    state.reached = Math.min(state.reached, first);
    changed.forEach((age) => generateAge(age));
  }
  state.shown = first;
  state.reached = Math.max(state.reached, first);
  showResult();
}

function showResult() {
  show("result", { step: 5, buttons: ["restart", "next", "keepsake"] });
  renderResult();
}

async function generateAge(age) {
  const run = state.run;
  const token = (state.tokens[age] = { choice: state.choices[age] });
  state.status[age] = "loading";
  restartAnimation($("progress"), "go");
  if (state.view === "result") renderResult();
  const request = { id: state.session.id, run, age, choice: state.choices[age], ...state.details };
  const post = (path, body) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  try {
    // The caption comes first so it can be read while the image is made.
    const { template, caption } = await post("/api/caption", request);
    if (token !== state.tokens[age]) return; // started over or changed path meanwhile
    state.captions[age] = caption;
    if (state.view === "result") renderResult();
    const data = await post("/api/generate", {
      ...request, baby_run: state.baby.run, template, gender: state.baby.gender || state.details.gender,
    });
    if (token !== state.tokens[age]) return;
    state.results[age] = data;
    state.status[age] = "done";
  } catch (e) {
    if (token !== state.tokens[age]) return;
    state.status[age] = "error";
    toast(`Age ${age}: ${e.message}`);
  }
  if (state.view === "result") renderResult();
}

function displayAge(index) {
  if (index > state.reached && !DEV_MODE) return; // ages are seen in order
  state.shown = index;
  const age = state.ages[index];
  // Clicking a failed age retries it.
  if (state.status[age] === "error") generateAge(age);
  renderResult();
}

// The next age starts a new pair whose path hasn't been picked yet.
const needsPick = () => !state.choices[state.ages[state.shown + 1]];

function nextAge() {
  if (state.view !== "result" || state.shown >= state.ages.length - 1) return;
  if (needsPick()) return showSensors(stageOf(state.shown + 1));
  state.reached = Math.max(state.reached, state.shown + 1);
  displayAge(state.shown + 1);
}

// --- Keepsake ----------------------------------------------------------------

// Everything in one image: the two faces, the baby, and every age with its
// caption. Phones on the same Wi-Fi save it by scanning the QR code. It's made
// once per set of images; picking a different path makes a new one.
async function showKeepsake() {
  show("keepsake", { step: 5, buttons: ["restart"] });
  const key = JSON.stringify([state.run, state.ages.map((a) => state.results[a]?.url)]);
  if (state.keepsake?.key !== key) {
    for (const id of ["keepsake-img", "keepsake-qr"]) setImage($(id), null);
    $("keepsake-link").textContent = "";
    $("keepsake-download").classList.add("hidden");
    busy("Putting your keepsake together…");
    try {
      const data = await api("/api/keepsake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: state.session?.id, run: state.run, baby_run: state.baby.run, names: state.details.names }),
      });
      state.keepsake = { key, ...data };
    } catch (e) {
      toast(e.message);
      return;
    } finally {
      busy(null);
    }
  }
  if (state.view !== "keepsake") return;
  const { url, qr, share } = state.keepsake;
  setImage($("keepsake-img"), url);
  setImage($("keepsake-qr"), qr);
  $("keepsake-link").textContent = share;
  $("keepsake-download").href = url;
  $("keepsake-download").classList.remove("hidden");
}

// Each age's story intro plays once per run.
const introsPlayed = new Set();
const introKey = (age) => `${state.run}:${age}:${state.choices[age]}`;

function captionStyle(css) {
  Object.assign($("caption").style, css);
}

async function playIntro(age, text) {
  const intro = (state.intro = { age, phase: "typing" });
  introsPlayed.add(introKey(age));
  renderResult();
  // Lay out the whole caption first (untyped letters are invisible), so it
  // doesn't move while typing, and centre it on the frame.
  const typed = document.createElement("span");
  const rest = document.createElement("span");
  rest.className = "untyped";
  rest.textContent = text;
  $("caption-text").replaceChildren(typed, rest);
  const frameEl = $("child-img").closest(".child-frame");
  const frame = frameEl.getBoundingClientRect();
  const box = $("caption").getBoundingClientRect();
  const dy = frame.top + frame.height * 0.42 - (box.top + box.height / 2);
  captionStyle({ transition: "none", transform: `translateY(${dy}px)` });
  // The progress bar sits just below the caption.
  frameEl.classList.add("story", "bare");
  $("progress").style.marginTop = `${Math.max(0, box.bottom + dy - frame.top + 24)}px`;

  for (let i = 1; i <= text.length; i++) {
    await sleep(TYPE_MS_PER_LETTER);
    if (state.intro !== intro) return;
    typed.textContent = text.slice(0, i);
    rest.textContent = text.slice(i);
  }
  intro.phase = "typed";
  renderResult(); // moves on now if the image is already done
}

async function finishIntro(intro) {
  intro.phase = "moving";
  $("child-img").closest(".child-frame").classList.remove("story");
  $("progress").style.marginTop = "";
  captionStyle({ transition: `transform ${INTRO_MOVE_MS}ms cubic-bezier(.45, 0, .2, 1)`, transform: "" });
  await sleep(INTRO_MOVE_MS);
  if (state.intro !== intro) return;
  endIntro();
  renderResult();
}

function endIntro() {
  state.intro = null;
  captionStyle({ transition: "", transform: "" });
  $("child-img").closest(".child-frame").classList.remove("story", "bare");
  $("progress").style.marginTop = "";
}

function renderResult() {
  btn.next.classList.toggle("hidden", state.shown >= state.ages.length - 1);
  btn.next.textContent = needsPick() ? "Monitor your child →" : "Next age →";
  // The keepsake is offered on the last age once every image is ready.
  const allDone = state.ages.every((a) => state.status[a] === "done");
  btn.keepsake.classList.toggle("hidden", state.shown < state.ages.length - 1 || !allDone);
  const age = state.ages[state.shown];
  const status = state.status[age];
  const result = state.results[age];
  const text = result?.caption || state.captions[age];
  if (state.intro && (state.intro.age !== age || state.view !== "result")) endIntro();
  if (!state.intro && text && state.view === "result" && !introsPlayed.has(introKey(age))) {
    return playIntro(age, text);
  }
  const intro = state.intro?.age === age ? state.intro : null;
  if (intro?.phase === "typed" && (status === "done" || status === "error")) finishIntro(intro);

  const img = $("child-img");
  setImage(img, status === "done" && !intro ? result.url : null);
  // While the story types out, only the progress bar shows in the frame.
  const typing = intro && intro.phase !== "moving";
  $("generating").classList.toggle("hidden", (status === "done" && !typing) || intro?.phase === "moving");
  $("generating-text").textContent =
    status === "error"
      ? `Age ${age} failed. Click it below to retry.`
      : `Imagining ${childName() || "your child"} at age ${age}…`;
  $("progress").classList.toggle("hidden", status === "error" || (status === "done" && typing));

  if (!intro) $("caption-text").textContent = text || "Writing this moment…";
  $("caption").classList.toggle("pending", !text);

  const list = $("timeline");
  list.style.setProperty("--fill", state.reached / Math.max(1, state.ages.length - 1));
  list.replaceChildren();
  state.ages.forEach((a, i) => {
    const li = document.createElement("li");
    const st = state.status[a];
    li.classList.toggle("done", st === "done");
    li.classList.toggle("loading", st === "loading");
    li.classList.toggle("error", st === "error");
    li.classList.toggle("current", i === state.shown);
    li.classList.toggle("seen", i <= state.reached);
    const b = document.createElement("button");
    b.disabled = i > state.reached && !DEV_MODE;
    b.title = st === "error" ? `Retry age ${a}` : `Age ${a}`;
    b.textContent = st === "error" ? `${a} ↻` : a;
    b.addEventListener("click", () => displayAge(i));
    li.append(b);
    list.append(li);
  });
}

// --- Navigation -------------------------------------------------------------

function restart() {
  state.session = null;
  state.run = null;
  state.baby = { run: null, key: null, url: null, status: null };
  state.mode === "saved" ? showSaved() : showLive();
}

// --- Developer mode ---------------------------------------------------------

// Pages that need a pair of faces use the newest saved pair if none is picked.
async function ensureSession() {
  if (state.session) return true;
  try {
    const { sessions } = await api("/api/sessions");
    if (!sessions.length) {
      toast("No saved faces yet. Take a photo first.");
      return false;
    }
    selectSession(sessions[0]);
    return true;
  } catch (e) {
    toast(e.message);
    return false;
  }
}

function devPages() {
  const stages = Array.from({ length: Math.ceil(state.ages.length / AGES_PER_PICK) }, (_, i) => i);
  return [
    { label: "Camera", view: "live", go: showLive },
    { label: "Saved", view: "saved", go: showSaved },
    { label: "Faces", view: "check", needs: true, go: showFaceCheck },
    { label: "Family", view: "confirm", needs: true, go: showConfirm },
    { label: "Baby", view: "baby", needs: true, go: showBaby },
    ...stages.map((i) => ({
      label: `Monitor ${stageAges(i).join("-")}`,
      view: "sensors",
      on: () => state.stage === i,
      needs: true,
      go: () => showSensors(i),
    })),
    ...stages.map((i) => ({
      label: `Path ${stageAges(i).join("-")}`,
      view: "choices",
      on: () => state.stage === i,
      needs: true,
      go: () => showChoices(i),
    })),
    ...state.ages.map((age, i) => ({
      label: `Age ${age}`,
      view: "result",
      on: () => state.shown === i,
      needs: true,
      go: () => {
        if (!state.run) showChoices(stageOf(i)); // sets up a run to show results in
        state.reached = Math.max(state.reached, i);
        state.shown = i;
        showResult();
      },
    })),
    { label: "Keepsake", view: "keepsake", needs: true, go: showKeepsake },
  ];
}

function renderDevBar() {
  const bar = $("dev-bar");
  if (!bar) return;
  bar.querySelectorAll("button").forEach((b, i) => {
    const page = devPages()[i];
    b.classList.toggle("on", !!page && page.view === state.view && (!page.on || page.on()));
  });
}

function buildDevBar() {
  if (!DEV_MODE) return;
  document.body.classList.add("dev");
  const bar = document.createElement("div");
  bar.id = "dev-bar";
  bar.className = "dev-bar";
  bar.innerHTML = `<span class="dev-title">DEV MODE</span><span class="dev-pages"></span>`;
  document.body.prepend(bar);
  renderDevPages();
}

function renderDevPages() {
  const holder = document.querySelector("#dev-bar .dev-pages");
  if (!holder) return;
  holder.replaceChildren(
    ...devPages().map((page) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = page.label;
      b.addEventListener("click", async () => {
        if (page.needs && !(await ensureSession())) return;
        page.go();
        renderDevBar();
      });
      return b;
    })
  );
  renderDevBar();
}

// --- Wiring -----------------------------------------------------------------

// New baby / child images fade in softly, once per image.
const revealed = new Set();
for (const id of ["baby-img", "child-img"]) {
  const img = $(id);
  img.addEventListener("load", () => {
    if (revealed.has(img.src)) return;
    revealed.add(img.src);
    restartAnimation(img, "reveal");
    restartAnimation(img.closest(".child-frame"), "glow");
  });
}

tabs.camera.addEventListener("click", showLive);
tabs.saved.addEventListener("click", showSaved);
btn.shutter.addEventListener("click", takePhoto);
btn.back.addEventListener("click", restart);
btn.facesOk.addEventListener("click", showConfirm);
btn.detailsBack.addEventListener("click", showFaceCheck);
btn.confirm.addEventListener("click", confirmDetails);
$("btn-surprise").addEventListener("click", surpriseMe);

// Dragging the gender slider's knob moves it smoothly along the track and it
// stays wherever it's let go. Its third of the track picks Girl, Either or Boy.
// Clicking a word slides the knob to that word.
{
  const choice = document.querySelector(".choice");
  const track = choice.querySelector(".track");
  const knob = choice.querySelector(".knob");
  const radios = [...choice.querySelectorAll('input[name="gender"]')]; // girl, any, boy
  const moveTo = (e) => {
    const box = track.getBoundingClientRect();
    const at = Math.min(1, Math.max(0, (e.clientX - box.left) / box.width));
    knob.style.left = `${at * 100}%`;
    radios[Math.round(at * (radios.length - 1))].checked = true;
  };
  track.addEventListener("pointerdown", (e) => {
    track.setPointerCapture(e.pointerId);
    choice.classList.add("dragging");
    moveTo(e);
  });
  track.addEventListener("pointermove", (e) => {
    if (choice.classList.contains("dragging")) moveTo(e);
  });
  const release = () => choice.classList.remove("dragging");
  track.addEventListener("pointerup", release);
  track.addEventListener("pointercancel", release);
  for (const radio of radios) radio.addEventListener("change", resetGenderKnob);
}
btn.rebaby.addEventListener("click", () => generateBaby());
btn.monitor.addEventListener("click", () => showSensors(0));
btn.grow.addEventListener("click", startGrowing);
btn.restart.addEventListener("click", restart);
btn.next.addEventListener("click", nextAge);
btn.keepsake.addEventListener("click", showKeepsake);
$("details-form").addEventListener("submit", (e) => {
  e.preventDefault();
  confirmDetails();
});

document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" && e.code !== "Enter") return;
  if (e.target.closest("input, textarea")) return; // typing; Enter submits the form
  if (e.target.closest("button") && e.code === "Enter") return; // let buttons handle Enter
  e.preventDefault();
  if (state.view === "live") takePhoto();
  else if (state.view === "check") showConfirm();
  else if (state.view === "confirm") confirmDetails();
  else if (state.view === "baby") showSensors(0);
  else if (state.view === "sensors" || state.view === "choices") startGrowing();
});

document.addEventListener("keydown", (e) => {
  if (state.view !== "result" || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
  if (e.key === "ArrowRight") nextAge();
  else if (state.shown > 0) displayAge(state.shown - 1);
});

api("/api/config")
  .then(({ ages }) => {
    state.ages = ages;
    renderDevPages();
  })
  .catch(() => {});
buildDevBar();
startCamera();
showLive();
