import time
import unittest
from struct import pack, pack_into
from unittest.mock import Mock

from sea2 import SAVE
from story import StoryPilot


class StoryPilotTests(unittest.TestCase):
    def setUp(self):
        self.pilot = StoryPilot((SAVE.parent / "ZA_DAT.DAT").read_bytes())
        self.debugger = Mock()
        self.state = bytearray(0x612)
        self.state[9] = 30
        pack_into("<I", self.state, 0x32, 0x3b)

    def test_waits_for_town_input_before_entering_church(self):
        self.debugger.command.return_value = "EV\n88e 148 41de f954"
        self.debugger.read.return_value = bytes(192)
        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state)
        self.debugger.write.assert_not_called()

        self.debugger.read.side_effect = [
            pack("<HH", 0xbb08, 0x19b3) + bytes(188),
            pack("<HH", 0x1e, 0x1e),
        ]
        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state)
        self.debugger.write.assert_any_call(0x41de, 0xcb4, pack("<4H", 0, 49, 12, 14))
        self.debugger.write.assert_any_call(0x40, 0x1e, b"\x00\x48")

    def test_other_hero_receives_no_automated_input(self):
        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 1, self.state)
        self.debugger.command.assert_not_called()
        self.debugger.write.assert_not_called()


    def test_exits_building_menu_after_game_confirms_event(self):
        self.pilot.pending = (2, 10, "去教堂招募恩里科")
        self.pilot.started = time.monotonic()
        pack_into("<I", self.state, 0x32, 0x3f)
        self.debugger.command.return_value = "EV\n19b3 3f46 41de f928"
        self.debugger.read.side_effect = [
            pack("<HHHH", 0x453f, 0x19b3, 0xb5ee, 0x19b3) + bytes(184),
            pack("<HH", 0x1e, 0x1e),
        ]

        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state)

        self.debugger.write.assert_any_call(0x40, 0x1e, b"\x1b\x01")
        self.assertIsNotNone(self.pilot.pending)


    def test_only_harbor_tutorial_accepts_story_choice(self):
        self.pilot.pending = (10, 3, "港口出航教学")
        self.pilot.started = time.monotonic()
        self.debugger.command.return_value = "EV\n19b3 3f46 41de f928"
        frame = pack("<HH", 0x4359, 0x37d2) + bytes(188)
        self.debugger.read.side_effect = [frame, pack("<HH", 0x1e, 0x1e)]
        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state)
        self.debugger.write.assert_any_call(0x40, 0x1e, b"\x0d\x1c")

    def test_harbor_menu_starts_sailing_after_tutorial(self):
        self.pilot.pending = (10, 3, "港口出航教学")
        self.pilot.started = time.monotonic()
        pack_into("<I", self.state, 0x32, 0x43f)
        self.debugger.command.return_value = "EV\n19b3 3f46 41de f928"
        frame = pack("<HH", 0x8c50, 0x29a2) + bytes(188)
        fleet = 0x2e76
        ships = pack("<H7B", 20, 0, 0, 0, 0, 0, 0, 0x10) + bytes(81)
        supply = bytes(9) + bytes((50,)) + bytes(290)
        self.debugger.read.side_effect = [frame, pack("<HH", 0x1e, 0x1e), ships, supply]

        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state, fleet)

        self.debugger.write.assert_any_call(0x40, 0x1e, b"\x0d\x1c")

    def test_story_continues_on_first_voyage(self):
        self.state[0x0a] = 0xff
        self.state[0x31] = 1
        self.debugger.command.return_value = "EV\n9c9 35f5 41de fa22"
        frame = pack("<HH", 0x5113, 0x37d2) + bytes(188)
        self.debugger.read.side_effect = [frame, pack("<HH", 0x1e, 0x1e)]

        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state)

        self.debugger.write.assert_any_call(0x40, 0x1e, b"\x0d\x1c")

    def test_sets_native_destination_once_after_first_voyage_story(self):
        self.state[0x0a] = 0xff
        self.state[0x30] = 1
        self.debugger.command.return_value = "EV\n9c9 35f5 41de fa22"
        frame = pack("<HH", 0xb4b5, 0x19b3) + bytes(188)
        coordinates = pack("<2H", 170, 314)
        self.debugger.read.side_effect = [frame, coordinates, pack("<2H", 118, 358), b"\x20", frame]

        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state, 0x2e76)
        self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state, 0x2e76)

        self.debugger.write.assert_any_call(0x41de, 0x2e7a, coordinates)
        self.debugger.write.assert_any_call(0x41de, 0x1005, b"\x80")
        self.assertEqual(self.debugger.write.call_count, 4)

    def test_confirms_own_port_call_once_and_closes_arrival_report(self):
        self.state[0x0a] = 27
        self.state[0x30] = 1
        self.pilot.destination = 27
        self.debugger.command.return_value = "EV\n9c9 35f5 41de fa22"
        confirmation = pack("<HH", 0xc0ce, 0x19b3) + bytes(188)
        report = pack("<HH", 0xbe5a, 0x19b3) + bytes(188)
        empty_buffer = pack("<HH", 0x1e, 0x1e)
        self.debugger.read.side_effect = [confirmation, empty_buffer, confirmation, report, empty_buffer]

        for _ in range(3):
            self.pilot.next_line = 0
            self.pilot.tick(self.debugger, 0x41de, 0x09c9, 0, self.state, 0x2e76)

        self.assertEqual(self.debugger.write.call_count, 4)
        self.assertTrue(self.pilot.port_confirmed)
