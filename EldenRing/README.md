# EldenRing

Boss callouts for Elden Ring, modelled on Shotcaller (the FFXIV fight-callout plugin in `roricthorne/FFXIV`, folder `Shotcaller/`).

Shotcaller's shape, for reference:
- **Fight logic**: reads the boss's casts and state, then turns them into callouts (spoken and on-screen).
- **Recorder + Replay**: records pulls; a CLI runs the fight rules on a recording without the game. Every change is checked with it.
- **Render**: a separate library that draws the callouts and floor marks.

Elden Ring has no plugin framework like Dalamud, and it runs Easy Anti-Cheat online: anything that reads or changes the
game's memory must run offline with EAC disabled, or it risks a ban.

How the game's state is read is not decided yet. No code until the user says "Go".
