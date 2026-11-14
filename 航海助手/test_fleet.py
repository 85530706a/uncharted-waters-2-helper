import random
import unittest
from struct import pack_into, unpack_from

import layout as L
from live import outfit_fleet_save
from sea2 import read_slots

SAVE_SIZE = L.HEADER_SIZE + L.SLOT_SIZE * L.SLOT_COUNT
SLOT = 3
HERO = 2
FLEET_ID = 5


def slot_base(number=SLOT):
    return L.HEADER_SIZE + (number - 1) * L.SLOT_SIZE


def officer(index):
    return slot_base() + L.OFFICERS + index * L.OFFICER_SIZE


def fleet():
    return slot_base() + L.FLEETS + FLEET_ID * L.FLEET_SIZE


def ship_slot(index):
    return fleet() + L.FLEET_SHIPS + index * L.SHIP_SLOT_SIZE


def supply(index):
    return slot_base() + L.SUPPLIES + index * L.SUPPLY_SIZE


def ship(instance):
    return slot_base() + L.SHIPS + instance * L.SHIP_SIZE


def make_save(*, owned=1, recruits=41):
    """A save whose third slot holds Otto's fleet with `owned` ships."""
    data = bytearray(SAVE_SIZE)
    header = (SLOT - 1) * L.SLOT_HEADER_SIZE
    data[header + L.SLOT_HEADER_DATE.start:header + L.SLOT_HEADER_DATE.stop] = b"20/  7/1522"
    data[header + L.SLOT_HEADER_PORT] = 1
    base = slot_base()
    data[base + L.HERO] = HERO
    for index in range(L.OFFICER_COUNT):
        data[officer(index) + L.OFFICER_FLEET] = 0xff
    data[officer(HERO) + L.OFFICER_FLEET] = FLEET_ID
    for index in range(L.RECRUIT_FIRST, L.OFFICER_COUNT):
        # Only officers whose +45 stays under 100 can be recruited.
        data[officer(index) + 45] = 10 if index < L.RECRUIT_FIRST + recruits else 100
    data[fleet() + L.FLEET_OWNER] = HERO
    for instance in range(L.SHIP_COUNT):
        data[ship(instance) + L.SHIP_TYPE] = 0xff
    for index in range(L.FLEET_SHIP_LIMIT):
        data[supply(index) + L.SUPPLY_CAPTAIN] = 0xff
    for index in range(owned):
        instance = 10 + index
        pack_into("<H", data, ship_slot(index), 30)
        data[ship_slot(index) + L.SHIP_SLOT_INSTANCE] = instance
        data[ship_slot(index) + L.SHIP_SLOT_STATUS] = L.SHIP_SLOT_ACTIVE | 0x01
        name = f"Old {index}".encode().ljust(15, b"\0")
        data[ship(instance):ship(instance) + 15] = name
        data[ship(instance) + L.SHIP_TYPE] = 3
        pack_into("<H", data, ship(instance) + L.SHIP_CAPACITY, 100)
        pack_into("<5H", data, supply(index) + L.SUPPLY_CARGO, 5, 0, 0, 0, 0)
    data[supply(0) + L.SUPPLY_CAPTAIN] = HERO
    return bytes(data)


def fleet_ships(data):
    ships = []
    for index in range(L.FLEET_SHIP_LIMIT):
        offset = ship_slot(index)
        if data[offset + L.SHIP_SLOT_STATUS] & L.SHIP_SLOT_STATE_MASK == L.SHIP_SLOT_ACTIVE:
            ships.append(index)
    return ships


class OutfitFleetTests(unittest.TestCase):
    def setUp(self):
        random.seed(0)

    def test_fills_requested_ships_with_crew_captains_and_loadout(self):
        before = make_save()
        result = outfit_fleet_save(before, SLOT, ships=10, crew=120)

        self.assertEqual(fleet_ships(result), list(range(10)))
        instances = [result[ship_slot(index) + L.SHIP_SLOT_INSTANCE] for index in range(10)]
        self.assertEqual(len(set(instances)), 10)
        self.assertTrue(all(instance < L.SHIP_PLAYER_LIMIT for instance in instances))
        captains = [result[supply(index) + L.SUPPLY_CAPTAIN] for index in range(10)]
        self.assertEqual(captains[0], HERO)
        self.assertEqual(len(set(captains)), 10)
        for index, (instance, captain) in enumerate(zip(instances, captains)):
            with self.subTest(ship=index):
                self.assertEqual(unpack_from("<H", result, ship_slot(index))[0], 120)
                self.assertEqual(result[officer(captain) + L.OFFICER_FLEET], FLEET_ID)
                abilities = officer(captain) + L.OFFICER_ABILITIES
                self.assertEqual(result[abilities:abilities + 8], bytes([100]) * 8)
                self.assertEqual(result[ship(instance) + L.SHIP_CANNONS], 40)
                capacity, = unpack_from("<H", result, ship(instance) + L.SHIP_CAPACITY)
                cargo = sum(unpack_from("<5H", result, supply(index) + L.SUPPLY_CARGO))
                water, food, lumber, shot = unpack_from("<4H", result, supply(index))
                self.assertGreaterEqual(capacity, 260)
                # Water and food count in tenths of a barrel; everything must fit in the hold.
                self.assertGreater(water, 0)
                self.assertLessEqual(water + food + (lumber + shot + cargo) * 10, capacity * 10)
                if captain != HERO:
                    self.assertGreaterEqual(captain, L.RECRUIT_FIRST)

    def test_keeps_flagship_identity_status_bits_and_cargo(self):
        before = make_save()
        result = outfit_fleet_save(before, SLOT, ships=4, crew=50)
        self.assertEqual(result[ship_slot(0) + L.SHIP_SLOT_INSTANCE], 10)
        self.assertEqual(result[ship(10):ship(10) + 15], b"Old 0".ljust(15, b"\0"))
        self.assertEqual(result[ship_slot(0) + L.SHIP_SLOT_STATUS], L.SHIP_SLOT_ACTIVE | 0x01)
        self.assertEqual(unpack_from("<5H", result, supply(0) + L.SUPPLY_CARGO), (5, 0, 0, 0, 0))
        self.assertEqual(result[ship(result[ship_slot(1) + L.SHIP_SLOT_INSTANCE]):][:7], b"Reale 2")

    def test_changes_only_the_selected_slot(self):
        before = make_save()
        result = outfit_fleet_save(before, SLOT, ships=10, crew=150)
        start, end = slot_base(), slot_base() + L.SLOT_SIZE
        self.assertEqual(len(result), len(before))
        self.assertEqual(result[:start], before[:start])
        self.assertEqual(result[end:], before[end:])
        self.assertEqual(read_slots(result), read_slots(before))

    def test_keeps_every_owned_ship_when_fewer_are_requested(self):
        before = make_save(owned=6)
        result = outfit_fleet_save(before, SLOT, ships=3, crew=80)
        self.assertEqual(fleet_ships(result), list(range(6)))
        for index in range(6):
            self.assertEqual(result[ship_slot(index) + L.SHIP_SLOT_INSTANCE], 10 + index)
            self.assertEqual(unpack_from("<H", result, ship_slot(index))[0], 80)

    def test_repeating_the_outfit_keeps_the_same_fleet(self):
        once = outfit_fleet_save(make_save(), SLOT, ships=10, crew=150)
        twice = outfit_fleet_save(once, SLOT, ships=10, crew=150)
        for index in range(10):
            self.assertEqual(twice[ship_slot(index):ship_slot(index) + L.SHIP_SLOT_SIZE],
                             once[ship_slot(index):ship_slot(index) + L.SHIP_SLOT_SIZE])
            self.assertEqual(twice[supply(index) + L.SUPPLY_CAPTAIN], once[supply(index) + L.SUPPLY_CAPTAIN])

    def test_rejects_saves_it_cannot_outfit_safely(self):
        cases = {
            "ship count": (make_save(), {"ships": 11}),
            "crew": (make_save(), {"crew": 151}),
            "empty slot": (make_save(), {"number": 1}),
            "not enough recruits": (make_save(recruits=3), {}),
        }
        no_fleet = bytearray(make_save())
        no_fleet[officer(HERO) + L.OFFICER_FLEET] = 0xff
        cases["no fleet"] = (bytes(no_fleet), {})
        other_owner = bytearray(make_save())
        other_owner[fleet() + L.FLEET_OWNER] = 0
        cases["fleet owner"] = (bytes(other_owner), {})
        no_flagship = bytearray(make_save())
        no_flagship[ship_slot(0) + L.SHIP_SLOT_STATUS] = 0
        cases["no flagship"] = (bytes(no_flagship), {})
        for name, (data, options) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                outfit_fleet_save(data, options.pop("number", SLOT), **{"ships": 10, "crew": 150, **options})


if __name__ == "__main__":
    unittest.main()
