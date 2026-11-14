import os
import pty
import tempfile
import threading
import unittest
from pathlib import Path
from struct import pack, pack_into, unpack
from unittest.mock import Mock

import live


class TrainerTests(unittest.TestCase):
    def test_gold_and_fame_writes_target_the_current_hero(self):
        self.assertEqual(
            live.lock_fields(5, {"gold": 12345678, "piracy": 47002}),
            [(0x12ae, pack("<I", 12345678)), (0x12a2, pack("<H", 47002))],
        )

    def test_limits_and_hero_id(self):
        for hero, fields in ((6, {"gold": 1}), (0, {"gold": -1}), (0, {"adventure": 50001})):
            with self.assertRaises(ValueError):
                live.lock_fields(hero, fields)

    def test_psp_must_belong_to_main(self):
        report = " 0812 1952 0813 KOUKAI2\n 09B8 295504 09B9 MAIN\n 51DE 36864 09B9 MAIN"
        self.assertEqual(live.main_psp(report), 0x09b9)
        with self.assertRaises(ValueError):
            live.main_psp(" 09B8 295504 09B9 OPEN")


class AutoResourcesTests(unittest.TestCase):
    def setUp(self):
        self.state = bytearray(0xc2d6)
        pack_into("<H", self.state, 0xc2d4, 0x2e76)
        self.state[0x12b5] = 1
        self.state[0x2ea1:0x2eaa] = bytes((1, 0, 0, 0, 0, 0, 0, 0, 0x10))
        self.state[0x5892 + 17] = 8
        pack_into("<H", self.state, 0x5892 + 20, 40)
        pack_into("<H", self.state, 0x52d4 + 12, 12)

    def test_replenishes_player_only_and_respects_ship_capacity(self):
        writes = dict(live.refill_writes(self.state, 1))
        self.assertEqual(writes[0x12ae], pack("<I", 10_000_000))
        self.assertEqual(writes[0x1268], pack("<3H", 50000, 50000, 50000))
        self.assertNotIn(0x125a, writes)
        self.assertNotIn(0x52f2, writes)
        water, food, lumber, shot = unpack("<4H", writes[0x52d4])
        self.assertGreaterEqual(min(water, food), 10)
        self.assertLessEqual(water + food + 10 * (lumber + shot + 12), 400)

    def test_auto_refill_leaves_emulation_running(self):
        self.state[live.LIVE_BASE + 6] = 21
        debugger = Mock()
        debugger.command.return_value = ""
        debugger.data_segment.return_value = 0x41de
        debugger.read.return_value = self.state
        game = Mock()
        game.poll.side_effect = [None, 0]

        live.auto_refill(debugger, game)

        debugger.command.assert_not_called()
        debugger.write.assert_any_call(0x41de, live.GOLD, pack("<I", 10_000_000))

    def test_title_screen_with_no_fleet_pointer_is_ignored(self):
        pack_into("<H", self.state, 0xc2d4, 0)
        self.assertEqual(live.refill_writes(self.state, 1), [])

    def test_full_ship_preserves_cargo_and_existing_supplies(self):
        pack_into("<4H", self.state, 0x52d4, 30, 40, 1, 2)
        pack_into("<H", self.state, 0x52d4 + 12, 30)
        writes = dict(live.refill_writes(self.state, 1))
        self.assertNotIn(0x52d4, writes)

    def test_terminal_drain_stops_even_while_slave_is_open(self):
        master, slave = pty.openpty()
        stop = threading.Event()
        def drain():
            try:
                live.drain_terminal(master, stop)
            except TypeError as error:
                errors.append(error)
        errors = []
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            stop.set()
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
        finally:
            os.close(slave)
            reader.join(timeout=1)
            os.close(master)

    def test_sailing_speed_switches_only_on_departure_and_arrival(self):
        self.state[live.LIVE_BASE + 6] = 21
        sailing = bytearray(self.state)
        sailing[live.LIVE_BASE + 10] = 0xff
        debugger = Mock()
        debugger.data_segment.return_value = 0x41de
        debugger.read.side_effect = [self.state, sailing, sailing, self.state]
        game = Mock()
        game.poll.side_effect = [None, None, None, None, 0]

        live.auto_refill(debugger, game)

        speed_writes = [call.args for call in debugger.write.call_args_list if call.args[1] == 0xb2b7]
        self.assertEqual(speed_writes, [(0x19c9, 0xb2b7, b"\x90\x90"), (0x19c9, 0xb2b7, b"\x75\xf7")])
        debugger.command.assert_not_called()

    def test_patch_backup_and_repeat_launch(self):
        original = live.SAVE.with_name("MAIN.EXE").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "MAIN.EXE"
            exe.write_bytes(original)
            live.prepare_provisions(exe)
            live.prepare_provisions(exe)
            self.assertEqual(exe.read_bytes(), live.patch_provisions(original))
            self.assertEqual(exe.with_suffix(".original.EXE").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
