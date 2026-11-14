import unittest
from struct import pack_into, unpack_from

from live import build_trainer_main
from sea2 import SAVE, SLOT_SIZE
from story_destinations import CoastMap, read_ports, route_targets


class StoryDestinationTests(unittest.TestCase):
    def test_route_switch_installs_live_coast_and_port_hooks(self):
        original = SAVE.with_name("MAIN.EXE").read_bytes()
        route = build_trainer_main(original, {"route"})
        port_only = build_trainer_main(original, {"teleport"})
        header = unpack_from("<H", route, 8)[0] * 16
        self.assertEqual(route[header + 0x20a], 0xe9)
        self.assertIn(bytes.fromhex("c606ae0cff"), route[header + 0x2d0:header + 0x300])
        port_header = unpack_from("<H", port_only, 8)[0] * 16
        self.assertEqual(port_only[port_header + 0x20a:port_header + 0x20f], bytes.fromhex("31c0a0ae0c"))

    def test_pierre_medal_uses_this_maps_coastal_coordinates(self):
        state = bytearray(SLOT_SIZE)
        state[0x30:0x32] = bytes((1, 1))
        state[0x611] = 4
        state[0x21b5] = 87
        pack_into("<HHBBB", state, 0x72e8, 296, 862, 97, 40, 0xa4)
        coast = CoastMap(SAVE.with_name("WORLDMAP.LZW").read_bytes(), SAVE.with_name("MAIN.EXE").read_bytes())
        bank = SAVE.with_name("SNR5.DAT").read_bytes()
        result = route_targets(state, bank, read_ports(), coast)
        self.assertEqual(result["直达藏宝点 · 金质奖章（296, 862）"], (296, 864))
        self.assertEqual(result["金质奖章 · 开普敦出海后向东 10 格（296, 862）"], 102)
        self.assertEqual(result["剧情候选 · 里斯本 · 法雷尔公爵宅"], 0)
        state[0x21b6] = 97
        self.assertFalse(any("金质奖章" in label for label in route_targets(state, bank, read_ports(), coast)))

    def test_fixed_routes_read_all_six_scenario_banks(self):
        for hero in range(6):
            with self.subTest(hero=hero):
                bank = SAVE.with_name(f"SNR{hero + 1}.DAT").read_bytes()
                state = bytearray(SLOT_SIZE)
                state[0x611] = hero
                ports = read_ports()
                self.assertTrue(route_targets(state, bank, ports))
