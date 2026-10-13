# EldenRing

Spoken boss callouts for Elden Ring, modelled on Shotcaller (the FFXIV fight-callout plugin in `roricthorne/FFXIV`,
folder `Shotcaller/`). One Python script, standard library only.

| Shotcaller (FFXIV) | Here |
|---|---|
| Fight logic: casts, statuses -> callouts | `bosses/<boss>.json`: boss animation ID -> callout (delay, hold, phase) |
| SAPI voice | Windows text-to-speech (one PowerShell process kept open) |
| Recorder: every fight event to `rec-*.log` | `recordings/rec-YYYY-MM-DD.log`: boss showed up, animations, HP, deaths, positions |
| Replay CLI: rules on a recording, no game | `python shotcaller.py replay <rec.log>` |
| Dalamud gives the game state | `GameReader`: reads the game's memory (read only), pointers from `offsets.json` |

Left out on purpose: Render (floor drawings), ACR reactions, OBS, the settings window.

## Use

```
python shotcaller.py check                         offsets.json and boss files
python shotcaller.py                               live (Windows), Ctrl+C stops
python shotcaller.py replay <rec.log>              callouts per pull, timed from the pull's start
python shotcaller.py replay <rec.log> --anims      every boss animation per pull (to wire up new calls)
python -m unittest test_shotcaller                 tests (no game)
```

**Offline only, Easy Anti-Cheat off.** Reading the game's memory with EAC on gets blocked, and online it risks a ban. The
script refuses to attach while an EasyAntiCheat process is running.

## Before live works

`offsets.json` ships empty: where the character list, HP and animation ID sit in memory changes with game patches, and
nothing is guessed. Fill it for your game version (a Cheat Engine table for that version has them); `check` lists what's
missing.

## Wiring up a boss

1. Fight it a few times with recording on (`settings.json` `"record": true`).
2. `replay recordings/rec-....log --anims`: the boss's NpcParam ID and every animation it used, when, and how often.
3. Copy `bosses/_template.json` to `bosses/<boss>.json`; add the ID and a call per animation worth one.
4. `replay` the same recording: check each call comes at the right moment.

## Settings (`settings.json`)

`voice`, `voiceRate` (-10..10), `record`, `trackRadius` (metres), `bossMinHp` (a big enemy counts as a boss even without
a file, so recordings catch new bosses), `pollRate` (reads per second).
