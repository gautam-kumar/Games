# EldenRing

`import_save.py`: load a downloaded Elden Ring save (e.g. 100% base game done) as your own, to go straight to the DLC.
Python 3, standard library only, Windows PC (Steam).

## Steps

1. Close Elden Ring. If you've never started it on this PC, start it once to the title screen and close it (that makes
   your save folder).
2. Download a completed PC save (`ER0000.sl2`), e.g. a "100% / DLC ready" save from Nexus Mods. Pick one where Radahn
   and Mohg are beaten and the items aren't cheated.
3. Run:
   ```
   python import_save.py "C:\Users\<you>\Downloads\ER0000.sl2"
   ```
   It checks the file, backs up your current save (`ER0000.sl2.before-import-<date>`), changes the save's Steam ID to
   yours, fixes the checksums, and installs it in `%APPDATA%\EldenRing\<your Steam ID>\`.
4. Start the game, load the character, go to Mohg's arena (Mohgwyn Palace) and touch the withered arm to enter the DLC.

Several Steam accounts on this PC: add `--steam-id <your SteamID64>`. `--info <file.sl2>` shows a save's Steam ID and
used slots without changing anything. Undo: copy the `.before-import-*` files back over `ER0000.sl2` and `ER0000.sl2.bak`.

The whole save is replaced (all character slots come from the downloaded file). If the file's checksums don't check
out, the script stops and changes nothing.
