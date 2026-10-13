"""Load someone else's Elden Ring save (e.g. a 100% base game save) as your own, to start the DLC.

A save (ER0000.sl2) is tied to the Steam account that made it: the game won't load a downloaded one as-is. This script
rewrites the save's Steam ID to yours, fixes the file's checksums, backs up your current save, and installs the new one.

    python import_save.py <downloaded ER0000.sl2>
    python import_save.py <downloaded ER0000.sl2> --steam-id 7656119xxxxxxxxxx   (if you have several Steam accounts)
    python import_save.py <downloaded ER0000.sl2> --out fixed.sl2                (write a file, install nothing)
    python import_save.py --info <any .sl2>                                       (what's in a save, changes nothing)

Close the game first. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import sys
from datetime import datetime
from pathlib import Path

SLOT_COUNT = 10            # USER_DATA000-009: the 10 character slots
PROFILE = 10               # USER_DATA010: the profile (Steam ID, which slots are in use)
CHECKSUM = 16              # every entry's data starts with the MD5 of the rest of it
STEAM_ID_AT = CHECKSUM + 4  # the Steam ID inside the profile entry


class SaveError(Exception):
    pass


class Save:
    """An Elden Ring PC save: a BND4 container of 12 entries, each = MD5 checksum (16 bytes) + data."""

    def __init__(self, data: bytes):
        if data[:4] != b"BND4":
            raise SaveError("not an Elden Ring PC save (no BND4 header). PS4/PS5/Xbox saves are a different format.")
        self.data = bytearray(data)
        count = struct.unpack_from("<i", data, 0x0C)[0]
        entry_size = struct.unpack_from("<q", data, 0x20)[0]
        if count < PROFILE + 1 or entry_size < 0x18:
            raise SaveError(f"unexpected save layout ({count} entries).")
        self.entries: list[tuple[int, int]] = []  # (offset, size) of each entry
        for i in range(count):
            h = 0x40 + i * entry_size
            size = struct.unpack_from("<q", data, h + 0x08)[0]
            offset = struct.unpack_from("<I", data, h + 0x18)[0]
            if size < CHECKSUM or offset + size > len(data):
                raise SaveError(f"entry {i} is out of bounds: the file is damaged or not an Elden Ring save.")
            self.entries.append((offset, size))
        bad = [i for i in range(PROFILE + 1) if not self.checksum_ok(i)]
        if bad:
            raise SaveError(f"checksum mismatch in entr{'y' if len(bad) == 1 else 'ies'} {bad}: the file is damaged, "
                            "or was edited without fixing checksums. Nothing was changed.")

    def checksum_ok(self, i: int) -> bool:
        off, size = self.entries[i]
        return hashlib.md5(self.data[off + CHECKSUM:off + size]).digest() == bytes(self.data[off:off + CHECKSUM])

    def fix_checksum(self, i: int) -> None:
        off, size = self.entries[i]
        self.data[off:off + CHECKSUM] = hashlib.md5(self.data[off + CHECKSUM:off + size]).digest()

    @property
    def steam_id(self) -> int:
        return struct.unpack_from("<Q", self.data, self.entries[PROFILE][0] + STEAM_ID_AT)[0]

    def set_steam_id(self, new: int) -> int:
        """Replaces the Steam ID everywhere it appears (the profile and each character slot); returns how many places."""
        old_b, new_b = struct.pack("<Q", self.steam_id), struct.pack("<Q", new)
        changed = 0
        for i in range(PROFILE + 1):
            off, size = self.entries[i]
            start = off + CHECKSUM
            body = bytes(self.data[start:off + size])
            n = body.count(old_b)
            if n:
                self.data[start:off + size] = body.replace(old_b, new_b)
                self.fix_checksum(i)
                changed += n
        if self.steam_id != new:
            raise SaveError("the Steam ID didn't take: unexpected save layout. Nothing was installed.")
        return changed

    def used_slots(self) -> list[int]:
        """Character slots that hold data (a slot never used is all zeros after its checksum)."""
        return [i for i in range(SLOT_COUNT) if any(self.data[self.entries[i][0] + CHECKSUM:self.entries[i][0] + self.entries[i][1]])]


def save_folders() -> list[Path]:
    """Your saves: %APPDATA%\\EldenRing\\<SteamID64>\\ (one folder per Steam account that played)."""
    root = Path(os.environ.get("APPDATA", "")) / "EldenRing"
    return sorted(p for p in root.glob("*") if p.is_dir() and p.name.isdigit() and len(p.name) == 17) if root.is_dir() else []


def pick_folder(steam_id: int | None) -> Path:
    folders = save_folders()
    if steam_id is not None:
        match = [f for f in folders if f.name == str(steam_id)]
        if match:
            return match[0]
        raise SaveError(f"no save folder for Steam ID {steam_id} under %APPDATA%\\EldenRing.")
    if len(folders) == 1:
        return folders[0]
    if not folders:
        raise SaveError("no save folder found under %APPDATA%\\EldenRing: start Elden Ring once (to the title screen) "
                        "so it makes one, close it, then run this again.")
    raise SaveError("several Steam accounts have saves here; pick one with --steam-id: "
                    + ", ".join(f.name for f in folders))


def info(path: Path) -> int:
    save = Save(path.read_bytes())
    print(f"{path}\n  Steam ID: {save.steam_id}\n  character slots in use: {[s + 1 for s in save.used_slots()] or 'none'}"
          "\n  checksums: OK")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Load a downloaded Elden Ring save as your own.")
    ap.add_argument("save", type=Path, nargs="?", help="the downloaded ER0000.sl2")
    ap.add_argument("--steam-id", type=int, help="your SteamID64 (only needed with several Steam accounts)")
    ap.add_argument("--out", type=Path, help="write the converted save here instead of installing it")
    ap.add_argument("--info", type=Path, metavar="SL2", help="show what's in a save and exit")
    a = ap.parse_args(argv)
    try:
        if a.info:
            return info(a.info)
        if not a.save:
            ap.print_help()
            return 1
        save = Save(a.save.read_bytes())
        print(f"Downloaded save: made by Steam ID {save.steam_id}, character slots in use: {[s + 1 for s in save.used_slots()]}")

        if a.out:
            if a.steam_id is None:
                raise SaveError("--out needs --steam-id (whose account the save is for).")
            n = save.set_steam_id(a.steam_id)
            a.out.write_bytes(save.data)
            print(f"Wrote {a.out} for Steam ID {a.steam_id} ({n} places changed). Installed nothing.")
            return 0

        folder = pick_folder(a.steam_id)
        mine = int(folder.name)
        n = save.set_steam_id(mine)
        target = folder / "ER0000.sl2"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for f in (target, folder / "ER0000.sl2.bak"):
            if f.exists():
                backup = f.with_name(f"{f.name}.before-import-{stamp}")
                shutil.copy2(f, backup)
                print(f"Backed up {f.name} -> {backup.name}")
        target.write_bytes(save.data)
        (folder / "ER0000.sl2.bak").write_bytes(save.data)  # the game falls back to .bak; keep them the same
        print(f"Installed for your Steam ID {mine} ({n} places changed) in {folder}")
        print("Start Elden Ring and load the character. To undo: copy the .before-import files back over ER0000.sl2 and .bak.")
        return 0
    except (SaveError, OSError) as ex:
        print(f"Error: {ex}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
