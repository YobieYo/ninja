// ── WebSocket ────────────────────────────────────────────────────────────────
const wsProtocol = location.protocol === "https:" ? "wss" : "ws";
const ws = new WebSocket(`${wsProtocol}://${location.host}/ws`);

// ── Client state ─────────────────────────────────────────────────────────────
const state = {
  playerId: null,
  phase: "lobby",
  round: 0,
  players: [],
  eventLog: [],
  winner: null,
  me: null,
  myNightActionDone: false,
  myDayVoteDone: false,
  mySenseiResult: null,
  killedLastNight: null,
  eliminatedToday: null,
  actionSeq: 0,
};

// ── DOM helpers ───────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

function send(payload) {
  if (ws.readyState !== WebSocket.OPEN) {
    toast("Нет соединения с сервером.");
    return;
  }
  ws.send(JSON.stringify(payload));
}

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}

function playerById(id) {
  return state.players.find((p) => p.id === id) || null;
}

// ── Renderers ─────────────────────────────────────────────────────────────────

function renderLobby() {
  $("lobbyCount").textContent = state.players.length;

  const list = $("lobbyList");
  list.innerHTML = "";
  state.players.forEach((p) => {
    const li = document.createElement("li");
    li.textContent = p.name + (p.is_host ? " 👑" : "");
    list.appendChild(li);
  });

  if (state.me) {
    $("joinCard").classList.add("hidden");
    $("waitCard").classList.remove("hidden");
    $("lobbyMyName").textContent = state.me.name;
    $("lobbyHostHint").classList.toggle("hidden", !state.me.is_host);
    $("startBtn").classList.toggle("hidden", !state.me.is_host);
  } else {
    $("joinCard").classList.remove("hidden");
    $("waitCard").classList.add("hidden");
  }
}

function renderNight() {
  $("nightSection").classList.remove("hidden");
  $("daySection").classList.add("hidden");

  // hide all status messages first
  ["nightDone", "nightSleep", "nightDead"].forEach((id) => $(id).classList.add("hidden"));
  $("nightTargets").innerHTML = "";

  const me = state.me;

  if (!me || !me.is_alive) {
    $("nightTitle").textContent = "💀 Ночь";
    $("nightDesc").textContent = "";
    $("nightDead").classList.remove("hidden");
    return;
  }

  if (state.myNightActionDone) {
    if (me.role === "samurai") {
      $("nightTitle").textContent = "😴 Деревня спит";
      $("nightDesc").textContent = "";
      $("nightSleep").classList.remove("hidden");
    } else {
      $("nightTitle").textContent = "✅ Действие выполнено";
      $("nightDesc").textContent = "";
      $("nightDone").classList.remove("hidden");
    }
    return;
  }

  const role = me.role;

  if (role === "ninja") {
    $("nightTitle").textContent = "🥷 Совет ниндзя";
    $("nightDesc").textContent = "Выберите жертву этой ночью:";
    const targets = state.players.filter((p) => p.is_alive && p.role !== "ninja");
    buildTargetBtns($("nightTargets"), targets, "danger", (id) => {
      send({ type: "night_action", target: id });
    });
  } else if (role === "sensei") {
    $("nightTitle").textContent = "🔍 Проверка Сэнсэя";
    $("nightDesc").textContent = "Выберите игрока для проверки:";
    const targets = state.players.filter((p) => p.is_alive && p.id !== me.id);
    buildTargetBtns($("nightTargets"), targets, "secondary", (id) => {
      send({ type: "night_action", target: id });
    });
  } else if (role === "healer") {
    $("nightTitle").textContent = "💚 Защита Целителя";
    $("nightDesc").textContent = "Выберите кого защитить этой ночью:";
    const targets = state.players.filter((p) => p.is_alive);
    buildTargetBtns($("nightTargets"), targets, "secondary", (id) => {
      send({ type: "night_action", target: id });
    });
  } else {
    // samurai
    $("nightTitle").textContent = "😴 Деревня спит";
    $("nightDesc").textContent = "";
    $("nightSleep").classList.remove("hidden");
  }
}

function renderDay() {
  $("nightSection").classList.add("hidden");
  $("daySection").classList.remove("hidden");

  // Night result
  const killedPlayer = state.killedLastNight ? playerById(state.killedLastNight) : null;
  $("nightResultMsg").textContent = killedPlayer
    ? `${killedPlayer.name} был(а) убит(а) ниндзя.`
    : "Этой ночью никто не погиб.";
  $("nightResultMsg").className = "night-result " + (killedPlayer ? "result-kill" : "result-safe");

  // Sensei result (only visible to sensei)
  const sr = state.mySenseiResult;
  if (sr) {
    $("senseiBox").classList.remove("hidden");
    $("senseiText").textContent = sr.is_ninja
      ? `${sr.name} — 🥷 НИНДЗЯ!`
      : `${sr.name} — ✅ не ниндзя`;
    $("senseiText").className = "sensei-text " + (sr.is_ninja ? "ninja-yes" : "ninja-no");
  } else {
    $("senseiBox").classList.add("hidden");
  }

  // Voting
  ["voteDone", "voteDead"].forEach((id) => $(id).classList.add("hidden"));
  $("voteTargets").innerHTML = "";
  $("abstainBtn").classList.remove("hidden");

  const me = state.me;
  if (!me || !me.is_alive) {
    $("voteDead").classList.remove("hidden");
    $("abstainBtn").classList.add("hidden");
    return;
  }
  if (state.myDayVoteDone) {
    $("voteDone").classList.remove("hidden");
    $("abstainBtn").classList.add("hidden");
    return;
  }

  const targets = state.players.filter((p) => p.is_alive && p.id !== me.id);
  buildTargetBtns($("voteTargets"), targets, "danger", (id) => {
    send({ type: "day_vote", target: id });
  });
}

function buildTargetBtns(container, players, btnClass, onClick) {
  if (players.length === 0) {
    container.innerHTML = "<p class='hint'>Нет доступных целей.</p>";
    return;
  }
  players.forEach((p) => {
    const btn = document.createElement("button");
    btn.className = btnClass;
    btn.textContent = p.name;
    btn.addEventListener("click", () => onClick(p.id));
    container.appendChild(btn);
  });
}

function renderPlayers() {
  const list = $("playersList");
  list.innerHTML = "";
  state.players.forEach((p) => {
    const li = document.createElement("li");
    const roleTag = p.role_name ? ` · ${p.role_name}` : "";
    li.textContent = (p.is_alive ? "" : "💀 ") + p.name + roleTag;
    if (!p.is_alive) li.classList.add("dead");
    list.appendChild(li);
  });
}

function renderLog(listId) {
  const list = $(listId);
  list.innerHTML = "";
  [...state.eventLog].reverse().forEach((entry) => {
    const li = document.createElement("li");
    li.textContent = entry;
    list.appendChild(li);
  });
}

function renderPhaseLabel() {
  const labels = {
    night: `🌙 Ночь ${state.round}`,
    day: `☀️ День ${state.round}`,
    finish: "🏆 Игра окончена",
  };
  $("phaseLabel").textContent = labels[state.phase] || state.phase;
}

function renderMyRole() {
  if (!state.me) return;
  const emojis = { ninja: "🥷", samurai: "⚔️", sensei: "🔍", healer: "💚" };
  const emoji = emojis[state.me.role] || "";
  const badge = $("myRoleBadge");
  badge.textContent = `${emoji} ${state.me.role_name || ""}`;
  badge.className = `role-badge role-${state.me.role}`;
  $("myNameLabel").textContent = state.me.name;
}

function renderFinish() {
  const winnerData = {
    ninja: { emoji: "🥷", title: "Победа ниндзя!", sub: "Ниндзя захватили деревню." },
    samurai: { emoji: "⚔️", title: "Победа самураев!", sub: "Все ниндзя уничтожены." },
  };
  const wd = winnerData[state.winner] || { emoji: "🏆", title: "Игра окончена", sub: "" };
  $("finishEmoji").textContent = wd.emoji;
  $("finishTitle").textContent = wd.title;
  $("finishSub").textContent = wd.sub;

  const list = $("finalPlayers");
  list.innerHTML = "";
  state.players.forEach((p) => {
    const li = document.createElement("li");
    li.textContent = (p.is_alive ? "" : "💀 ") + p.name + " — " + (p.role_name || "?");
    if (!p.is_alive) li.classList.add("dead");
    list.appendChild(li);
  });

  renderLog("finalLog");

  $("resetBtn").classList.toggle("hidden", !(state.me && state.me.is_host));
}

// ── Main render dispatcher ────────────────────────────────────────────────────

function render() {
  const phase = state.phase;

  $("lobbySection").classList.toggle("hidden", phase !== "lobby");
  $("gameSection").classList.toggle("hidden", phase === "lobby" || phase === "finish");
  $("finishSection").classList.toggle("hidden", phase !== "finish");

  if (phase === "lobby") {
    renderLobby();
    return;
  }
  if (phase === "finish") {
    renderFinish();
    return;
  }

  renderPhaseLabel();
  renderMyRole();
  renderPlayers();
  renderLog("eventLog");

  if (phase === "night") renderNight();
  else if (phase === "day") renderDay();
}

// ── WebSocket events ──────────────────────────────────────────────────────────

ws.addEventListener("open", () => toast("Соединение установлено."));
ws.addEventListener("close", () => toast("Соединение потеряно. Обновите страницу."));

ws.addEventListener("message", (event) => {
  let data;
  try {
    data = JSON.parse(event.data);
  } catch {
    return;
  }

  if (data.type === "error") {
    toast(data.message || "Ошибка.");
    return;
  }
  if (data.type === "joined") {
    state.playerId = data.playerId;
    return;
  }
  if (data.type === "state") {
    const s = data.state;
    state.phase = s.phase;
    state.round = s.round || 0;
    state.players = s.players || [];
    state.eventLog = s.eventLog || [];
    state.winner = s.winner || null;
    state.me = s.me || null;
    state.myNightActionDone = Boolean(s.myNightActionDone);
    state.myDayVoteDone = Boolean(s.myDayVoteDone);
    state.mySenseiResult = s.mySenseiResult || null;
    state.killedLastNight = s.killedLastNight || null;
    state.eliminatedToday = s.eliminatedToday || null;
    state.actionSeq = s.actionSeq || 0;
    render();
  }
});

// ── Button handlers ───────────────────────────────────────────────────────────

$("joinBtn").addEventListener("click", () => {
  const name = $("nameInput").value.trim();
  if (!name) { toast("Введите имя."); return; }
  send({ type: "join", name });
});

$("nameInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("joinBtn").click();
});

$("startBtn").addEventListener("click", () => send({ type: "start" }));

$("abstainBtn").addEventListener("click", () => send({ type: "day_vote", target: null }));

$("resetBtn").addEventListener("click", () => send({ type: "reset" }));
