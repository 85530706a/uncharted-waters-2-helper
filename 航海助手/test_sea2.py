import tempfile
import unittest
from pathlib import Path
from struct import pack_into

import sea2


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.original = bytearray(328991)
        for slot in range(10):
            header = slot * 15
            self.original[header + 1:header + 12] = b"20/  7/1522"
            self.original[header + 13] = 82
            self.original[header + 14] = 2
            base = 151 + slot * 32884
            self.original[base + 0xa] = 82
            self.original[base + 0x611] = slot % 6
            pack_into("<I", self.original, base + 0x60a, 1000 + slot)

    def test_reads_all_slots(self):
        slots = sea2.read_slots(self.original)
        self.assertEqual(len(slots), 10)
        self.assertEqual((slots[9].hero, slots[9].port, slots[9].gold), (3, 82, 1009))

    def test_edits_only_requested_gold_and_fame_in_selected_slot(self):
        before = bytes(self.original)
        result = sea2.edit_slot(self.original, 10, gold=12345678, fame={"piracy": 47002})
        expected = bytearray(before)
        base = 151 + 9 * 32884
        pack_into("<I", expected, base + 0x60a, 12345678)
        pack_into("<H", expected, base + 0x5b6 + 3 * 14 + 2, 47002)
        self.assertEqual(result, expected)
        self.assertEqual(self.original, before)
        self.assertEqual(sea2.read_slots(result)[-1].fame, (0, 47002, 0))

    def test_rejects_wrong_version_empty_slot_and_out_of_range_values(self):
        with self.assertRaises(ValueError):
            sea2.read_slots(bytes(319591))
        self.original[1:12] = bytes(11)
        with self.assertRaises(ValueError):
            sea2.edit_slot(self.original, 1, gold=10)
        with self.assertRaises(ValueError):
            sea2.edit_slot(self.original, 2, fame={"trade": 50001})

    def test_copy_never_overwrites_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "KOUKAI2.DAT"
            output = Path(folder) / "edited.DAT"
            source.write_bytes(self.original)
            sea2.write_copy(source, output, b"copy")
            self.assertEqual(output.read_bytes(), b"copy")
            self.assertEqual(source.read_bytes(), self.original)
            with self.assertRaises(FileExistsError):
                sea2.write_copy(source, output, b"another")
            with self.assertRaises(ValueError):
                sea2.write_copy(source, source, b"another")

class ProvisionTests(unittest.TestCase):
    def test_changes_only_the_two_daily_deductions(self):
        image = sea2.SAVE.with_name("MAIN.EXE").read_bytes()
        patched = sea2.patch_provisions(image)
        expected = bytearray(image)
        expected[0x1e2a9:0x1e2ac] = b"\x90" * 3
        expected[0x1e35b:0x1e35d] = b"\x90" * 2
        self.assertEqual(patched, expected)

    def test_rejects_unknown_and_already_patched_programs(self):
        image = sea2.SAVE.with_name("MAIN.EXE").read_bytes()
        with self.assertRaises(ValueError):
            sea2.patch_provisions(image[:-1])
        with self.assertRaises(ValueError):
            sea2.patch_provisions(sea2.patch_provisions(image))


class StoryTests(unittest.TestCase):
    def test_all_seven_chinese_script_banks(self):
        root = sea2.SAVE.parent
        counts = [13, 6, 8, 6, 5, 4, 6]
        for bank, count in enumerate(counts):
            with self.subTest(bank=bank):
                data = (root / f"SNR{bank}.DAT").read_bytes()
                chapters = sea2.story_sections(data)
                self.assertEqual(len(chapters), count)
                self.assertTrue(sea2.story_entries(data, 0, 0))

    def test_joao_first_chapter_uses_actual_port_and_building_keys(self):
        data = sea2.SAVE.with_name("SNR1.DAT").read_bytes()
        routes = sea2.story_entries(data, 0, 0)
        keys = {(route["selector"], route["qualifier"]) for route in routes}
        self.assertIn((0, 1), keys)
        self.assertIn((0, 3), keys)
        self.assertEqual(sea2.story_entries(data, 255, 0), [])


if __name__ == "__main__":
    unittest.main()
