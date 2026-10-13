# Handoff: EldenRing

Written 2026-10-13 by the session that started it.

## What it is
`EldenRing/shotcaller.py`: spoken boss callouts for Elden Ring, modelled on FFXIV's Shotcaller (`roricthorne/FFXIV`,
`Shotcaller/`). The user wants **a Python script**, not an app (asked mid-build; a C# version was dropped before commit).
Standard library only (the user's Python on Windows may be a bundled one, e.g. moomoo's, without pip packages).

## Shape
- Events: Boss (showed up), Anim (animation ID), Hp, Pos, Dead, Note. Recording line format in `format_event`.
- `FightRunner`: boss file -> calls on animations (delay, hold, cooldown, phase by HP% or animation). One fight at a time.
- `EventDiffer`: per-frame character states -> events. `GameReader`: ctypes ReadProcessMemory, AOB for WorldChrMan,
  pointer chains from `offsets.json`. Actor ID = NpcParam ID (two same adds collapse to one; fine for bosses).
- Voice: one PowerShell System.Speech process; a new line cancels the one still speaking.
- Replay + `--anims` work anywhere; tests: `python -m unittest test_shotcaller` (10, all green 10-13).

## State
- Core, replay and tests run (Linux, Python 3.13). **Live mode never ran against the game**: GameReader and Voice are
  Windows-only and unverified.
- `offsets.json` is empty on purpose (no guessed offsets). Needs filling for the user's game version before live works.
- No boss files yet; `bosses/_template.json` only. Animation IDs come from the user's recordings.

## Open
- Fill offsets.json (game version, Cheat Engine table) and run live once: check reads, voice, the recording.
- First boss: the user picks. Record pulls, `replay --anims`, wire calls, replay to check.
- "Boss" fires when a big enemy comes within trackRadius, before the fight really starts; tighten (e.g. boss HP bar
  showing) if it calls too early.

## Rules
Build nothing until the user says "Go" (they said go for this first version); never enter credentials or print keys.
