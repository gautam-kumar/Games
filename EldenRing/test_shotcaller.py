"""Scenario tests for shotcaller.py (no game): python -m unittest test_shotcaller.  The IDs are made up; real ones come
from recordings."""

import unittest
from datetime import datetime, timedelta

import shotcaller as sc

T0 = datetime(2026, 10, 13, 21, 0, 0)

BOSS = sc.Boss.parse(sc.load_jsonc("""
{
    // comments and trailing commas are fine
    "name": "Test Boss",
    "npcParamIds": [100],
    "phases": [ { "phase": 2, "hpBelow": 0.5, "say": "Phase two" } ],
    "calls": [
        { "anim": 3000, "text": "Roll late", "say": "late", "delay": 0.5 },
        { "anim": 3001, "text": "Run", "phase": 2 },
        { "anim": 3002, "text": "Quiet // not a comment", "say": "-" },
    ]
}
"""))


class Host(sc.Host):
    def __init__(self):
        self.calls, self.notes = [], []

    def callout(self, text, say, at, until):
        self.calls.append(((at - T0).total_seconds(), text, say))

    def note(self, text):
        self.notes.append(text)


def ev(t, kind, actor, value=0, mx=0):
    return sc.Event(T0 + timedelta(seconds=t), kind, actor, "You" if actor == 0 else "Test Boss", value, mx)


class Recording(unittest.TestCase):
    def test_round_trip(self):
        e = sc.Event(T0 + timedelta(milliseconds=250), "Anim", 100, "Test Boss", 3000, 0, 1.5, -2.25, 30.0)
        line = sc.format_event(e)
        self.assertEqual("21:00:00.250 Anim Test Boss[100] anim=3000 pos=(1.50,-2.25,30.00)", line)
        self.assertEqual(e, sc.parse_line(line, T0))
        note = sc.Event(T0, "Note", text="Fight Test Boss")
        self.assertEqual(note, sc.parse_line(sc.format_event(note), T0))

    def test_split_pulls(self):
        lines = [sc.format_event(e) for e in [
            ev(0, "Hp", 0, 900, 1000),  # before any boss: dropped
            ev(1, "Boss", 100, 5000, 5000), ev(2, "Anim", 100, 3000), ev(3, "Dead", 0),
            ev(10, "Boss", 100, 5000, 5000), ev(40, "Dead", 100),
        ]]
        pulls = sc.split_pulls(lines)
        self.assertEqual(2, len(pulls))
        self.assertEqual(("Dead", 3), (pulls[0].end, len(pulls[0].events)))
        self.assertEqual("Kill", pulls[1].end)


class Runner(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.runner = sc.FightRunner([BOSS], self.host)
        self.runner.on(ev(0, "Boss", 100, 5000, 5000))

    def test_calls_fire_delayed_and_only_in_their_phase(self):
        r = self.runner
        r.on(ev(1, "Anim", 100, 3000))
        self.assertEqual([], self.host.calls)  # 0.5 s delay
        r.tick(T0 + timedelta(seconds=1.5))
        r.on(ev(2, "Anim", 100, 3001))  # phase 2 only
        r.on(ev(3, "Anim", 100, 3002))
        r.on(ev(4, "Hp", 100, 2400, 5000))  # below 50%
        r.on(ev(5, "Anim", 100, 3001))
        self.assertEqual([(1.5, "Roll late", "late"), (3.0, "Quiet // not a comment", None), (4.0, "Phase 2", "Phase two"), (5.0, "Run", "Run")],
                         self.host.calls)
        self.assertEqual(["Fight Test Boss", "Phase 2"], self.host.notes)

    def test_cooldown(self):
        for t in (1, 1.5, 3):
            self.runner.on(ev(t, "Anim", 100, 3002))
        self.assertEqual([1.0, 3.0], [c[0] for c in self.host.calls])

    def test_death_ends_the_fight_and_drops_waiting_calls(self):
        r = self.runner
        r.on(ev(1, "Anim", 100, 3000))
        r.on(ev(1.2, "Dead", 0))
        r.tick(T0 + timedelta(seconds=5))
        r.on(ev(6, "Anim", 100, 3000))
        r.tick(T0 + timedelta(seconds=8))
        self.assertEqual([], self.host.calls)
        self.assertEqual(["Fight Test Boss", "Wipe Test Boss"], self.host.notes)
        self.assertIsNone(r.boss)

    def test_unknown_boss_is_ignored(self):
        host = Host()
        r = sc.FightRunner([BOSS], host)
        r.on(ev(0, "Boss", 999, 5000, 5000))
        r.on(ev(1, "Anim", 999, 3002))
        self.assertEqual(([], []), (host.calls, host.notes))


class Differ(unittest.TestCase):
    def test_frames_to_events(self):
        d = sc.EventDiffer(lambda c: c.max_hp >= 5000)
        me = sc.Chr(0, "You", 0, 1000, 1000)
        boss = sc.Chr(100, "Test Boss", 1, 5000, 5000, 0, 0, 10)
        add = sc.Chr(200, "Add", 1, 300, 300, 0, 0, 5)
        self.assertEqual([("Boss", 100)], [(e.kind, e.actor) for e in d.diff(T0, [me, boss, add])])
        second = d.diff(T0, [sc.Chr(0, "You", 0, 0, 1000), sc.Chr(100, "Test Boss", 3000, 5000, 5000, 0, 0, 10), add])
        self.assertEqual([("Hp", 0, 0), ("Dead", 0, 0), ("Anim", 100, 3000)], [(e.kind, e.actor, e.value) for e in second])
        d.diff(T0, [me])  # boss unloaded
        self.assertEqual(["Boss"], [e.kind for e in d.diff(T0, [me, boss])])  # back: a new pull


class Aob(unittest.TestCase):
    def test_find_and_rip(self):
        code = bytes([0x90, 0x90, 0x48, 0x8B, 0x05, 0x10, 0x00, 0x00, 0x00, 0x48, 0x85, 0xC0])  # mov rax,[rip+0x10]; test rax,rax
        at = sc.aob_find(code, "48 8B 05 ?? ?? ?? ?? 48 85 C0")
        self.assertEqual(2, at)
        self.assertEqual(0x1000 + 2 + 7 + 0x10, sc.resolve_rip(code, 0x1000, at, 3, 7))
        self.assertEqual(-1, sc.aob_find(code, "48 8B 0D"))
        self.assertEqual(1, sc.aob_find(bytes([0x0A, 0x0A, 0x2E]), "?? 2E"))  # ?? matches any byte, newline included


class Files(unittest.TestCase):
    def test_shipped_files_parse(self):
        self.assertEqual("Boss name", sc.Boss.parse(sc.load_jsonc((sc.HOME / "bosses" / "_template.json").read_text())).name)
        self.assertNotIn("Boss name", [b.name for b in sc.load_bosses(sc.HOME / "bosses")])
        self.assertEqual(sc.OFFSET_FIELDS, sc.missing_offsets(sc.load_offsets()))  # shipped empty: nothing guessed
        self.assertTrue(sc.load_settings()["voice"])


class Replay(unittest.TestCase):
    def test_replay_end_to_end(self):
        import io, tempfile, contextlib
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test.json").write_text('{"name": "Test Boss", "npcParamIds": [100], "calls": [{"anim": 3000, "text": "Roll", "delay": 0.5}]}')
            rec = Path(tmp, "rec.log")
            rec.write_text("\n".join(sc.format_event(e) for e in [ev(0, "Boss", 100, 5000, 5000), ev(1, "Anim", 100, 3000), ev(1.2, "Pos", 100)]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, sc.main(["replay", str(rec), "--bosses", tmp]))
            self.assertIn("1.50  CALL  Roll  for 3.0 s", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                sc.main(["replay", str(rec), "--anims"])
            self.assertIn("anim     3000  x1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
