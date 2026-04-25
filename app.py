from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ── Constants ────────────────────────────────────────────────────────────────

PHASE_LOBBY = "lobby"
PHASE_NIGHT = "night"
PHASE_DAY = "day"
PHASE_FINISH = "finish"

ROLE_NINJA = "ninja"
ROLE_SAMURAI = "samurai"
ROLE_SENSEI = "sensei"
ROLE_HEALER = "healer"

ROLE_NAMES: dict[str, str] = {
    ROLE_NINJA: "Ниндзя",
    ROLE_SAMURAI: "Самурай",
    ROLE_SENSEI: "Сэнсэй",
    ROLE_HEALER: "Целитель",
}

MIN_PLAYERS = 4


# ── Role assignment ──────────────────────────────────────────────────────────

def assign_roles(num_players: int) -> list[str]:
    roles: list[str] = []
    num_ninjas = max(1, num_players // 3)
    roles.extend([ROLE_NINJA] * num_ninjas)
    if num_players >= 5:
        roles.append(ROLE_SENSEI)
    if num_players >= 7:
        roles.append(ROLE_HEALER)
    while len(roles) < num_players:
        roles.append(ROLE_SAMURAI)
    random.shuffle(roles)
    return roles


# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class Player:
    player_id: str
    name: str
    role: str = ""
    is_alive: bool = True
    is_host: bool = False


@dataclass
class GameRoom:
    phase: str = PHASE_LOBBY
    players: dict[str, Player] = field(default_factory=dict)

    # Night-phase tracking
    ninja_votes: dict[str, str] = field(default_factory=dict)   # ninja_id → target_id
    sensei_submitted: bool = False
    sensei_action: str | None = None
    healer_submitted: bool = False
    healer_action: str | None = None

    # Day-phase tracking
    day_votes: dict[str, str | None] = field(default_factory=dict)  # voter_id → target_id | None

    # Round results
    killed_last_night: str | None = None
    sensei_result: dict[str, Any] | None = None
    eliminated_today: str | None = None

    winner: str | None = None   # "ninja" or "samurai"
    event_log: list[str] = field(default_factory=list)
    round_number: int = 0
    action_seq: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        # Ensure each instance gets its own fresh lock (defensive initialisation)
        self.lock = asyncio.Lock()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def append_event(self, msg: str) -> None:
        self.event_log.append(msg)
        if len(self.event_log) > 50:
            self.event_log = self.event_log[-50:]

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.is_alive]

    def alive_ninjas(self) -> list[Player]:
        return [p for p in self.alive_players() if p.role == ROLE_NINJA]

    def alive_non_ninjas(self) -> list[Player]:
        return [p for p in self.alive_players() if p.role != ROLE_NINJA]

    def get_sensei(self) -> Player | None:
        return next((p for p in self.players.values() if p.role == ROLE_SENSEI and p.is_alive), None)

    def get_healer(self) -> Player | None:
        return next((p for p in self.players.values() if p.role == ROLE_HEALER and p.is_alive), None)

    def get_host(self) -> Player | None:
        return next((p for p in self.players.values() if p.is_host), None)

    # ── Phase-completion checks ───────────────────────────────────────────────

    def night_actions_complete(self) -> bool:
        alive_ninja_ids = {p.player_id for p in self.alive_ninjas()}
        if not alive_ninja_ids.issubset(set(self.ninja_votes.keys())):
            return False
        if self.get_sensei() and not self.sensei_submitted:
            return False
        if self.get_healer() and not self.healer_submitted:
            return False
        return True

    def day_votes_complete(self) -> bool:
        alive_ids = {p.player_id for p in self.alive_players()}
        return alive_ids.issubset(set(self.day_votes.keys()))

    def check_winner(self) -> str | None:
        ninjas = self.alive_ninjas()
        non_ninjas = self.alive_non_ninjas()
        if not ninjas:
            return "samurai"
        if len(ninjas) >= len(non_ninjas):
            return "ninja"
        return None

    # ── Transitions ───────────────────────────────────────────────────────────

    def start_game(self) -> None:
        all_players = list(self.players.values())
        roles = assign_roles(len(all_players))
        for player, role in zip(all_players, roles):
            player.role = role
            player.is_alive = True
        self.round_number = 0
        self.winner = None
        self.action_seq = 0
        self.event_log.clear()
        self._begin_night()

    def _begin_night(self) -> None:
        self.phase = PHASE_NIGHT
        self.round_number += 1
        self.ninja_votes.clear()
        self.sensei_action = None
        self.sensei_submitted = False
        self.healer_action = None
        self.healer_submitted = False
        self.killed_last_night = None
        self.sensei_result = None
        self.eliminated_today = None
        self.action_seq += 1
        self.append_event(f"🌙 Ночь {self.round_number}: деревня засыпает...")

    def resolve_night(self) -> None:
        """Resolve night actions and transition to day phase."""
        if self.ninja_votes:
            counts: dict[str, int] = {}
            for tid in self.ninja_votes.values():
                counts[tid] = counts.get(tid, 0) + 1
            max_v = max(counts.values())
            candidates = [t for t, c in counts.items() if c == max_v]
            kill_id: str | None = random.choice(candidates)
        else:
            kill_id = None

        if kill_id and kill_id == self.healer_action and kill_id in self.players:
            self.killed_last_night = None
            saved_name = self.players[kill_id].name
            self.append_event(f"☀️ Рассвет: {saved_name} спасён(а) Целителем — никто не погиб.")
        elif kill_id and kill_id in self.players:
            victim = self.players[kill_id]
            victim.is_alive = False
            self.killed_last_night = kill_id
            self.append_event(f"☀️ Рассвет: {victim.name} найден(а) мёртвым(ой)!")
        else:
            self.killed_last_night = None
            self.append_event("☀️ Рассвет: этой ночью никто не погиб.")

        if self.sensei_action and self.sensei_action in self.players:
            t = self.players[self.sensei_action]
            self.sensei_result = {"name": t.name, "is_ninja": t.role == ROLE_NINJA}

        self.phase = PHASE_DAY
        self.day_votes.clear()
        self.eliminated_today = None
        self.action_seq += 1

    def resolve_day_vote(self) -> None:
        """Tally votes, eliminate if clear majority, then check win."""
        counts: dict[str, int] = {}
        for tid in self.day_votes.values():
            if tid:
                counts[tid] = counts.get(tid, 0) + 1

        if not counts:
            self.eliminated_today = None
            self.append_event("🤐 Деревня решила никого не изгонять.")
        else:
            max_v = max(counts.values())
            candidates = [t for t, c in counts.items() if c == max_v]
            if len(candidates) > 1:
                self.eliminated_today = None
                self.append_event("🤝 Голоса разделились — никто не изгнан.")
            else:
                eid = candidates[0]
                elim = self.players[eid]
                elim.is_alive = False
                self.eliminated_today = eid
                self.append_event(
                    f"🗳️ Деревня изгоняет {elim.name} ({ROLE_NAMES.get(elim.role, elim.role)})."
                )

        self.action_seq += 1

    def finish_game(self, winner: str) -> None:
        self.winner = winner
        self.phase = PHASE_FINISH
        self.action_seq += 1
        if winner == "ninja":
            self.append_event("🥷 Ниндзя захватили деревню! Победа ниндзя!")
        else:
            self.append_event("⚔️ Все ниндзя уничтожены! Победа самураев!")

    def reset_to_lobby(self) -> None:
        self.phase = PHASE_LOBBY
        self.winner = None
        self.round_number = 0
        self.action_seq = 0
        self.event_log.clear()
        for p in self.players.values():
            p.role = ""
            p.is_alive = True
        self.append_event("Игра сброшена. Ожидаем начала новой партии.")

    # ── State serialization ──────────────────────────────────────────────────

    def public_players(self, for_player_id: str | None = None) -> list[dict[str, Any]]:
        me = self.players.get(for_player_id) if for_player_id else None
        result = []
        for p in self.players.values():
            show_role = (
                self.phase == PHASE_FINISH
                or (me and me.role == ROLE_NINJA and p.role == ROLE_NINJA)
                or (me and me.player_id == p.player_id)
            )
            result.append({
                "id": p.player_id,
                "name": p.name,
                "is_alive": p.is_alive,
                "is_host": p.is_host,
                "role": p.role if show_role else None,
                "role_name": ROLE_NAMES.get(p.role) if show_role else None,
            })
        return result

    def state_for(self, player_id: str | None) -> dict[str, Any]:
        me = self.players.get(player_id) if player_id else None

        sensei_result = None
        if me and me.role == ROLE_SENSEI and self.phase == PHASE_DAY and self.sensei_result:
            sensei_result = self.sensei_result

        my_night_done = True
        if me and self.phase == PHASE_NIGHT:
            if not me.is_alive:
                my_night_done = True
            elif me.role == ROLE_NINJA:
                my_night_done = me.player_id in self.ninja_votes
            elif me.role == ROLE_SENSEI:
                my_night_done = self.sensei_submitted
            elif me.role == ROLE_HEALER:
                my_night_done = self.healer_submitted
            else:
                my_night_done = True  # samurai have no night action

        my_day_vote_done = (player_id in self.day_votes) if player_id else False

        return {
            "phase": self.phase,
            "round": self.round_number,
            "players": self.public_players(player_id),
            "eventLog": self.event_log,
            "winner": self.winner,
            "actionSeq": self.action_seq,
            "killedLastNight": self.killed_last_night,
            "eliminatedToday": self.eliminated_today,
            "mySenseiResult": sensei_result,
            "myNightActionDone": my_night_done,
            "myDayVoteDone": my_day_vote_done,
            "me": {
                "id": me.player_id,
                "name": me.name,
                "role": me.role,
                "role_name": ROLE_NAMES.get(me.role, me.role),
                "is_alive": me.is_alive,
                "is_host": me.is_host,
            } if me else None,
        }


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Ночь ниндзя LAN")
room = GameRoom()
connections: dict[WebSocket, str | None] = {}
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


async def broadcast_state() -> None:
    stale: list[WebSocket] = []
    for ws, pid in list(connections.items()):
        try:
            await ws.send_json({"type": "state", "state": room.state_for(pid)})
        except Exception:
            stale.append(ws)
    for ws in stale:
        connections.pop(ws, None)


# ── WebSocket handler ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    connections[websocket] = None
    await websocket.send_json({"type": "state", "state": room.state_for(None)})

    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            msg_type = payload.get("type")

            if msg_type == "join":
                await handle_join(websocket, payload)
            elif msg_type == "start":
                await handle_start(websocket)
            elif msg_type == "night_action":
                await handle_night_action(websocket, payload)
            elif msg_type == "day_vote":
                await handle_day_vote(websocket, payload)
            elif msg_type == "reset":
                await handle_reset(websocket)
            else:
                await websocket.send_json({"type": "error", "message": "Неизвестный тип сообщения."})
    except WebSocketDisconnect:
        pass
    finally:
        pid = connections.pop(websocket, None)
        if pid:
            async with room.lock:
                _on_disconnect(pid)
            await broadcast_state()


# ── Disconnect helper ─────────────────────────────────────────────────────────

def _on_disconnect(pid: str) -> None:
    player = room.players.get(pid)
    if not player:
        return

    if room.phase == PHASE_LOBBY:
        room.players.pop(pid, None)
        room.append_event(f"{player.name} покинул(а) лобби.")
        if player.is_host and room.players:
            next(iter(room.players.values())).is_host = True
        return

    if player.is_alive:
        player.is_alive = False
        room.append_event(f"{player.name} отключился(ась) и выбыл(а) из игры.")

    # Auto-skip pending night actions for the disconnected player
    if room.phase == PHASE_NIGHT:
        if player.role == ROLE_SENSEI and not room.sensei_submitted:
            room.sensei_submitted = True
        elif player.role == ROLE_HEALER and not room.healer_submitted:
            room.healer_submitted = True

    # Auto-abstain for day vote
    if room.phase == PHASE_DAY and pid not in room.day_votes:
        room.day_votes[pid] = None

    _try_advance()


def _try_advance() -> None:
    """Attempt to advance the game phase if all actions are complete."""
    if room.phase == PHASE_NIGHT and room.night_actions_complete():
        room.resolve_night()
        winner = room.check_winner()
        if winner:
            room.finish_game(winner)
    elif room.phase == PHASE_DAY and room.day_votes_complete():
        room.resolve_day_vote()
        winner = room.check_winner()
        if winner:
            room.finish_game(winner)
        else:
            room._begin_night()


# ── Action handlers ───────────────────────────────────────────────────────────

async def handle_join(websocket: WebSocket, payload: dict[str, Any]) -> None:
    name = str(payload.get("name", "")).strip()[:30]
    if not name:
        await websocket.send_json({"type": "error", "message": "Введите имя."})
        return

    async with room.lock:
        if room.phase != PHASE_LOBBY:
            await websocket.send_json({"type": "error", "message": "Игра уже идёт, подождите следующей партии."})
            return

        player_id = uuid.uuid4().hex
        is_first = not room.players
        room.players[player_id] = Player(player_id=player_id, name=name, is_host=is_first)
        connections[websocket] = player_id
        room.append_event(f"{name} присоединился(ась) к игре.")

    await websocket.send_json({"type": "joined", "playerId": player_id})
    await broadcast_state()


async def handle_start(websocket: WebSocket) -> None:
    pid = connections.get(websocket)
    async with room.lock:
        if room.phase != PHASE_LOBBY:
            await websocket.send_json({"type": "error", "message": "Игра уже идёт."})
            return
        player = room.players.get(pid) if pid else None
        if not player or not player.is_host:
            await websocket.send_json({"type": "error", "message": "Только ведущий может начать игру."})
            return
        if len(room.players) < MIN_PLAYERS:
            await websocket.send_json({
                "type": "error",
                "message": f"Нужно минимум {MIN_PLAYERS} игрока.",
            })
            return
        room.start_game()

    await broadcast_state()


async def handle_night_action(websocket: WebSocket, payload: dict[str, Any]) -> None:
    pid = connections.get(websocket)
    if not pid:
        await websocket.send_json({"type": "error", "message": "Сначала войдите в игру."})
        return

    target_id = str(payload.get("target", "")).strip()

    async with room.lock:
        if room.phase != PHASE_NIGHT:
            await websocket.send_json({"type": "error", "message": "Сейчас не ночная фаза."})
            return
        player = room.players.get(pid)
        if not player or not player.is_alive:
            await websocket.send_json({"type": "error", "message": "Вы не можете действовать."})
            return

        if player.role == ROLE_NINJA:
            if pid in room.ninja_votes:
                await websocket.send_json({"type": "error", "message": "Вы уже проголосовали этой ночью."})
                return
            target = room.players.get(target_id)
            if not target or not target.is_alive:
                await websocket.send_json({"type": "error", "message": "Неверная цель."})
                return
            if target.role == ROLE_NINJA:
                await websocket.send_json({"type": "error", "message": "Нельзя атаковать своих."})
                return
            room.ninja_votes[pid] = target_id
            room.action_seq += 1
            room.append_event("🥷 Ниндзя сделал(а) выбор.")

        elif player.role == ROLE_SENSEI:
            if room.sensei_submitted:
                await websocket.send_json({"type": "error", "message": "Вы уже использовали способность."})
                return
            target = room.players.get(target_id)
            if not target or not target.is_alive:
                await websocket.send_json({"type": "error", "message": "Неверная цель."})
                return
            room.sensei_action = target_id
            room.sensei_submitted = True
            room.action_seq += 1
            room.append_event("🔍 Сэнсэй провёл проверку.")

        elif player.role == ROLE_HEALER:
            if room.healer_submitted:
                await websocket.send_json({"type": "error", "message": "Вы уже использовали способность."})
                return
            target = room.players.get(target_id)
            if not target or not target.is_alive:
                await websocket.send_json({"type": "error", "message": "Неверная цель."})
                return
            room.healer_action = target_id
            room.healer_submitted = True
            room.action_seq += 1
            room.append_event("💚 Целитель выбрал(а) кого защитить.")

        else:
            await websocket.send_json({"type": "error", "message": "Ваша роль не имеет ночных действий."})
            return

        _try_advance()

    await broadcast_state()


async def handle_day_vote(websocket: WebSocket, payload: dict[str, Any]) -> None:
    pid = connections.get(websocket)
    if not pid:
        await websocket.send_json({"type": "error", "message": "Сначала войдите в игру."})
        return

    target_id: str | None = payload.get("target") or None

    async with room.lock:
        if room.phase != PHASE_DAY:
            await websocket.send_json({"type": "error", "message": "Сейчас не дневная фаза."})
            return
        player = room.players.get(pid)
        if not player or not player.is_alive:
            await websocket.send_json({"type": "error", "message": "Вы не можете голосовать."})
            return
        if pid in room.day_votes:
            await websocket.send_json({"type": "error", "message": "Вы уже проголосовали."})
            return

        if target_id is not None:
            target = room.players.get(target_id)
            if not target or not target.is_alive:
                await websocket.send_json({"type": "error", "message": "Неверная цель голосования."})
                return
            if target_id == pid:
                await websocket.send_json({"type": "error", "message": "Нельзя голосовать за себя."})
                return
            room.append_event(f"🗳️ {player.name} проголосовал(а).")
        else:
            room.append_event(f"🤐 {player.name} воздержался(ась).")

        room.day_votes[pid] = target_id
        room.action_seq += 1

        _try_advance()

    await broadcast_state()


async def handle_reset(websocket: WebSocket) -> None:
    pid = connections.get(websocket)
    async with room.lock:
        player = room.players.get(pid) if pid else None
        if not player or not player.is_host:
            await websocket.send_json({"type": "error", "message": "Только ведущий может начать новую игру."})
            return
        if room.phase != PHASE_FINISH:
            await websocket.send_json({"type": "error", "message": "Новую игру можно начать только после окончания текущей."})
            return
        room.reset_to_lobby()

    await broadcast_state()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
