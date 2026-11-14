import unittest
from struct import pack_into, unpack_from
from tempfile import TemporaryDirectory
from pathlib import Path

from live import (
    FLEET_POINTER, GOLD, FAME_BASE, LIVE_BASE, SAVE, build_trainer_main, prepare_trainer,
    trainer_chart_writes, trainer_discovery_writes, trainer_writes,
)
from original_game import run_original
from sea2 import read_slots


class TrainerTests(unittest.TestCase):
    def test_discoveries_enable_villages_and_keep_report_and_treasure_flags(self):
        state = bytearray(FLEET_POINTER + 2)
        start = LIVE_BASE + 0x72e8
        for index, (item, flags) in enumerate(((0, 0x03), (0, 0x85), (0, 0x71), (97, 0xa4))):
            pack_into("<HHBBB", state, start + index * 7, 296, 862, item, 40, flags)
        writes = trainer_discovery_writes(state)
        self.assertEqual(writes, [(start + 6, b"\x63")])
        for offset, value in writes:
            state[offset] = value[0]
        self.assertEqual(trainer_discovery_writes(state), [])

    def test_discoveries_restore_misclassified_royal_treasure(self):
        state = bytearray(FLEET_POINTER + 2)
        start = LIVE_BASE + 0x72e8
        pack_into("<HHBBB", state, start, 296, 862, 97, 40, 0x74)
        pack_into("<HHBBB", state, start + 7, 300, 862, 98, 40, 0xb4)
        self.assertEqual(trainer_discovery_writes(state), [(start + 6, b"\x84")])

    def setUp(self):
        self.original = SAVE.with_name("MAIN.EXE").read_bytes()

    def test_empty_and_money_only_leave_executable_intact(self):
        for features in (set(), {"gold", "trade"}):
            self.assertEqual(build_trainer_main(self.original, features), self.original)

    def test_each_combat_switch_controls_its_own_gate(self):
        gates = {"boarding": (0x83,), "sea_guard": (0xd3, 0xea),
                 "fast": (0x110,), "hazards": (0x121, 0x134, 0x141, 0x150, 0x173)}
        for feature in gates:
            with self.subTest(feature=feature):
                image = build_trainer_main(self.original, {feature})
                header = unpack_from("<H", image, 8)[0] * 16
                for name, offsets in gates.items():
                    for offset in offsets:
                        self.assertEqual(image[header + offset:header + offset + 5],
                                         bytes.fromhex("38c0909090" if feature == name else "803eb512ff"))

    def test_money_and_reputation_work_without_fleet(self):
        state = bytearray(FLEET_POINTER + 2)
        pack_into("<I", state, GOLD, 900)
        self.assertEqual(trainer_writes(state, 4, {"gold": 1000, "adventure": 75}),
                         [(GOLD, b"\xe8\x03\x00\x00"), (FAME_BASE + 4 * 14 + 4, b"K\x00")])
        self.assertEqual(trainer_writes(state, 4, {"gold": 500, "supplies": True}), [])

    def test_switching_profiles_restores_stock_program_and_keeps_save(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            save = folder / SAVE.name
            save.write_bytes(b"existing save")
            palace = folder / "SNR0.DAT"
            palace.write_bytes(b"old palace patch")
            prepare_trainer(folder, {"supplies", "daylight", "opening", "teleport", "duel", "discoveries"})
            self.assertEqual(palace.read_bytes(), SAVE.with_name(palace.name).read_bytes())
            self.assertNotEqual((folder / "MAIN.EXE").read_bytes(), self.original)
            prepare_trainer(folder, set())
            for name in ("MAIN.EXE", "OPEN.EXE", "KOUKAI2.COM", "SNR0.DAT", "SNR1.DAT", "SNR2.DAT"):
                self.assertEqual((folder / name).read_bytes(), (SAVE.parent / name).read_bytes())
            self.assertEqual(save.read_bytes(), b"existing save")

    def test_original_launch_copies_program_but_starts_with_an_empty_save(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            emulator = folder / "dosbox-x"
            emulator.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > launch-args\n')
            emulator.chmod(0o700)
            session = folder / "original"
            self.assertEqual(run_original(str(emulator), SAVE.parent, session), 0)
            self.assertEqual((session / "MAIN.EXE").read_bytes(), self.original)
            self.assertEqual(read_slots((session / SAVE.name).read_bytes()), [])
            self.assertIn("dosbox mcp_server=0", (session / "launch-args").read_text())
            save = session / SAVE.name
            save.write_bytes(b"new player save")
            self.assertEqual(run_original(str(emulator), SAVE.parent, session), 0)
            self.assertEqual(save.read_bytes(), b"new player save")

    def test_chart_reveals_matching_ports_and_preserves_existing_flags(self):
        state = bytearray(FLEET_POINTER + 2)
        record = LIVE_BASE + 0x52b0 + 22
        pack_into("<HH", state, record, 12, 34)
        state[record + 21] = 0x40
        writes = dict(trainer_chart_writes(state, [(1, 12, 34), (2, 56, 78)]))
        self.assertEqual(writes[record + 21], b"\x50")
        self.assertNotIn(record + 43, writes)
        for offset, data in writes.items():
            state[offset:offset + len(data)] = data
        self.assertEqual(trainer_chart_writes(state, [(1, 12, 34)]), [])
