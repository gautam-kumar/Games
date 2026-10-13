"""Elden Ring Shotcaller: spoken boss callouts, modelled on FFXIV's Shotcaller.

Reads the game's memory (read only), turns what the boss does into events (it showed up, started an animation, lost HP,
died), and speaks the calls wired up in bosses/*.json. Records every pull so new calls can be wired up from real fights,
and replays recordings through the rules without the game.

    python shotcaller.py                      live (Windows, game running OFFLINE with Easy Anti-Cheat off); Ctrl+C stops
    python shotcaller.py check                check offsets.json and the boss files
    python shotcaller.py replay <rec.log>     what you would have seen and heard, per pull
    python shotcaller.py replay <rec.log> --anims    every boss animation per pull (to wire up new calls)

Standard library only. The voice is Windows text-to-speech through one PowerShell process kept open.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

HOME = Path(__file__).resolve().parent
PLAYER = 0  # the player's actor ID; everyone else is their NpcParam ID

# --- Events and the recording format ---------------------------------------------------------------------------------

KINDS = ("Boss", "Anim", "Hp", "Pos", "Dead", "Note")
# Boss: a boss showed up (value = HP, max = max HP)   Anim: started an animation (value = animation ID)
# Hp: HP changed (value, max)   Pos: position sample (4/s while in a fight)   Dead: died   Note: free text


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str
    actor: int = 0
    name: str = ""
    value: int = 0
    max: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    text: str = ""


_LINE = re.compile(r"^(?P<t>\d\d:\d\d:\d\d\.\d{3}) (?P<kind>[A-Za-z]+) (?:(?P<name>.*?)\[(?P<id>\d+)\](?P<rest>.*)|(?P<text>.*))$")
_FIELD = re.compile(r"\b(?P<k>anim|hp|max|pos)=(?P<v>\S+)")


def format_event(e: Event) -> str:
    """One recording line: `HH:mm:ss.fff Kind name[actor] key=value ...`, e.g. `21:04:13.250 Anim Malenia[2120000] anim=3010 pos=(...)`."""
    head = f"{e.at:%H:%M:%S}.{e.at.microsecond // 1000:03d} {e.kind}"
    if e.kind == "Note":
        return f"{head} {e.text}"
    s = f"{head} {e.name.replace('[', '(').replace(']', ')')}[{e.actor}]"
    if e.kind == "Anim":
        s += f" anim={e.value}"
    elif e.kind in ("Boss", "Hp"):
        s += f" hp={e.value} max={e.max}"
    return s + f" pos=({e.x:.2f},{e.y:.2f},{e.z:.2f})"


def parse_line(line: str, day: datetime) -> Optional[Event]:
    m = _LINE.match(line.rstrip("\r\n"))
    if not m or m["kind"] not in KINDS:
        return None
    hh, mm, rest = m["t"].split(":")
    ss, ms = rest.split(".")
    at = day.replace(hour=int(hh), minute=int(mm), second=int(ss), microsecond=int(ms) * 1000)
    if m["kind"] == "Note":
        return Event(at, "Note", text=m["text"] if m["text"] is not None else m["name"] + "[" + m["id"] + "]" + m["rest"])
    if m["id"] is None:
        return None
    value = mx = 0
    x = y = z = 0.0
    for f in _FIELD.finditer(m["rest"]):
        k, v = f["k"], f["v"]
        if k in ("anim", "hp"):
            value = int(v)
        elif k == "max":
            mx = int(v)
        else:
            x, y, z = (float(p) for p in v.strip("()").split(","))
    return Event(at, m["kind"], int(m["id"]), m["name"], value, mx, x, y, z)


@dataclass
class Pull:
    """From a boss showing up to your death ("Dead"), the boss's ("Kill"), or the end of the recording ("Open")."""
    boss: str
    boss_id: int
    events: list[Event] = field(default_factory=list)
    end: str = "Open"

    @property
    def start(self) -> datetime:
        return self.events[0].at


def split_pulls(lines: Iterable[str]) -> list[Pull]:
    pulls: list[Pull] = []
    pull: Optional[Pull] = None
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    last = None
    for line in lines:
        e = parse_line(line, day)
        if e is None:
            continue
        if last is not None and e.at < last - timedelta(hours=12):  # past midnight
            day += timedelta(days=1)
            e = replace(e, at=e.at + timedelta(days=1))
        last = e.at
        if e.kind == "Boss":
            pull = Pull(e.name, e.actor)
            pulls.append(pull)
        if pull is None:
            continue
        pull.events.append(e)
        if e.kind == "Dead" and (e.actor == PLAYER or e.actor == pull.boss_id):
            pull.end = "Dead" if e.actor == PLAYER else "Kill"
            pull = None
    return pulls


# --- Boss rules -------------------------------------------------------------------------------------------------------

def load_jsonc(text: str):
    """JSON with // comments and trailing commas (the boss files and settings are hand-edited)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        else:
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[\]}])", r"\1", "".join(out)))


@dataclass(eq=False)
class Call:
    """When the boss starts `anim` (in `phase`, 0 = any): show `text`, speak `say` ("" = the text, "-" = silent) after
    `delay` s, for `hold` s. `cooldown`: one attack can restart its animation; the call won't fire again that soon."""
    anim: int
    text: str
    say: str = ""
    phase: int = 0
    delay: float = 0.0
    hold: float = 3.0
    cooldown: float = 1.5

    @property
    def voice(self) -> Optional[str]:
        return None if self.say == "-" else self.say or self.text


@dataclass
class PhaseRule:
    """A phase change: the boss's HP fell below `hp_below` (0-1), or it started `anim`."""
    phase: int
    hp_below: Optional[float] = None
    anim: Optional[int] = None
    say: str = ""


@dataclass
class Boss:
    """One bosses/<boss>.json. `npc_param_ids`: the characters that count as this boss (a duo lists both)."""
    name: str
    npc_param_ids: list[int]
    phases: list[PhaseRule] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    @staticmethod
    def parse(d: dict) -> "Boss":
        return Boss(
            d.get("name", ""),
            [int(i) for i in d.get("npcParamIds", [])],
            [PhaseRule(p["phase"], p.get("hpBelow"), p.get("anim"), p.get("say", "")) for p in d.get("phases", [])],
            [Call(c["anim"], c.get("text", ""), c.get("say", ""), c.get("phase", 0), c.get("delay", 0.0), c.get("hold", 3.0), c.get("cooldown", 1.5))
             for c in d.get("calls", [])])


def load_bosses(folder: Path) -> list[Boss]:
    """Every *.json in the folder except the ones starting with "_" (templates)."""
    if not folder.is_dir():
        return []
    return [Boss.parse(load_jsonc(f.read_text(encoding="utf-8"))) for f in sorted(folder.glob("*.json")) if not f.name.startswith("_")]


class Host:
    """What the runner talks to: live = voice + console + recording; replay = printed."""

    def callout(self, text: str, say: Optional[str], at: datetime, until: datetime) -> None: ...

    def note(self, text: str) -> None: ...


class FightRunner:
    """Runs the boss rules on the event stream: finds the boss when it shows up, tracks its phase, fires the calls for its
    animations (delayed and held as each says). One fight at a time; your death or the boss's ends it."""

    def __init__(self, bosses: list[Boss], host: Host):
        self.bosses = bosses
        self.host = host
        self.boss: Optional[Boss] = None
        self.phase = 0
        self._pending: list[tuple[datetime, int, Call]] = []
        self._last: dict[int, datetime] = {}

    def on(self, e: Event) -> None:
        b = self.boss
        if e.kind == "Boss" and b is None:
            match = next((x for x in self.bosses if e.actor in x.npc_param_ids), None)
            if match:
                self.boss, self.phase = match, 1
                self._pending.clear()
                self._last.clear()
                self.host.note(f"Fight {match.name}")
        elif b and e.actor in b.npc_param_ids and e.kind == "Hp" and e.max > 0:
            self._phase(e, lambda p: p.hp_below is not None and e.value / e.max < p.hp_below)
        elif b and e.actor in b.npc_param_ids and e.kind == "Anim":
            self._phase(e, lambda p: p.anim == e.value)
            for call in b.calls:
                if call.anim != e.value or call.phase not in (0, self.phase):
                    continue
                last = self._last.get(id(call))
                if last is not None and (e.at - last).total_seconds() < call.cooldown:
                    continue
                self._last[id(call)] = e.at
                self._pending.append((e.at + timedelta(seconds=call.delay), len(self._pending), call))
        elif b and e.kind == "Dead" and (e.actor == PLAYER or e.actor in b.npc_param_ids):
            self.host.note(f"{'Wipe' if e.actor == PLAYER else 'Kill'} {b.name}")
            self.boss, self.phase = None, 0
            self._pending.clear()
        self.tick(e.at)

    def tick(self, now: datetime) -> None:
        """Fires the calls whose delay is over. Live: every frame; replay: on every event."""
        self._pending.sort(key=lambda p: (p[0], p[1]))
        while self._pending and self._pending[0][0] <= now:
            at, _, call = self._pending.pop(0)
            self.host.callout(call.text, call.voice, at, at + timedelta(seconds=call.hold))

    @property
    def next_due(self) -> Optional[datetime]:
        return min((p[0] for p in self._pending), default=None)

    def _phase(self, e: Event, hit: Callable[[PhaseRule], bool]) -> None:
        for p in sorted((p for p in self.boss.phases if p.phase > self.phase and hit(p)), key=lambda p: p.phase)[:1]:
            self.phase = p.phase
            self.host.note(f"Phase {p.phase}")
            if p.say:
                self.host.callout(f"Phase {p.phase}", p.say, e.at, e.at + timedelta(seconds=3))


# --- Frames to events -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Chr:
    """One character as the game reader sees it in one frame (actor 0 = you)."""
    actor: int
    name: str
    anim: int
    hp: int
    max_hp: int
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class EventDiffer:
    """Per-frame character states -> events: a boss showing up, animation starts, HP changes and deaths."""

    def __init__(self, is_boss: Callable[[Chr], bool]):
        self.is_boss = is_boss
        self._last: dict[int, Chr] = {}

    def diff(self, now: datetime, frame: list[Chr]) -> list[Event]:
        out: list[Event] = []

        def ev(kind: str, c: Chr, value: int = 0, mx: int = 0) -> Event:
            return Event(now, kind, c.actor, c.name, value, mx, c.x, c.y, c.z)

        for c in frame:
            before = self._last.get(c.actor)
            self._last[c.actor] = c
            if before is None:
                if c.actor != PLAYER and c.hp > 0 and self.is_boss(c):
                    out.append(ev("Boss", c, c.hp, c.max_hp))
                continue
            if c.anim != before.anim:
                out.append(ev("Anim", c, c.anim))
            if c.hp != before.hp:
                out.append(ev("Hp", c, c.hp, c.max_hp))
                if c.hp <= 0 < before.hp:
                    out.append(ev("Dead", c))
        # Gone (unloaded or out of range): forget it, so it shows up as new when it's back.
        seen = {c.actor for c in frame}
        for gone in [k for k in self._last if k not in seen]:
            del self._last[gone]
        return out


# --- Byte patterns ----------------------------------------------------------------------------------------------------

def aob_regex(pattern: str) -> re.Pattern:
    """`"48 8B 05 ?? ?? ?? ??"` (?? = any byte) as a bytes regex: fast enough to scan the whole game image."""
    parts = pattern.split()
    if not parts:
        raise ValueError("empty AOB")
    return re.compile(b"".join(b"." if p in ("?", "??") else re.escape(bytes([int(p, 16)])) for p in parts), re.DOTALL)


def aob_find(data: bytes, pattern: str) -> int:
    m = aob_regex(pattern).search(data)
    return m.start() if m else -1


def resolve_rip(data: bytes, base: int, at: int, disp_offset: int, length: int) -> int:
    """Where a RIP-relative instruction at `at` points: its int32 displacement counts from the instruction's end."""
    return base + at + length + struct.unpack_from("<i", data, at + disp_offset)[0]


# --- Live: settings, offsets, game reader, voice, recorder (Windows) --------------------------------------------------

SETTINGS = {"voice": True, "voiceRate": 2, "record": True, "trackRadius": 40.0, "bossMinHp": 5000, "pollRate": 30}

OFFSET_FIELDS = ["worldChrMan.aob", "player", "characters.begin", "characters.end",
                 "chr.npcParamId", "chr.hp", "chr.maxHp", "chr.anim", "chr.pos"]


def load_settings() -> dict:
    path = HOME / "settings.json"
    return {**SETTINGS, **(load_jsonc(path.read_text(encoding="utf-8")) if path.exists() else {})}


def load_offsets() -> dict:
    path = HOME / "offsets.json"
    return load_jsonc(path.read_text(encoding="utf-8")) if path.exists() else {}


def missing_offsets(o: dict) -> list[str]:
    def get(path: str):
        v = o
        for part in path.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        return v
    return [f for f in OFFSET_FIELDS if not get(f)]


def processes() -> list[tuple[str, int]]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        cells = line.strip().strip('"').split('","')
        if len(cells) > 1 and cells[1].isdigit():
            rows.append((cells[0], int(cells[1])))
    return rows


class GameReader:
    """Reads eldenring.exe's memory (never writes). Finds WorldChrMan by its AOB once, then each frame walks the
    character list: you plus everyone within the track radius. EAC must be off (it blocks the reads, and bans for them)."""

    def __init__(self, offsets: dict):
        from ctypes import wintypes
        self.o = offsets
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self._k32 = k32

        pid = next((p for n, p in processes() if n.lower() == "eldenring.exe"), None)
        if pid is None:
            raise RuntimeError("eldenring.exe isn't running.")
        self._h = k32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | QUERY_INFORMATION
        if not self._h:
            raise RuntimeError(f"Can't open the game (error {ctypes.get_last_error()}): is EAC on?")

        mods = (wintypes.HMODULE * 1)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(self._h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 0x03):
            raise RuntimeError("Can't list the game's modules: is EAC on?")

        class ModuleInfo(ctypes.Structure):
            _fields_ = [("base", ctypes.c_void_p), ("size", wintypes.DWORD), ("entry", ctypes.c_void_p)]
        info = ModuleInfo()
        psapi.GetModuleInformation(self._h, mods[0], ctypes.byref(info), ctypes.sizeof(info))
        image = self.read(info.base, info.size)
        if image is None:
            raise RuntimeError("Can't read the game's code: is EAC on?")
        w = offsets["worldChrMan"]
        at = aob_find(image, w["aob"])
        if at < 0:
            raise RuntimeError(f"worldChrMan.aob not found (offsets.json is for game version \"{offsets.get('gameVersion', '')}\").")
        self._world_static = resolve_rip(image, info.base, at, w.get("dispOffset", 3), w.get("length", 7))

    def read(self, at: int, size: int) -> Optional[bytes]:
        if not at:
            return None
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = self._k32.ReadProcessMemory(self._h, ctypes.c_void_p(at), buf, size, ctypes.byref(got))
        return buf.raw if ok and got.value == size else None

    def ptr(self, at: int) -> int:
        b = self.read(at, 8)
        return struct.unpack("<Q", b)[0] if b else 0

    def i32(self, at: int) -> int:
        b = self.read(at, 4)
        return struct.unpack("<i", b)[0] if b else 0

    def address(self, start: int, chain: list[int]) -> int:
        """Every offset but the last: add, then read a pointer; the last: add. 0 if a pointer on the way is null."""
        at = start
        for off in chain[:-1]:
            at = self.ptr(at + off)
            if not at:
                return 0
        return at + chain[-1] if chain else at

    def chr(self, ins: int, name: str) -> Optional[Chr]:
        f = self.o["chr"]
        pos = self.read(self.address(ins, f["pos"]), 12)
        if pos is None:
            return None
        x, y, z = struct.unpack("<fff", pos)
        return Chr(self.i32(self.address(ins, f["npcParamId"])), name, self.i32(self.address(ins, f["anim"])),
                   self.i32(self.address(ins, f["hp"])), self.i32(self.address(ins, f["maxHp"])), x, y, z)

    def frame(self, radius: float, name_of: Callable[[int], str]) -> list[Chr]:
        world = self.ptr(self._world_static)
        if not world:
            return []  # loading screen
        player_ins = self.ptr(self.address(world, self.o["player"]))
        me = self.chr(player_ins, "You") if player_ins else None
        if me is None:
            return []
        me = replace(me, actor=PLAYER)
        frame = [me]
        t = self.o["characters"]
        begin, end = self.ptr(self.address(world, t["begin"])), self.ptr(self.address(world, t["end"]))
        stride = max(8, t.get("stride", 8))
        if not begin or end <= begin or (end - begin) // stride > 4096:
            return frame
        seen = {PLAYER}
        for slot in range(begin, end, stride):
            ins = self.ptr(slot)
            c = self.chr(ins, "") if ins and ins != player_ins else None
            if c is None or c.max_hp <= 0 or c.actor in seen:
                continue
            if (c.x - me.x) ** 2 + (c.z - me.z) ** 2 <= radius * radius:
                seen.add(c.actor)
                frame.append(replace(c, name=name_of(c.actor)))
        return frame

    def close(self) -> None:
        self._k32.CloseHandle(self._h)


class Voice:
    """Windows text-to-speech (System.Speech) in one PowerShell process kept open, so each line is spoken at once.
    A new line cuts off the one still being said (a late call is worse than none)."""

    def __init__(self, rate: int):
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.Rate = {int(rate)}; while (($l = [Console]::In.ReadLine()) -ne $null) {{ $s.SpeakAsyncCancelAll(); $s.SpeakAsync($l) | Out-Null }}")
        self._p = subprocess.Popen(["powershell", "-NoProfile", "-Command", script], stdin=subprocess.PIPE, text=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def say(self, text: str) -> None:
        try:
            self._p.stdin.write(text.replace("\n", " ") + "\n")
            self._p.stdin.flush()
        except OSError as ex:
            print(f"voice unavailable: {ex}")

    def close(self) -> None:
        self._p.stdin.close()
        self._p.terminate()


class Recorder:
    """Every fight event to recordings/rec-YYYY-MM-DD.log (appended). Must never break the live loop."""

    def __init__(self, on: bool):
        self.on = on
        self._path: Optional[Path] = None
        self._file = None

    def write(self, e: Event) -> None:
        if not self.on:
            return
        try:
            path = HOME / "recordings" / f"rec-{e.at:%Y-%m-%d}.log"
            if path != self._path:
                path.parent.mkdir(exist_ok=True)
                if self._file:
                    self._file.close()
                self._file, self._path = open(path, "a", encoding="utf-8", buffering=1), path
            self._file.write(format_event(e) + "\n")
        except OSError:
            pass


class LiveHost(Host):
    def __init__(self, voice: Optional[Voice], recorder: Recorder):
        self.voice, self.recorder = voice, recorder

    def callout(self, text, say, at, until):
        print(f"{at:%H:%M:%S}  \033[93m{text}\033[0m", flush=True)
        if say and self.voice:
            self.voice.say(say)

    def note(self, text):
        print(f"{datetime.now():%H:%M:%S}  ({text})", flush=True)
        self.recorder.write(Event(datetime.now(), "Note", text=text))


def live() -> int:
    settings, offsets, bosses = load_settings(), load_offsets(), load_bosses(HOME / "bosses")
    print(f"Shotcaller (Elden Ring): {len(bosses)} boss file(s): {', '.join(b.name for b in bosses) or '-'}")
    if missing := missing_offsets(offsets):
        print(f"offsets.json isn't filled in yet; missing: {', '.join(missing)}")
        return 2
    if os.name != "nt":
        print("Live mode runs on Windows only (replay works anywhere).")
        return 2
    if any("easyanticheat" in n.lower() for n, _ in processes()):
        print("Easy Anti-Cheat is running: start the game offline with EAC off. Not attaching.")
        return 3
    try:
        reader = GameReader(offsets)
    except RuntimeError as ex:
        print(ex)
        return 4

    names = {i: b.name for b in reversed(bosses) for i in b.npc_param_ids}
    voice = Voice(settings["voiceRate"]) if settings["voice"] else None
    recorder = Recorder(settings["record"])
    runner = FightRunner(bosses, LiveHost(voice, recorder))
    differ = EventDiffer(lambda c: c.actor in names or c.max_hp >= settings["bossMinHp"])
    in_fight: set[int] = set()  # characters seen as bosses this pull: their positions get recorded
    last_pos = datetime.min
    tick = 1.0 / min(max(settings["pollRate"], 5), 120)
    print("Attached. Ctrl+C to stop.")
    try:
        while True:
            now = datetime.now()
            frame = reader.frame(settings["trackRadius"], lambda i: names.get(i, f"npc{i}"))
            for e in differ.diff(now, frame):
                if e.kind == "Boss":
                    in_fight.add(e.actor)
                if e.actor == PLAYER or e.actor in in_fight:
                    recorder.write(e)
                runner.on(e)
                if e.kind == "Dead" and (e.actor == PLAYER or e.actor in in_fight):
                    in_fight.clear()
            runner.tick(now)
            if in_fight and now - last_pos >= timedelta(milliseconds=250):
                last_pos = now
                for c in frame:
                    if c.actor == PLAYER or c.actor in in_fight:
                        recorder.write(Event(now, "Pos", c.actor, c.name, x=c.x, y=c.y, z=c.z))
            time.sleep(tick)
    except KeyboardInterrupt:
        return 0
    finally:
        reader.close()
        if voice:
            voice.close()


# --- Replay and check -------------------------------------------------------------------------------------------------

class ReplayHost(Host):
    def __init__(self, start: datetime):
        self.start, self.calls = start, 0

    def callout(self, text, say, at, until):
        self.calls += 1
        says = f'  (says "{say}")' if say and say != text else ""
        print(f"  {(at - self.start).total_seconds():7.2f}  CALL  {text}{says}  for {(until - at).total_seconds():.1f} s")

    def note(self, text):
        print(f"           note  {text}")


def replay(path: Path, bosses_dir: Path, anims: bool) -> int:
    bosses = load_bosses(bosses_dir)
    with open(path, encoding="utf-8") as f:
        pulls = split_pulls(f)
    print(f"{len(pulls)} pull(s), {len(bosses)} boss file(s) from {bosses_dir}")
    for pull in pulls:
        print(f"\n== {pull.start:%H:%M:%S} {pull.boss} [{pull.boss_id}] ({pull.end}, {(pull.events[-1].at - pull.start).total_seconds():.0f} s)")
        if anims:
            seen: dict[tuple[int, int], list[Event]] = {}
            for e in pull.events:
                if e.kind == "Anim" and e.actor != PLAYER:
                    seen.setdefault((e.actor, e.value), []).append(e)
            for (actor, anim), es in seen.items():
                print(f"  {actor:>9} anim {anim:>8}  x{len(es):<3} first {(es[0].at - pull.start).total_seconds():6.1f} s")
            continue
        host = ReplayHost(pull.start)
        runner = FightRunner(bosses, host)
        for e in pull.events:
            runner.on(e)
        if runner.next_due:
            runner.tick(runner.next_due + timedelta(hours=1))  # calls still waiting when the recording ends
        if host.calls == 0:
            print("  (no callouts: no boss file matches, or none of its animations came up)")
    return 0


def check() -> int:
    bosses = load_bosses(HOME / "bosses")
    for b in bosses:
        print(f"{b.name}: npcParamIds {b.npc_param_ids}, {len(b.phases)} phase rule(s), {len(b.calls)} call(s)")
    missing = missing_offsets(load_offsets())
    print(f"offsets.json: missing {', '.join(missing)}" if missing else "offsets.json: complete")
    return 2 if missing else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Elden Ring Shotcaller")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check")
    r = sub.add_parser("replay")
    r.add_argument("recording", type=Path)
    r.add_argument("--bosses", type=Path, default=HOME / "bosses")
    r.add_argument("--anims", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "replay":
        return replay(a.recording, a.bosses, a.anims)
    if a.cmd == "check":
        return check()
    return live()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
