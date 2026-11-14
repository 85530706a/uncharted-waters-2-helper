import hashlib
import unittest
from struct import unpack_from

from patches import (
    build_trainer_main, move_patches_out_of_video_buffer, patch_ali_combat, patch_ali_voyage,
    patch_opening_enter, pin_navigation_ports, protect_ali_ships,
)
from sea2 import SAVE, patch_provisions

VIDEO_BUFFER = 0x3b840

# SHA-256 of the trainer's MAIN.EXE for representative switch sets, recorded
# from the version played through with every hero. Update a value only when a
# patch is changed on purpose and has been checked in the game again.
TRAINER_MAIN = {
    ("pins",): "09957cfdda4ae0a0b471fc1075e3004819f7137339fcbfaf8b13261c57445da2",
    ("supplies",): "2c6aa6f094955912c71c13bbb17edc2b3069141ff4f665cb56c3c97530d727d5",
    ("daylight",): "2cf6cc4c5e37ed09eac37a2992892e70609a75535f7645442c037e8f25d4e922",
    ("teleport",): "2152d4da3c6fe2b28d81d868f871c5cc4610e07a306aaf4de2f0070c59ad4549",
    ("route",): "bac481a2f121ebfca4f0f7ce6b929a9307436ded8789333be70dac6b13fa4cca",
    ("cannon",): "65e2806a375b9c0e93074aec01283b6c6556a6e22c17bbdfb1fb68a62b78154d",
    ("supplies", "cannon"): "8bd7accf108d7bf4082ce26f014284bf7b01d3393bb177936ddafc9c65e1ffe3",
    ("boarding", "sea_guard", "fast", "hazards", "duel", "duel_guard"):
        "5bc73bb53880436345ebb3be23fd002b7b6c0d12f41dc2ae859553f16990a5f5",
    ("supplies", "teleport", "daylight", "pins"):
        "a55d46b8ecd0f438d1e3f7afc4debe101a7e9794fac2c0e15d3ac4ef231c5e9a",
    ("boarding", "sea_guard", "fast", "hazards", "supplies", "cannon", "duel", "duel_guard", "teleport",
     "daylight", "opening", "pins", "map", "route", "discoveries"):
        "46678c8855af7015e9d9fc222797f830a9b16b3af4a55d466417e3c8e552e6ff",
}
OPENING = "1e52837296d317c127d2a151162710f67e2588310c763cf1098f14b729cdff4c"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def exe_layout(image):
    """Header size, relocation entries, CS, SS and the size the MZ header declares."""
    tail, pages, count, paragraphs = unpack_from("<HHHH", image, 2)
    table = unpack_from("<H", image, 0x18)[0]
    relocations = [unpack_from("<HH", image, table + index * 4) for index in range(count)]
    declared = pages * 512 - (512 - tail if tail else 0)
    return {
        "header": paragraphs * 16, "table": table, "relocations": relocations,
        "ss": unpack_from("<H", image, 0x0e)[0], "cs": unpack_from("<H", image, 0x16)[0],
        "declared": declared,
    }


class TrainerMainRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = SAVE.with_name("MAIN.EXE").read_bytes()

    def test_known_switch_sets_produce_the_recorded_programs(self):
        for features, expected in TRAINER_MAIN.items():
            with self.subTest(features=features):
                self.assertEqual(digest(build_trainer_main(self.original, set(features))), expected)

    def test_opening_patch_produces_the_recorded_program(self):
        self.assertEqual(digest(patch_opening_enter(SAVE.with_name("OPEN.EXE").read_bytes())), OPENING)


class VideoBufferMoveTests(unittest.TestCase):
    """Structural checks that hold for every combination the trainer builds."""

    @classmethod
    def setUpClass(cls):
        original = SAVE.with_name("MAIN.EXE").read_bytes()
        voyage = patch_ali_voyage(protect_ali_ships(patch_ali_combat(patch_provisions(original))))
        teleport = {"town_exit": True, "direct": True, "repair_massawa": True}
        cls.original = original
        cls.cases = {
            "pins only": (pin_navigation_ports(original), {}),
            "voyage": (voyage, {}),
            "cannon and ammunition": (voyage, {"cannon": True, "ammunition": True}),
            "teleport": (voyage, teleport),
            "teleport and coast": (voyage, {**teleport, "coast": True}),
            "everything": (voyage, {**teleport, "coast": True, "cannon": True, "ammunition": True}),
        }
        cls.results = {name: move_patches_out_of_video_buffer(image, original, **options)
                       for name, (image, options) in cls.cases.items()}

    def each(self):
        return [(name, image, self.results[name], exe_layout(image), exe_layout(self.results[name]))
                for name, (image, _) in self.cases.items()]

    def prefix_paragraphs(self, before, after, result):
        prefix = len(result) - after["header"] - (len(self.original) - before["header"])
        self.assertEqual(prefix % 16, 0)
        return prefix // 16

    def test_header_declares_the_real_file_size(self):
        for name, _, result, _, after in self.each():
            with self.subTest(name):
                self.assertEqual(after["declared"], len(result))
                self.assertLessEqual(after["table"] + len(after["relocations"]) * 4, after["header"])

    def test_entry_and_stack_segments_move_with_the_prefix(self):
        for name, image, result, before, after in self.each():
            with self.subTest(name):
                shift = self.prefix_paragraphs(before, after, result)
                self.assertGreater(shift, 0)
                self.assertEqual(after["cs"], before["cs"] + shift)
                self.assertEqual(after["ss"], before["ss"] + shift)

    def test_original_relocations_move_with_the_prefix(self):
        for name, image, result, before, after in self.each():
            with self.subTest(name):
                shift = self.prefix_paragraphs(before, after, result)
                self.assertGreaterEqual(len(after["relocations"]), len(before["relocations"]))
                for (offset, segment), (new_offset, new_segment) in zip(before["relocations"], after["relocations"]):
                    self.assertEqual((new_offset, new_segment), (offset, segment + shift))
                    old_word, = unpack_from("<H", image, before["header"] + segment * 16 + offset)
                    new_word, = unpack_from("<H", result, after["header"] + new_segment * 16 + new_offset)
                    if new_word != old_word + shift:
                        # Hooks reuse a relocation for their far call into the patch area,
                        # which sits in the first `shift` paragraphs of the load image.
                        self.assertLess(new_word, shift)

    def test_every_relocation_stays_inside_the_load_image(self):
        for name, _, result, _, after in self.each():
            with self.subTest(name):
                size = len(result) - after["header"]
                for offset, segment in after["relocations"]:
                    self.assertLess(segment * 16 + offset + 2, size)

    def test_graphics_workspace_matches_the_unmodified_game(self):
        for name, image, result, before, after in self.each():
            with self.subTest(name):
                shift = self.prefix_paragraphs(before, after, result)
                moved = VIDEO_BUFFER - before["header"] + after["header"] + shift * 16
                # The workspace used to hold the voyage patches; it must be the stock bytes again.
                self.assertEqual(result[moved:moved + 0x100], self.original[VIDEO_BUFFER:VIDEO_BUFFER + 0x100])


if __name__ == "__main__":
    unittest.main()
