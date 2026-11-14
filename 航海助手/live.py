"""Use the DOSBox-X local debugger to lock selected resources in a game copy."""

import argparse
import fcntl
import hashlib
import json
import os
import pty
import random
import re
import select
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from struct import pack, pack_into, unpack_from

from sea2 import FAME, HEROES, HEADER_SIZE, ORIGINAL_EXE_SHA256, SAVE, SLOT_SIZE, patch_provisions, read_slots
from story import StoryPilot
from story_destinations import CoastMap, read_ports, route_targets

DATA_SEGMENT_DELTA = 0x3815
LIVE_BASE = 0xca4
GOLD = LIVE_BASE + 0x60a
FAME_BASE = LIVE_BASE + 0x5b6
FLEET_BASE, FLEET_SIZE, FLEET_COUNT = 0x2e76, 0x85, 70
FLEET_POINTER = 0xc2d4
SHIP_BASE, SHIP_SIZE, SHIP_COUNT = 0x5892, 22, 65
SUPPLY_BASE, SUPPLY_SIZE = 0x52d4, 30
FLEET_SHIP_LIMIT = 10
TRAINER_GATES = {
    "boarding": (0x83,),
    "sea_guard": (0xd3, 0xea),
    "fast": (0x110,),
    "hazards": (0x121, 0x134, 0x141, 0x150, 0x173),
}
TRAINER_SWITCHES = (*TRAINER_GATES, "supplies", "cannon", "duel", "duel_guard", "teleport",
                    "daylight", "opening", "pins", "map", "route", "discoveries")
GOLD_FLOOR = 10_000_000
JOHN_GOLD_FLOOR = 600_000_000
ALI_GOLD_FLOOR = 9_990_000
FAME_FLOOR = 50_000
TELEPORT_SEGMENT = 0x18
TELEPORT_MAILBOX = 0xb0
COAST_TELEPORT = 0xff


def main_psp(report):
    owners = {int(owner, 16) for owner in re.findall(
        r"^\s*[0-9A-F]{4}\s+\d+\s+([0-9A-F]{4})\s+MAIN\s*$", report, re.MULTILINE
    )}
    if len(owners) != 1:
        raise ValueError("游戏主程序尚未就绪。")
    return owners.pop()


def lock_fields(hero, values):
    if not 0 <= hero < len(HEROES):
        raise ValueError("当前角色无法识别。")
    writes = []
    for name, value in values.items():
        if name == "gold" and 0 <= value <= 600_000_000:
            writes.append((GOLD, pack("<I", value)))
        elif name in FAME and 0 <= value <= 50_000:
            offset = FAME_BASE + hero * 14 + FAME.index(name) * 2
            writes.append((offset, pack("<H", value)))
        else:
            raise ValueError("金币范围为 0–600000000，各项声望范围为 0–50000。")
    return writes


def player_fleet(state):
    fleet, = unpack_from("<H", state, FLEET_POINTER)
    index, remainder = divmod(fleet - FLEET_BASE, FLEET_SIZE)
    if not remainder and 0 <= index < FLEET_COUNT:
        return fleet
    return None


def refill_writes(state, hero, *, legacy=True):
    fleet = player_fleet(state)
    if fleet is None:
        return []

    writes = []
    if legacy:
        gold, = unpack_from("<I", state, GOLD)
        gold_floor = {
            HEROES.index("约翰"): JOHN_GOLD_FLOOR,
            HEROES.index("阿兰"): ALI_GOLD_FLOOR,
        }.get(hero, GOLD_FLOOR)
        if gold < gold_floor:
            writes.append((GOLD, pack("<I", gold_floor)))
        fame_offset = FAME_BASE + hero * 14
        fame = unpack_from("<3H", state, fame_offset)
        if min(fame) < FAME_FLOOR:
            writes.append((fame_offset, pack("<3H", *(max(value, FAME_FLOOR) for value in fame))))

    for slot in range(10):
        ship_slot = fleet + 0x2b + slot * 9
        instance = state[ship_slot + 7]
        if state[ship_slot + 8] & 0x30 != 0x10 or instance >= SHIP_COUNT:
            continue
        ship = SHIP_BASE + instance * SHIP_SIZE
        capacity, = unpack_from("<H", state, ship + 20)
        supply = SUPPLY_BASE + slot * SUPPLY_SIZE
        stores = unpack_from("<4H", state, supply)
        cargo = sum(unpack_from("<5H", state, supply + 12))
        room = capacity * 10 - (stores[0] + stores[1] + (stores[2] + stores[3] + cargo) * 10)
        if room <= 0:
            continue
        # Water and food are recorded in tenths of a barrel, other stores in whole units.
        updated = list(stores)
        for field, target, unit in (
            (0, 10, 1), (1, 10, 1), (2, 5, 10), (3, 10 if state[ship + 17] else 0, 10),
            (0, 200, 1), (1, 200, 1),
        ):
            added = min(max(0, target - updated[field]), room // unit)
            updated[field] += added
            room -= added * unit
        if tuple(updated) != stores:
            writes.append((supply, pack("<4H", *updated)))
    return writes


def trainer_writes(state, hero, profile):
    fields = {name: profile[name] for name in ("gold", *FAME) if name in profile}
    writes = [
        (offset, value) for offset, value in lock_fields(hero, fields)
        if int.from_bytes(state[offset:offset + len(value)], "little") < int.from_bytes(value, "little")
    ]
    if "supplies" in profile:
        writes.extend(refill_writes(state, hero, legacy=False))
    return writes


def outfit_fleet_save(data, number, *, ships=10, crew=150):
    """Outfit one save before the emulator loads the fleet."""
    if not 1 <= ships <= FLEET_SHIP_LIMIT or not 1 <= crew <= 150:
        raise ValueError("舰船数量须为 1–10，每船水手须为 1–150。")
    slot = next((slot for slot in read_slots(data) if slot.number == number), None)
    if slot is None:
        raise ValueError(f"第 {number} 槽没有存档。")
    start = HEADER_SIZE + (number - 1) * SLOT_SIZE
    state = bytearray(bytes(LIVE_BASE) + data[start:start + SLOT_SIZE])
    officers = LIVE_BASE + 0x612
    fleet_id = state[officers + slot.hero * 50 + 44]
    if fleet_id >= FLEET_COUNT:
        raise ValueError("这份存档还没有玩家舰队。")
    fleet = FLEET_BASE + fleet_id * FLEET_SIZE
    if state[fleet + 42] != slot.hero:
        raise ValueError("存档的舰队归属无法确认。")

    slots = [fleet + 43 + index * 9 for index in range(FLEET_SHIP_LIMIT)]
    owned = [index for index, offset in enumerate(slots) if state[offset + 8] & 0x30 == 0x10]
    if not owned:
        raise ValueError("请使用已有旗舰的存档。")
    empty = [index for index in range(FLEET_SHIP_LIMIT) if state[slots[index] + 8] & 0x30 == 0]
    selected = owned + empty[:max(0, ships - len(owned))]
    if len(selected) < ships:
        raise ValueError("空闲船位不足，请先处理存档里未出航的船。")
    instances = [state[slots[index] + 7] for index in owned]
    if len(set(instances)) != len(owned) or any(index >= 40 for index in instances):
        raise ValueError("存档的船只编号无法确认。")
    free = iter(index for index in range(40) if state[SHIP_BASE + index * SHIP_SIZE + 15] == 0xff
                and index not in instances)
    instances += [next(free, None) for _ in range(len(selected) - len(owned))]
    if None in instances:
        raise ValueError("存档的空闲船只记录不足。")

    captain_offsets = [SUPPLY_BASE + index * SUPPLY_SIZE + 27 for index in selected]
    candidates = [index for index in range(79, 120)
                  if state[officers + index * 50 + 44] == 0xff
                  and state[officers + index * 50 + 45] < 100
                  and state[officers + index * 50 + 46] == 0]
    random.shuffle(candidates)
    if sum(state[offset] == 0xff for offset in captain_offsets) > len(candidates):
        raise ValueError("可招募的空闲航海士不足。")

    for index, instance, captain_offset in zip(selected, instances, captain_offsets):
        ship = SHIP_BASE + instance * SHIP_SIZE
        supply = captain_offset - 27
        if index not in owned:
            name = f"Reale {index + 1}".encode().ljust(15, b"\0")
            state[ship:ship + 15] = name
            state[supply:supply + SUPPLY_SIZE] = bytes(22) + bytes([255]) * 6 + b"\x64\0"
        captain = state[captain_offset]
        if captain == 0xff:
            captain = candidates.pop()
            state[captain_offset] = captain
            officer = officers + captain * 50
            state[officer + 43:officer + 47] = bytes((100, fleet_id, 255, 2))
        elif captain >= 120 or state[officers + captain * 50 + 44] != fleet_id:
            raise ValueError("存档的船长归属无法确认。")
        officer = officers + captain * 50
        state[officer + 28:officer + 36] = bytes([100]) * 8
        for offset in (36, 37):
            state[officer + offset] = max(100, state[officer + offset])

        # La Reale with the battle loadout from the supported DOS release.
        cargo = sum(unpack_from("<5H", state, supply + 12))
        capacity = max(260, cargo + 70)
        state[ship + 15:ship + 22] = pack("<BBBHH", 0x15, 0x42, 40, 150, capacity)
        position = slots[index]
        state[position:position + 9] = pack("<H7B", crew, 60, 60, 95, 100, 40,
                                             instance, 0x10 | (state[position + 8] & 0xcf))
        water = (capacity - cargo - 60) * 5
        pack_into("<4H", state, supply, water, water, 5, 40)
        state[supply + 8:supply + 10] = b"\x19\x32"
        pack_into("<H", state, supply + 10, crew + capacity + 25)
    return data[:start] + state[LIVE_BASE:] + data[start + SLOT_SIZE:]


def trainer_chart_writes(state, ports):
    writes = []
    for relative, mask in (
        (0x144, (b"\xff" * 11 + b"\xc0") * 45),
        (0x3a2, b"\xff" * 77 + b"\xfc"),
    ):
        for index in range(0, len(mask), 48):
            offset = LIVE_BASE + relative + index
            bits = mask[index:index + 48]
            current = state[offset:offset + len(bits)]
            updated = bytes(value | visible for value, visible in zip(current, bits))
            if updated != current:
                writes.append((offset, updated))
    count = LIVE_BASE + 0x36a
    if unpack_from("<H", state, count)[0] < 4050:
        writes.append((count, pack("<H", 4050)))
    for port, x, y in ports:
        record = LIVE_BASE + 0x52b0 + port * 22
        flag = state[record + 21]
        if not flag & 0x10 and unpack_from("<HH", state, record) == (x, y):
            writes.append((record + 21, bytes([flag | 0x10])))
    return writes


def trainer_discovery_writes(state):
    writes = []
    start = LIVE_BASE + 0x72e8
    for record in range(start, start + 98 * 7, 7):
        x, y, treasure, _, flags = unpack_from("<HHBBB", state, record)
        # The engine selects 50 ordinary sites per campaign. Disabled sites
        # also serve as the pool for treasure placement and later events.
        if 90 <= treasure <= 99 and flags & 0xe0 == 0x60:
            # Old unlocks could put treasure sites in the ordinary report list.
            # Restore their reserved status and remove the spurious discovery.
            writes.append((record + 6, bytes([(flags & 0x0f) | 0x80])))
            continue
        if treasure or flags & 0x80 or not (0 < x < 2160 and 0 < y < 1080):
            continue
        discovered = flags | 0x60
        if discovered != flags:
            writes.append((record + 6, bytes([discovered])))
    return writes


def pin_navigation_ports(image):
    # The Chinese Auto Sail command alone calls this known-port list builder.
    # Keep the MZ relocation on the original far call's segment word and use
    # it for a far jump to the replacement in the executable's zero padding.
    entry, padding = 0x182ba, 0x3b840
    replacement = bytes.fromhex(
        "56 57 06 0e 07 fc 31 db 31 f6"  # Save SI, DI, ES. Scan forward with ES = CS.
        "89 f0 3c 06 73 06 2e 8a 44 40 eb 0c"  # Take the first six IDs from the table.
        "2c 06 bf 40 00 6a 06 59 f2 ae 74 10"  # Scan original IDs, skipping the pinned ones.
        "6b f8 16 f6 85 69 5f 10 74 06"  # Only include known ports (record +0x15).
        "89 87 38 05 43 43 46 81 fe 88 00 72 d1"  # Append a word ID to DS:0538.
        "93 d1 e8 07 5f 5e cb"  # Return the count in AX.
        "4c 02 0d 00 62 63"  # Basra, Istanbul, Venice, Lisbon, Sakai, Nagasaki.
    )
    if (image[entry:entry+15] != bytes.fromhex("558bec83ec0457562ac09a9a5c0000")
            or image[padding:padding+len(replacement)] != bytes(len(replacement))):
        raise ValueError("自动航海列表的程序版本不匹配。")
    result = bytearray(image)
    result[entry:entry+15] = b"\xeb\x08" + b"\x90" * 8 + bytes.fromhex("ea00006436")
    result[padding:padding+len(replacement)] = replacement
    return bytes(result)


def patch_ali_combat(image):
    # Chinese MAIN.EXE, Ali (protagonist 5) only. The first hook runs after
    # boarding starts and before either crew takes damage. The second replaces
    # the damage function's epilogue, after it identifies the attacking sailor.
    boarding, duel, padding = 0x13b25, 0x17687, 0x3b8c0
    routines = bytes.fromhex(
        "8b 77 02 80 3e b5 12 05 75 15"  # Load target crew pointer; check protagonist.
        "8b 3e c0 9b a1 d4 c2 39 45 04 75 09"  # Compare attacking fleet to player's fleet.
        "c7 04 00 00 83 46 f6 2e cb"  # Clear target crew; return to boarding epilogue.
        "8b 1e c2 9b cb"  # Replay original attacker load for other attacks.
        "90 90 90 90 90 90 90 90 90 90 90 90"
        "8b 46 fe 80 3e b5 12 05 75 10"  # Load the original damage; check protagonist.
        "80 3e ae 0c ff 75 09 3b 3e ce c2 75 03"  # At sea, attacking sailor is Ali.
        "b8 c8 00 5e 5f 89 ec 5d cb"  # 200 damage; replay the normal return.
    )
    original_boarding = bytes.fromhex("8b77028b1ec29b")
    original_duel = bytes.fromhex("8b46fe5e5f8be55dcb")
    if (image[boarding:boarding+7] != original_boarding
            or image[duel:duel+9] != original_duel
            or image[padding:padding+len(routines)] != bytes(len(routines))):
        raise ValueError("一击必杀补丁的程序版本不匹配。")

    result = bytearray(image)
    header = unpack_from("<H", image, 8)[0] * 16
    count = unpack_from("<H", image, 6)[0]
    table = unpack_from("<H", image, 0x18)[0]
    end = table + count * 4
    if end + 8 > header or any(image[end:end+8]):
        raise ValueError("主程序的重定位表没有可用空间。")
    segment = (padding - header) // 16
    result[boarding:boarding+7] = b"\x9a" + pack("<HH", 0, segment) + b"\x90" * 2
    result[duel:duel+9] = b"\xea" + pack("<HH", 0x30, segment) + b"\x90" * 4
    for index, hook in enumerate((boarding, duel)):
        relocation_segment, offset = divmod(hook + 3 - header, 16)
        pack_into("<HH", result, end + index*4, offset, relocation_segment)
    pack_into("<H", result, 6, count+2)
    result[padding:padding+len(routines)] = routines
    return bytes(result)


def protect_ali_ships(image):
    cannon, boarding_operand, padding = 0x1393e, 0x13b26, 0x3b910
    routines = bytes.fromhex(
        "8b 77 02 80 3e b5 12 05 75 0a e8 22 00 73 05"  # Check boarding target.
        "83 46 f6 2e cb e9 99 ff"  # Skip enemy boarding damage, or use the original hook.
        "8b 77 02 80 3e b5 12 05 75 0b e8 0b 00 73 06"  # Check cannon target.
        "81 46 f4 83 01 cb 89 d8 cb"  # Return to the epilogue, or replay mov ax,bx.
        "89 f0 2b 06 d4 c2 83 e8 2b 83 f8 5a c3"  # Carry if slot is within the player fleet.
    )
    if (image[cannon:cannon+5] != bytes.fromhex("8b77028bc3")
            or image[boarding_operand:boarding_operand+2] != b"\0\0"
            or image[padding:padding+len(routines)] != bytes(len(routines))):
        raise ValueError("船队保护补丁的程序版本不匹配。")
    result = bytearray(image)
    header = unpack_from("<H", image, 8)[0] * 16
    count = unpack_from("<H", image, 6)[0]
    table = unpack_from("<H", image, 0x18)[0]
    end = table + count * 4
    if end + 4 > header or any(image[end:end+4]):
        raise ValueError("主程序的重定位表没有可用空间。")
    segment = unpack_from("<H", image, boarding_operand + 2)[0]
    result[cannon:cannon+5] = b"\x9a" + pack("<HH", 0x67, segment)
    pack_into("<H", result, boarding_operand, 0x50)
    relocation_segment, offset = divmod(cannon + 3 - header, 16)
    pack_into("<HH", result, end, offset, relocation_segment)
    pack_into("<H", result, 6, count + 1)
    result[padding:padding+len(routines)] = routines
    return bytes(result)


def patch_ali_voyage(image, *, immune=True):
    # Preserve the previous patch bytes to recognize installations on upgrade.
    payload = bytes.fromhex(
        "80 3e b5 12 05 75 02 b0 28 a2 ac bd 8b 1e d4 c2 cb"
        "08 c0 75 09 80 3e b5 12 05 75 02 fe c0 88 45 02 08 c0 cb"
        "e8 0d 00 83 c6 09 cb"
        "e8 06 00 5e 5f 89 ec 5d cb"
        "29 04 75 09 80 3e b5 12 05 75 02 ff 04 c3"
    )
    hooks = (
        (0x36bd6, "a2acbd8b1ed4c2", 0, 0x9a),  # Speed after the heading check.
        (0x1eed3, "8845020ac0", 17, 0x9a),  # Storm hull damage.
        (0x1e6fa, "290483c609", 36, 0x9a),  # Scurvy casualties.
        (0x1e24e, "29045e5f8be55dcb", 43, 0xea),  # Food/water-related casualties.
    )
    if immune:
        payload = bytes.fromhex(
            "80 3e b5 12 05 75 02 b0 28 a2 ac bd 8b 1e d4 c2 cb"  # Max speed.
            "80 3e b5 12 05 75 06 81 46 ec 80 01 cb 8a 45 02 28 e4 cb"  # Skip storm damage.
            "80 3e b5 12 05 74 02 29 04 83 c6 09 cb"  # Skip scurvy casualties.
            "80 3e b5 12 05 74 02 29 04 5e 5f 89 ec 5d cb"  # Skip ration casualties.
            "80 3e b5 12 05 75 11 80 7e f4 03 74 0b 80 7e f4 04 74 05"
            "b8 01 00 eb 03 ff 5e f6 09 c0 cb"  # Report harmful sea events without applying them.
            "89 f3 8b 46 fc 80 3e b5 12 05 75 04 83 46 f4 04 cb"  # Keep a missing ship.
        )
        hooks = (
            hooks[0],
            (0x1ee6d, "8a45022ae4", 17, 0x9a),
            (0x1e6fa, "290483c609", 36, 0x9a),
            (0x1e24e, "29045e5f8be55dcb", 49, 0xea),
            (0x360c4, "ff5ef60bc0", 64, 0x9a),
            (0x1eaf3, "8bde8b46fc", 94, 0x9a),
        )
    padding = 0x3b950
    if image[padding:padding+len(payload)] != bytes(len(payload)):
        raise ValueError("航行保护补丁的程序版本不匹配。")
    result = bytearray(image)
    header = unpack_from("<H", image, 8)[0] * 16
    count = unpack_from("<H", image, 6)[0]
    table = unpack_from("<H", image, 0x18)[0]
    end = table + count * 4
    extra = len(hooks) * 4
    if end + extra > header or any(image[end:end+extra]):
        raise ValueError("主程序的重定位表没有可用空间。")
    segment = (padding - header) // 16
    for index, (address, original, entry, opcode) in enumerate(hooks):
        original_bytes = bytes.fromhex(original)
        length = len(original_bytes)
        if image[address:address+length] != original_bytes:
            raise ValueError("航行保护补丁的程序版本不匹配。")
        result[address:address+length] = bytes([opcode]) + pack("<HH", entry, segment) + b"\x90" * (length - 5)
        relocation_segment, offset = divmod(address + 3 - header, 16)
        pack_into("<HH", result, end + index*4, offset, relocation_segment)
    pack_into("<H", result, 6, count + len(hooks))
    result[padding:padding+len(payload)] = payload
    return bytes(result)


def move_patches_out_of_video_buffer(image, original, *, teleport=False, facing=False,
                                    town_exit=False, direct=False, repair_massawa=False, coast=False,
                                    cannon=False, ammunition=False):
    # Allocate dedicated executable paragraphs before the original load image.
    # Move the original CS, SS and every DOS relocation together. The graphics
    # workspace at 3b840 stays byte-for-byte identical to the unmodified game.
    header_size = unpack_from("<H", image, 8)[0] * 16
    header = bytearray(image[:header_size])
    if image[0x360c4] == 0x9a:
        patch_end = 0x3b9c0
    elif image[0x36bd6] == 0x9a:
        patch_end = 0x3b9a0
    else:
        patch_end = 0x3b950 if image[0x1393e] == 0x9a else 0x3b910
    prefix = image[0x3b840:patch_end]
    if town_exit:
        prefix += bytes.fromhex(
            "0006350584c00f849c00803eae0cff0f8493002e803e"
            "b000000f848900538b1ed4c281fb762e727d81fbd452"
            "73775056575152062ea0b0002ec606b00000fec8a2ae"
            "0cc606af0cff8026b30cc08026051007800e0510202e"
            "a1b10089078947082ea1b30089470289470a31c08947048947068947"  # Sync current and previous ship coordinates.
            "0c894718c6471a082ea1b500a306102ea1b700a30810"
            "31c0a0ae0cb216f6e289c3808f695f509a538c000007"
            "5a595f5e585bcb"
        ).ljust(0xc0, b"\0")
    elif teleport:
        prefix += bytes.fromhex(
            "807efb00 0f848c00 803eae0cff 0f848300 2e803ea00000 747b"
            "8b1ed4c2 81fb762e 7271 81fbd452 736b 515206"
            "2ea0a000 2ec606a00000 fec8 a2ae0c c606af0cff"
            "8026b30cc0 8026051007 800e051020"
            "2ea1a100 8907 2ea1a300 894702 31c0 894704 894706"
            "89470c 894718 c6471a08 2ea1a500 a30610 2ea1a700 a30810"
            "31c0 a0ae0c b216 f6e2 89c3 808f695f50"
            "9a538c0000 075a59 8a46fb5e5f8be55dcb"
        ).ljust(0xb0, b"\0")
    if facing:
        # Keep the last direction the walking code actually drew. The
        # idle redraw can otherwise use the town loop's default direction.
        prefix += bytes.fromhex(
            "2e8936c800 89f3 a1b80c cb"  # Save SI, replay overwritten instructions.
            "2e8b1ec800 ea e0600000"      # Draw the idle frame in that direction.
        ).ljust(0x18, b"\0") + b"\x02\x00" + bytes(6)
    if direct:
        prefix = bytearray(prefix)
        # The sea entry reaches the same transfer routine at offset 0x0c.
        prefix[0x18a:0x193] = b"\x90" * 9
        prefix += bytes.fromhex(
            "803eea0300750e 2e803eb00000740b fe0e3505 b001"
            "ea61bc0000 ea03bb0000"
            "803eae0cff7504 0ee822ff 803eae0cffcb"
        ).ljust(0x30, b"\0")
        if repair_massawa:
            # Two affected saves have Massawa's camera (417, 517), but the
            # fleet's X lost its low byte. Correct it before the sea renderer.
            entry, repair = 0x25f, 0x280
            if prefix[entry:entry + 17] != bytes.fromhex("803eae0cff75040ee822ff803eae0cffcb"):
                raise ValueError("马萨瓦出港修复的程序版本不匹配。")
            prefix = prefix.ljust(repair, b"\0")
            prefix[entry:entry + 3] = b"\xe9" + pack("<h", repair - entry - 3)
            prefix += bytes.fromhex(
                "803eae0cff 7537"  # In town, return the original comparison.
                "813e0610a101 752b 813e08100502 7523"  # Massawa camera.
                "53 8b1ed4c2 81fb762e 7217 81fbd452 7311"
                "813f0001 750b 817f021002 7504 c707ac01 5b"  # (256,528) -> (428,528).
                "0ee8c2fe 803eae0cff cb"  # Preserve the sea teleport call and return flags.
            ).ljust(0x50, b"\0")
        if coast:
            # The shared transfer has already set the fleet and camera coordinates.
            # 0xFF in the mailbox becomes 0xFE after DEC, outside the port table.
            entry, helper = 0x20a, 0x2d0
            original_port = bytes.fromhex("31c0a0ae0c")
            if len(prefix) != helper or prefix[entry:entry + 5] != original_port:
                raise ValueError("藏宝点热瞬移补丁的程序版本不匹配。")
            prefix[entry:entry + 5] = b"\xe9" + pack("<h", helper - entry - 3) + b"\x90\x90"
            open_sea = bytes.fromhex(
                "c606ae0cff 8026051007"  # Enter open sea with auto-sailing stopped.
                "8b07894708 8b470289470a"  # Reset the previous movement coordinates.
            )
            branch = bytes.fromhex("803eae0cfe75") + bytes([len(open_sea) + 8]) + open_sea
            coast_segment = helper + len(branch) + 3
            branch += bytes.fromhex("9a1f850000")  # The game's normal sea screen initializer.
            branch += b"\xe9" + pack("<h", 0x21f - helper - len(branch) - 3)
            branch += original_port
            branch += b"\xe9" + pack("<h", 0x20f - helper - len(branch) - 3)
            prefix += branch.ljust(0x30, b"\0")
    departure_entry = len(prefix)
    if direct:
        # The harbor can retain a viewport from the previous sea area.
        # Rebuild its origin from the fleet before the stock sea initializer
        # selects and loads the map bank for the first departure frame.
        prefix += bytes.fromhex("9a4a6aea0f ea1f850000").ljust(0x10, b"\0")
    cannon_entry = len(prefix)
    if cannon:
        # The protection hook has already handled incoming fire. Check the
        # firing fleet and target slot, then let the game's caller sink the ship.
        prefix = bytearray(prefix)
        if prefix[0xfc:0xff] != bytes.fromhex("89d8cb"):
            raise ValueError("舰炮伤害补丁的程序版本不匹配。")
        prefix[0xfc:0xff] = b"\xe9" + pack("<h", cannon_entry - 0xff)
        prefix += bytes.fromhex(
            "a1d4c2 57 8b3ec09b 394504 5f 7518"
            "89f0 2b06d4c2 83e82b 83f85a 720a"
            "c7040000 8146f48301 cb"
            "89d8 cb"
        ).ljust(0x30, b"\0")
    ammunition_entry = len(prefix)
    if ammunition:
        prefix += bytes.fromhex(
            "8b1ec29b 50 57 a1d4c2 8b3ec09b 394504 5f 58"
            "7403 ff4f0e cb"
        ).ljust(0x20, b"\0")
    paragraphs = len(prefix) // 16
    module = bytearray(image[header_size:])
    module[0x3b840-header_size:patch_end-header_size] = original[0x3b840:patch_end]
    count = unpack_from("<H", header, 6)[0]
    table = unpack_from("<H", header, 0x18)[0]
    patch_segments = {0x182c7: 0, 0x13b28: 8, 0x1768a: 8, 0x13941: 8}
    for address in (0x36bd6, 0x1eed3, 0x1ee6d, 0x1e6fa, 0x1e24e, 0x360c4, 0x1eaf3):
        patch_segments[address + 3] = 17
    for index in range(count):
        entry = table + index * 4
        offset, segment = unpack_from("<HH", header, entry)
        location = segment * 16 + offset
        target = unpack_from("<H", module, location)[0]
        patch_segment = patch_segments.get(location + header_size)
        if patch_segment is not None and target in (0x3664, 0x366c, 0x3675):
            target = patch_segment
        else:
            target += paragraphs
        pack_into("<H", module, location, target)
        pack_into("<H", header, entry + 2, segment + paragraphs)
    added_relocations = []
    if direct:
        start = 0x2cc47 - header_size
        if image[0x2cc47:0x2cc4c] != bytes.fromhex("9a1f850000"):
            raise ValueError("出港地图补丁的程序版本不匹配。")
        prefix = bytearray(prefix)
        for offset in (3, 8):
            location = departure_entry + offset
            segment = unpack_from("<H", prefix, location)[0]
            pack_into("<H", prefix, location, segment + paragraphs)
            added_relocations.append(location)
        # Keep the existing DOS relocation for the departure call.
        module[start:start + 5] = b"\x9a" + pack("<HH", 0, departure_entry // 16)
    if ammunition:
        start = 0x13b13 - header_size
        if module[start:start + 7] != bytes.fromhex("8b1ec29bff4f0e"):
            raise ValueError("舰炮弹药补丁的程序版本不匹配。")
        module[start:start + 7] = b"\x9a" + pack("<HH", 0, ammunition_entry // 16) + b"\x90\x90"
        added_relocations.append(len(prefix) + start + 3)
    if coast:
        pack_into("<H", prefix, coast_segment, paragraphs)
        added_relocations.append(coast_segment)
    if teleport or town_exit:
        # Run after the town wrapper has finished its input and drawing cleanup.
        address, signature, opcode, operand = (
            (0x20430, "0006350590", 0x9a, 0x9d) if town_exit else
            (0x20abf, "8a46fb5e5f8be55dcb", 0xea, 0x8f)
        )
        hook = address - header_size
        expected = bytes.fromhex(signature)
        if module[hook:hook + len(expected)] != expected:
            raise ValueError("港口瞬移补丁的程序版本不匹配。")
        module[hook:hook + len(expected)] = (
            bytes((opcode,)) + pack("<HH", 0, TELEPORT_SEGMENT) + b"\x90" * (len(expected) - 5)
        )
        prefix = bytearray(prefix)
        call_segment = TELEPORT_SEGMENT * 16 + operand
        pack_into("<H", prefix, call_segment, paragraphs)
        added_relocations.extend((len(prefix) + hook + 3, call_segment))
    if facing:
        first = 0xbc3b - header_size
        if module[first:first + 5] != bytes.fromhex("8bdea1b80c"):
            raise ValueError("港内人物朝向补丁的程序版本不匹配。")
        module[first:first + 5] = b"\x9a" + pack("<HH", 0xb0, TELEPORT_SEGMENT)
        added_relocations.append(len(prefix) + first + 3)
        for address in (0x20cbf, 0x20cce):
            start = address - header_size
            if module[start:start + 6] != bytes.fromhex("8b5efc9ae060"):
                raise ValueError("港内人物重绘补丁的程序版本不匹配。")
            # Keep the original relocation at the same CALL segment word.
            module[start:start + 8] = b"\x90" * 3 + b"\x9a" + pack("<HH", 0xbb, TELEPORT_SEGMENT)
        target = TELEPORT_SEGMENT * 16 + 0xc3
        pack_into("<H", prefix, target, paragraphs)
        added_relocations.append(target)
    if direct:
        base = TELEPORT_SEGMENT * 16
        for address, signature, opcode, entry in (
            (0x20b99, "803eea03007403e95e01", 0xea, 0xc0),
            (0x20404, "803eae0cff", 0x9a, 0xdf),
        ):
            start = address - header_size
            expected = bytes.fromhex(signature)
            if module[start:start + len(expected)] != expected:
                raise ValueError("即时瞬移补丁的程序版本不匹配。")
            module[start:start + len(expected)] = (
                bytes((opcode,)) + pack("<HH", entry, TELEPORT_SEGMENT) + b"\x90" * (len(expected) - 5)
            )
            added_relocations.append(len(prefix) + start + 3)
        for offset in (0xd8, 0xdd):
            location = base + offset
            pack_into("<H", prefix, location, paragraphs + 0x0fea)
            added_relocations.append(location)
    if added_relocations:
        required = table + (count + len(added_relocations)) * 4
        if required > header_size:
            header.extend(bytes((required - header_size + 15) // 16 * 16))
            pack_into("<H", header, 8, len(header) // 16)
        for index, location in enumerate(added_relocations, count):
            segment, offset = divmod(location, 16)
            pack_into("<HH", header, table + index * 4, offset, segment)
        pack_into("<H", header, 6, count + len(added_relocations))
    for location in (0x0e, 0x16):
        pack_into("<H", header, location, unpack_from("<H", header, location)[0] + paragraphs)
    pages, tail = divmod(len(header) + len(prefix) + len(module), 512)
    pack_into("<HH", header, 2, tail, pages + bool(tail))
    return bytes(header) + prefix + bytes(module)


def enable_john_protection(image):
    # Use unused bytes in the relocated code, outside the teleport mailbox.
    header = unpack_from("<H", image, 8)[0] * 16
    result = bytearray(image)
    gate = bytes.fromhex("803eb51205")
    checks = (0x83, 0xb3, 0xd3, 0xea, 0x110, 0x121, 0x134, 0x141, 0x150, 0x173)
    helper = header + 0x221
    code = bytes.fromhex("803eb51200 7405 803eb51205 c3")
    if (result[helper:helper + len(code)] != bytes(len(code))
            or any(result[header + offset:header + offset + len(gate)] != gate for offset in checks)):
        raise ValueError("约翰保护补丁的程序版本不匹配。")
    result[helper:helper + len(code)] = code
    for offset in checks:
        start = header + offset
        result[start:start + 5] = b"\xe8" + pack("<h", helper - start - 3) + b"\x90\x90"

    # João has scripted duels ashore. The damage routine identifies the
    # attacking sailor the same way in town and at sea.
    town_duel = header + 0xa4
    port_check = header + 0x239  # After the nine-byte teleport mailbox.
    sea_test = header + 0xba
    code = bytes.fromhex("803eb51200 7403 e9") + pack("<h", port_check - town_duel - 10) + b"\xc3"
    port_code = bytes.fromhex("803eae0cff c3")
    if (result[town_duel:town_duel + len(code)] != b"\x90" * len(code)
            or result[port_check:port_check + len(port_code)] != bytes(len(port_code))
            or result[sea_test:sea_test + 5] != bytes.fromhex("803eae0cff")):
        raise ValueError("约翰陆上单挑补丁的程序版本不匹配。")
    result[town_duel:town_duel + len(code)] = code
    result[port_check:port_check + len(port_code)] = port_code
    result[sea_test:sea_test + 5] = b"\xe8" + pack("<h", town_duel - sea_test - 3) + b"\x90\x90"
    return bytes(result)


def enable_catalina_protection(image):
    header = unpack_from("<H", image, 8)[0] * 16
    helper = header + 0x221
    extension = header + 0x270  # Padding after the direct teleport code.
    old_code = bytes.fromhex("803eb51200 7405 803eb51205 c3")
    code = bytes.fromhex(
        "803eb51201 7703 38c0 c3"  # João and Catalina return with ZF set.
        "803eb51205 c3"  # Ali also returns with ZF set.
    )
    if (image[helper:helper + len(old_code)] != old_code
            or image[extension:extension + len(code)] != bytes(len(code))):
        raise ValueError("卡特琳娜保护补丁的程序版本不匹配。")
    result = bytearray(image)
    result[helper:helper + 3] = b"\xe9" + pack("<h", extension - helper - 3)
    result[extension:extension + len(code)] = code
    return bytes(result)


def repair_massawa_in_town(image):
    header = unpack_from("<H", image, 8)[0] * 16
    result = bytearray(image)
    town, sea, town_helper, coordinates = 0x240, 0x25f, 0x262, 0x280
    sea_helper = 0x2b4
    if (image[header + town:header + town + 5] != bytes.fromhex("803eea0300")
            or image[header + sea:header + sea + 3] != bytes.fromhex("e91e00")
            or image[header + coordinates:header + coordinates + 7] != bytes.fromhex("803eae0cff7537")):
        raise ValueError("马萨瓦港内修复的程序版本不匹配。")
    # The street dispatcher also runs while the ship is still docked.
    # Keep its original comparison and correct the coordinates before launch.
    result[header + town:header + town + 5] = b"\xe8" + pack("<h", town_helper - town - 3) + b"\x90\x90"
    result[header + sea:header + sea + 3] = b"\xe9" + pack("<h", sea_helper - sea - 3)
    result[header + town_helper:header + town_helper + 9] = bytes.fromhex("e81b00 803eea0300 c3")
    code = bytes.fromhex(
        "813e0610a101 752b 813e08100502 7523"
        "53 8b1ed4c2 81fb762e 7217 81fbd452 7311"
        "813f0001 750b 817f021002 7504 c707ac01 5b c3"
        "803eae0cff 7507 e8c2ff 0e e8befe 803eae0cff cb"
    )
    result[header + coordinates:header + coordinates + len(code)] = code
    return bytes(result)


def keep_daylight(image):
    # Towns and the world map share the four time-of-day palettes.
    # Keep the clock and fog palette available to the game.
    start = 0x477fc
    size = 0x30
    day = image[start + 2 * size:start + 3 * size]
    if image[start - 8:start] != bytes.fromhex("0001020203000000") or len(day) != size:
        raise ValueError("白天画面补丁的程序版本不匹配。")
    result = bytearray(image)
    result[start:start + 4 * size] = day * 4
    return bytes(result)


def open_buildings_at_all_hours(image):
    # The closed branch clears the flag that guards the building visit.
    address = 0x2095c
    if image[address:address + 4] != bytes.fromhex("c646fb00"):
        raise ValueError("全天营业补丁的程序版本不匹配。")
    return image[:address] + b"\x90" * 4 + image[address + 4:]


def smooth_town_walking(image):
    # Use the engine's faster four-tick town cadence while still updating
    # the walking sprite when the graphics-delay option is turned off.
    shift = (unpack_from("<H", image, 8)[0] + unpack_from("<H", image, 0x16)[0]) * 16 - 0x5200
    result = bytearray(image)
    for address, before, after in (
        (0x20c58, "a0a40c24013c011bc024fc050800", "b804009090909090909090909090"),
        (0xe718, "740f", "9090"),
    ):
        start = address + shift
        expected = bytes.fromhex(before)
        replacement = bytes.fromhex(after)
        end = start + len(expected)
        if len(expected) != len(replacement) or result[start:end] != expected:
            raise ValueError("港内步行补丁的程序版本不匹配。")
        result[start:end] = replacement
    return bytes(result)


def prepare_story_hours(folder):
    changes = {
        "SNR1.DAT": (
            (0x7a, "0f01078e014200f7", "0f01078e010000f7"),
            (0xa53, "0f01078e010d01268f01330126", "0f01078e010001268f01470126"),
            (0x3349, "0f01078e010d01c68f013301c6", "0f01078e010001c68f014701c6"),
            (0x35d3, "8f01180043", "fe0043f2f2"),
            (0x35e2, "8e012d0052", "8e01480052"),
        ),
        "SNR2.DAT": (
            (0xe17, "0f01078e010d00658f01330065", "0f01078e010000658f01470065"),
            (0x1730, "0f01078f013f0155", "0f01078f01470155"),
        ),
    }
    for name, edits in changes.items():
        path = folder / name
        image = path.read_bytes()
        result = bytearray(image)
        for address, before, after in edits:
            original = bytes.fromhex(before)
            replacement = bytes.fromhex(after)
            end = address + len(original)
            if image[address:end] not in (original, replacement):
                raise ValueError(f"{name} 的剧情时间补丁版本不匹配。")
            result[address:end] = replacement
        if result != image:
            replace_game_file(path, result)


def prepare_provisions(exe, *, pin_ports=False, ali_combat=False):
    original = SAVE.with_name("MAIN.EXE").read_bytes()
    patched = patch_provisions(original)
    image = exe.read_bytes()
    pinned = pin_navigation_ports(patched)
    combat = patch_ali_combat(pinned)
    previous = move_patches_out_of_video_buffer(
        patch_ali_voyage(protect_ali_ships(combat)), original)
    desired = pinned if pin_ports else patched
    if ali_combat:
        desired = patch_ali_voyage(protect_ali_ships(patch_ali_combat(desired)))
        desired = open_buildings_at_all_hours(keep_daylight(desired))
        previous_walking = move_patches_out_of_video_buffer(desired, original, teleport=True)
        previous_facing = smooth_town_walking(move_patches_out_of_video_buffer(
            desired, original, teleport=True, facing=True))
        previous_town_exit = move_patches_out_of_video_buffer(desired, original, town_exit=True)
        previous_direct = move_patches_out_of_video_buffer(desired, original, town_exit=True, direct=True)
        previous_protection = enable_john_protection(previous_direct)
        previous_catalina = enable_catalina_protection(previous_protection)
        repaired_direct = move_patches_out_of_video_buffer(
            desired, original, town_exit=True, direct=True, repair_massawa=True)
        previous_massawa = enable_catalina_protection(enable_john_protection(repaired_direct))
        desired = repair_massawa_in_town(previous_massawa)
    else:
        if pin_ports:
            desired = move_patches_out_of_video_buffer(desired, original)
        previous_walking = desired
        previous_facing = desired
        previous_town_exit = desired
        previous_direct = desired
        previous_protection = desired
        previous_catalina = desired
        previous_massawa = desired
    if image == desired:
        return
    if image not in (original, patched, pinned, combat, previous, previous_walking, previous_facing, previous_town_exit,
                     previous_direct, previous_protection, previous_catalina, previous_massawa,
                     smooth_town_walking(previous_walking),
                     keep_daylight_version(original, combat),
                     move_patches_out_of_video_buffer(open_buildings_at_all_hours(keep_daylight(
                         patch_ali_voyage(protect_ali_ships(combat)))), original),
                     move_patches_out_of_video_buffer(
                         patch_ali_voyage(protect_ali_ships(combat), immune=False), original),
                     move_patches_out_of_video_buffer(protect_ali_ships(combat), original),
                     move_patches_out_of_video_buffer(combat, original),
                     move_patches_out_of_video_buffer(pinned, original)):
        raise ValueError("游戏副本的 MAIN.EXE 版本不匹配。")
    backup = exe.with_suffix(".original.EXE")
    if not backup.exists():
        with backup.open("xb") as file:
            file.write(original)
    replace_game_file(exe, desired)


def keep_daylight_version(original, combat):
    return move_patches_out_of_video_buffer(
        keep_daylight(patch_ali_voyage(protect_ali_ships(combat))), original)


def build_trainer_main(original, features):
    if hashlib.sha256(original).hexdigest() != ORIGINAL_EXE_SHA256:
        raise ValueError("主程序版本不匹配。请使用这份中文版原始 MAIN.EXE。")
    image = patch_provisions(original) if "supplies" in features else original
    if "daylight" in features:
        image = open_buildings_at_all_hours(keep_daylight(image))
    if "pins" in features:
        image = pin_navigation_ports(image)
    teleport = bool(features.intersection(("teleport", "route")))
    if not teleport and not features.intersection((*TRAINER_GATES, "duel", "duel_guard", "cannon", "supplies")):
        return move_patches_out_of_video_buffer(image, original) if "pins" in features else image

    image = patch_ali_voyage(protect_ali_ships(patch_ali_combat(image)))
    image = move_patches_out_of_video_buffer(
        image, original, town_exit=teleport, direct=teleport, repair_massawa=teleport,
        coast="route" in features, cannon="cannon" in features, ammunition="supplies" in features,
    )
    if teleport:
        image = repair_massawa_in_town(image)
    header = unpack_from("<H", image, 8)[0] * 16
    result = bytearray(image)
    for name, offsets in TRAINER_GATES.items():
        for offset in offsets:
            start = header + offset
            if image[start:start + 5] != bytes.fromhex("803eb51205"):
                raise ValueError("修改器开关的程序版本不匹配。")
            result[start:start + 5] = bytes.fromhex("38c0909090" if name in features else "803eb512ff")

    # DI points to the attacker, [BP-6] to the defender. SI is restored by the epilogue.
    attack = bytes.fromhex("3b3ecec2 7503 b8c800") if "duel" in features else b""
    defend = bytes.fromhex("8b76fa 3b36cec2 7504 31c0 eb") + bytes([len(attack)]) if "duel_guard" in features else b""
    duel = bytes.fromhex("8b46fe") + defend + attack + bytes.fromhex("5e5f89ec5dcb")
    result[header + 0xb0:header + 0xd0] = duel.ljust(0x20, b"\x90")
    return bytes(result)


def prepare_trainer(folder, features):
    original = SAVE.with_name("MAIN.EXE").read_bytes()
    desired = {"MAIN.EXE": build_trainer_main(original, features)}
    for name in ("OPEN.EXE", "KOUKAI2.COM", "SNR0.DAT", "SNR1.DAT", "SNR2.DAT", "SNR6.DAT"):
        desired[name] = SAVE.with_name(name).read_bytes()
    if "opening" in features:
        desired["OPEN.EXE"] = patch_opening_enter(desired["OPEN.EXE"])
    for name, data in desired.items():
        path = folder / name
        if not path.is_file() or path.read_bytes() != data:
            replace_game_file(path, data)
    if "daylight" in features:
        prepare_story_hours(folder)


def replace_game_file(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        staging = Path(file.name)
        try:
            file.write(data)
            file.close()
            staging.replace(path)
        finally:
            staging.unlink(missing_ok=True)


def patch_opening_enter(original):
    # OPEN.EXE runs in one code segment. The new code lives in its own
    # paragraphs so it does not overlap its graphics buffers or stack.
    header_size = unpack_from("<H", original, 8)[0] * 16
    header = bytearray(original[:header_size])
    module = bytearray(original[header_size:])
    prefix = bytearray(112)
    paragraphs = len(prefix) // 16
    count = unpack_from("<H", header, 6)[0]
    table = unpack_from("<H", header, 0x18)[0]
    if header_size != 0x200 or table + (count + 7) * 4 > header_size:
        raise ValueError("片头程序的重定位表不匹配。")

    for index in range(count):
        entry = table + index * 4
        offset, segment = unpack_from("<HH", header, entry)
        location = segment * 16 + offset
        value = unpack_from("<H", module, location)[0]
        pack_into("<H", module, location, value + paragraphs)
        pack_into("<H", header, entry + 2, segment + paragraphs)

    relocations = []
    for address, signature, entry in (
        (0x86a8, "c606680400", 0),
        (0x86af, "8bded1e3d1e3", 11),
        (0x4a64, "900ee804d0", 25),
        (0x4aa6, "900ee8c2cf", 25),
        (0x2efb, "32e4cd160ac0", 80),
    ):
        start = address - header_size
        expected = bytes.fromhex(signature)
        if module[start:start + len(expected)] != expected:
            raise ValueError("片头程序的回车补丁版本不匹配。")
        module[start:start + len(expected)] = b"\x9a" + pack("<HH", entry, 0) + b"\x90" * (len(expected) - 5)
        relocations.append(len(prefix) + start + 3)

    # Save the outer frame, then check for Enter between scenes and on
    # each delay iteration. On Enter, resume at OPEN's normal cleanup.
    prefix[:11] = bytes.fromhex("2e892e6400 c606680400 cb")
    prefix[11:21] = bytes.fromhex("e81300 8bded1e3d1e3 cb")
    prefix[25:33] = bytes.fromhex("e80500 ea 6d18 0000")
    prefix[33:79] = bytes.fromhex(
        "50 2e833e640000 7423 2e803e660000 750e"
        "b401 cd16 7415 3c0d 7511 30e4 cd16"
        "2e8b2e6400 8d66fa ea cc84 0000 58c3"
    )
    prefix[80:96] = bytes.fromhex("32e4cd16 3c0d7506 2ec606660001 0ac0")
    prefix[96] = 0xcb
    pack_into("<H", prefix, 31, paragraphs)
    pack_into("<H", prefix, 75, paragraphs)
    relocations.extend((31, 75))
    for index, location in enumerate(relocations, count):
        segment, offset = divmod(location, 16)
        pack_into("<HH", header, table + index * 4, offset, segment)
    pack_into("<H", header, 6, count + len(relocations))
    for address in (0x0e, 0x16):
        pack_into("<H", header, address, unpack_from("<H", header, address)[0] + paragraphs)
    pages, tail = divmod(header_size + len(prefix) + len(module), 512)
    pack_into("<HH", header, 2, tail, pages + bool(tail))
    # Keep an Enter already queued while DOS is loading the opening.
    start = 0x868d - header_size
    if module[start:start + 5] != bytes.fromhex("900ee8c4b3"):
        raise ValueError("片头程序的按键初始化不匹配。")
    module[start:start + 5] = b"\x90" * 5
    return bytes(header) + bytes(prefix) + bytes(module)


def prepare_opening(folder):
    source = SAVE.parent
    opening = folder / "OPEN.EXE"
    original = (source / opening.name).read_bytes()
    desired = patch_opening_enter(original)
    if opening.read_bytes() != desired:
        replace_game_file(opening, desired)
    launcher = folder / "KOUKAI2.COM"
    original = (source / launcher.name).read_bytes()
    if launcher.read_bytes() != original:
        replace_game_file(launcher, original)


def game_hero(state):
    if not (1 <= state[6] < 100 and state[7] < 12 and state[8] < 31 and state[9] < 72):
        raise ValueError("请先在游戏里创建角色或读档。")
    hero = state[0x611]
    if hero >= len(HEROES):
        raise ValueError("当前角色无法识别。")
    return hero


class Debugger:
    def __init__(self, connection, folder, image_shift=0):
        self.stream = connection.makefile("rwb")
        self.folder = folder
        self.sequence = 0
        self.image_shift = image_shift

    def command(self, command):
        self.sequence += 1
        request = str(self.sequence)
        self.stream.write(f"REQ {request} {command}\n".encode("ascii"))
        self.stream.flush()
        header = self.stream.readline().decode(errors="replace").strip()
        if header not in (f"BEGIN {request} OK", f"BEGIN {request} ERR"):
            raise OSError("DOSBox-X 调试连接断开或返回异常。")
        lines = []
        for line in self.stream:
            line = line.decode(errors="replace").rstrip()
            if line == f"END {request}":
                response = "\n".join(lines)
                if header.endswith(" ERR"):
                    raise ValueError(response)
                return response
            lines.append(line)
        raise OSError("DOSBox-X 调试连接已关闭。")

    def read(self, segment, offset, size):
        dump = self.folder / "SEA2READ.BIN"
        dump.unlink(missing_ok=True)
        self.command(f"EXEC MEMDUMPBIN {segment:04X}:{offset:04X} {size:X} {dump.name}")
        data = dump.read_bytes()
        if len(data) != size:
            raise OSError("调试器内存读回长度不正确。")
        return data

    def data_segment(self):
        return main_psp(self.command("EXEC DOS MCBS")) + 0x10 + DATA_SEGMENT_DELTA + self.image_shift

    def game_state(self):
        segment = self.data_segment()
        state = self.read(segment, LIVE_BASE, 0x612)
        return segment, game_hero(state), state

    def write(self, segment, offset, data):
        values = " ".join(f"{value:02X}" for value in data)
        self.command(f"EXEC SM {segment:04X}:{offset:04X} {values}")

    def install_locks(self, segment, writes):
        for offset, data in writes:
            self.write(segment, offset, data)
            if self.read(segment, offset, len(data)) != data:
                raise OSError("写入的数值无法读回。")
            for index in range(len(data)):
                self.command(f"EXEC FM {segment:04X}:{offset + index:04X}")


def prompt(debugger):
    print("进入游戏或读档后，输入 status 查看数值。")
    print("示例  lock gold=10000000 adventure=8000")
    print("unlock 解除锁定。换角色或读另一档前先解除。quit 退出游戏副本。")
    active = {}
    while True:
        words = input("航海助手> ").split()
        if not words:
            continue
        command, *options = words
        if command == "quit":
            return
        if command not in ("status", "lock", "unlock"):
            print("命令为 status、lock、unlock、quit。")
            continue
        if command == "lock":
            try:
                requested = dict(word.split("=", 1) for word in options)
                requested = {name: int(value) for name, value in requested.items()}
                if not requested:
                    raise ValueError("请指定要锁定的数值。")
            except ValueError as error:
                print(error)
                continue

        response = debugger.command("BREAK")
        if "not available" in response:
            raise OSError("DOSBox-X 未能进入调试模式。")
        try:
            if command == "unlock":
                debugger.command("EXEC BPDEL *")
                active.clear()
                print("已解除锁定。")
                continue

            segment, hero, state = debugger.game_state()
            if command == "status":
                gold, = unpack_from("<I", state, 0x60a)
                trade, piracy, adventure = unpack_from("<3H", state, 0x5b6 + hero * 14)
                print(f"{HEROES[hero]} · 金币 {gold} · 商业 {trade} · 海盗 {piracy} · 冒险 {adventure}")
                continue

            fields = active | requested
            writes = lock_fields(hero, fields)
            debugger.command("EXEC BPDEL *")
            active.clear()
            try:
                debugger.install_locks(segment, writes)
            except (OSError, ValueError):
                debugger.command("EXEC BPDEL *")
                raise
            active = fields
            print("已锁定 " + " ".join(f"{name}={value}" for name, value in active.items()))
        except (OSError, ValueError) as error:
            print(error)
        finally:
            debugger.command("EXEC RUNWATCH" if active else "EXEC RUN")


def deliver_teleport_request(debugger, folder, data_segment, *, coast=False):
    request = folder / "teleport.request"
    claimed = folder / "teleport.claimed"
    try:
        request.replace(claimed)
    except FileNotFoundError:
        return
    try:
        payload = claimed.read_bytes()
        if len(payload) == 9 and (1 <= payload[0] <= 130 or coast and payload[0] == COAST_TELEPORT):
            load = data_segment - DATA_SEGMENT_DELTA - debugger.image_shift
            segment = load + TELEPORT_SEGMENT
            # Publish the marker last so the guest sees a complete request.
            debugger.write(segment, TELEPORT_MAILBOX + 1, payload[1:])
            debugger.write(segment, TELEPORT_MAILBOX, payload[:1])
    finally:
        claimed.unlink()


def auto_refill(debugger, game, story=None, *, teleport_folder=None, profile=None):
    if profile is None:
        print("已启动自动补给。新建角色或读档后会自动生效，无需输入命令。", flush=True)
    fast = profile is None or "fast" in profile
    ports = ()
    if profile is not None and "map" in profile:
        rows = Path(__file__).with_name("港口坐标.txt").read_text().splitlines()
        ports = [tuple(map(int, row.split("\t", 3)[:3])) for row in rows]
    unlock_discoveries = profile is not None and "discoveries" in profile
    route_file = teleport_folder / "story-destinations.json" if profile is not None and "route" in profile else None
    if route_file is not None:
        route_ports = read_ports()
        route_coast = CoastMap(SAVE.with_name("WORLDMAP.LZW").read_bytes(),
                               SAVE.with_name("MAIN.EXE").read_bytes())
        route_banks = [(teleport_folder / "game" / f"SNR{hero + 1}.DAT").read_bytes()
                       for hero in range(len(HEROES))]
        last_routes = None
        (teleport_folder / "coast-teleport").touch()
    if story is not None:
        print("剧情助手已启动。请选择约翰开局，或读入约翰的里斯本开场存档。", flush=True)
    fast_segment = None
    while game.poll() is None:
        # The emulator executes each memory command on its own thread.
        # BREAK/RUN interrupts guest timing and causes audible dropouts.
        try:
            segment = debugger.data_segment()
            state = debugger.read(segment, 0, FLEET_POINTER + 2)
            hero = game_hero(state[LIVE_BASE:LIVE_BASE + 0x612])
        except ValueError:
            # Opening animation and title screen do not have a loaded character.
            if route_file is not None:
                route_file.unlink(missing_ok=True)
                last_routes = None
        else:
            if route_file is not None:
                try:
                    routes = route_targets(state[LIVE_BASE:], route_banks[hero], route_ports, route_coast)
                except ValueError:
                    routes = {}
                if routes != last_routes:
                    replace_game_file(route_file, json.dumps(routes, ensure_ascii=False).encode())
                    last_routes = routes
            fleet = player_fleet(state)
            if fleet is not None and teleport_folder is not None:
                if any(state[fleet + 0x33 + ship * 9] & 0x30 == 0x10 for ship in range(10)):
                    deliver_teleport_request(debugger, teleport_folder, segment, coast=route_file is not None)
            at_sea = fast and state[LIVE_BASE + 0x0a] == 0xff and fleet is not None
            code_segment = segment - DATA_SEGMENT_DELTA + 0x1000
            if at_sea and fast_segment != code_segment:
                # File offset 0x204b7: retry the voyage's FMDRV timer query.
                debugger.write(code_segment, 0xb2b7, b"\x90\x90")
                fast_segment = code_segment
            elif not at_sea and fast_segment == code_segment:
                debugger.write(code_segment, 0xb2b7, b"\x75\xf7")
                fast_segment = None
            writes = refill_writes(state, hero) if profile is None else trainer_writes(state, hero, profile)
            if ports:
                writes.extend(trainer_chart_writes(state, ports))
            if unlock_discoveries:
                writes.extend(trainer_discovery_writes(state))
            for offset, data in writes:
                debugger.write(segment, offset, data)
            if story is not None and fleet is not None:
                story.tick(debugger, segment, segment - DATA_SEGMENT_DELTA, hero, state[LIVE_BASE:], fleet)
        try:
            game.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def drain_terminal(fd, stop):
    try:
        while not stop.is_set():
            readable, _, _ = select.select([fd], [], [], 0.2)
            if readable and not os.read(fd, 8192):
                return
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("emulator")
    parser.add_argument("game_copy", type=Path)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--story", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--trainer", action="store_true")
    parser.add_argument("--enable", choices=TRAINER_SWITCHES, action="append", default=[])
    parser.add_argument("--fleet-slot", type=int, choices=range(1, 11))
    parser.add_argument("--fleet-ships", type=int, default=10)
    parser.add_argument("--fleet-crew", type=int, default=150)
    for name in ("gold", *FAME):
        parser.add_argument(f"--{name}", type=int)
    args = parser.parse_args()
    try:
        profile = None
        if args.trainer:
            values = {name: getattr(args, name) for name in ("gold", *FAME) if getattr(args, name) is not None}
            lock_fields(0, values)
            profile = dict.fromkeys(args.enable, True) | values
            if args.fleet_slot is not None:
                profile.update(dict.fromkeys(("cannon", "boarding", "supplies", "sea_guard"), True))
        with (args.game_copy / ".sea2-session.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, "游戏副本正在运行。请先保存并退出旧窗口。\n")
            # The launcher predating auto mode ran DOSBox-X directly in this directory.
            legacy = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-d", "cwd", "-Fc", "--", str(args.game_copy)],
                capture_output=True, text=True, check=False,
            )
            if "cdosbox-x" in legacy.stdout.splitlines():
                parser.exit(1, "旧游戏窗口还开着。请先保存并退出，再双击启动器。\n")
            exe = args.game_copy / "MAIN.EXE"
            if profile is not None:
                if args.fleet_slot is not None:
                    save_path = args.game_copy / SAVE.name
                    saved = save_path.read_bytes()
                    outfitted = outfit_fleet_save(saved, args.fleet_slot, ships=args.fleet_ships, crew=args.fleet_crew)
                prepare_trainer(args.game_copy, set(profile))
                if args.fleet_slot is not None and outfitted != saved:
                    replace_game_file(save_path, outfitted)
            elif args.live:
                prepare_trainer(args.game_copy, set())
            elif args.auto:
                prepare_provisions(exe, pin_ports=True, ali_combat=True)
                source = SAVE.with_name("SNR0.DAT")
                replace_game_file(args.game_copy / source.name, source.read_bytes())
                prepare_story_hours(args.game_copy)
                prepare_opening(args.game_copy)
            else:
                image = exe.read_bytes()
                original = SAVE.with_name("MAIN.EXE").read_bytes()
                patched = patch_provisions(original)
                pinned = pin_navigation_ports(patched)
                combat = patch_ali_combat(pinned)
                teleport = move_patches_out_of_video_buffer(
                    open_buildings_at_all_hours(keep_daylight(
                        patch_ali_voyage(protect_ali_ships(combat)))), original, teleport=True)
                voyage = open_buildings_at_all_hours(keep_daylight(
                    patch_ali_voyage(protect_ali_ships(combat))))
                direct = move_patches_out_of_video_buffer(voyage, original, town_exit=True, direct=True)
                repaired_direct = move_patches_out_of_video_buffer(
                    voyage, original, town_exit=True, direct=True, repair_massawa=True)
                repaired = enable_catalina_protection(enable_john_protection(repaired_direct))
                if (hashlib.sha256(image).hexdigest() != ORIGINAL_EXE_SHA256
                        and image not in (patched, pinned, combat,
                                          move_patches_out_of_video_buffer(pinned, original),
                                          move_patches_out_of_video_buffer(combat, original),
                                          move_patches_out_of_video_buffer(protect_ali_ships(combat), original),
                                          move_patches_out_of_video_buffer(
                                              patch_ali_voyage(protect_ali_ships(combat), immune=False), original),
                                          move_patches_out_of_video_buffer(
                                              patch_ali_voyage(protect_ali_ships(combat)), original),
                                          move_patches_out_of_video_buffer(
                                              keep_daylight(patch_ali_voyage(protect_ali_ships(combat))), original),
                                          move_patches_out_of_video_buffer(
                                              open_buildings_at_all_hours(keep_daylight(
                                                  patch_ali_voyage(protect_ali_ships(combat)))), original),
                                          teleport, smooth_town_walking(teleport),
                                          move_patches_out_of_video_buffer(
                                              open_buildings_at_all_hours(keep_daylight(
                                                  patch_ali_voyage(protect_ali_ships(combat)))), original,
                                              town_exit=True),
                                          direct, enable_john_protection(direct),
                                          enable_catalina_protection(enable_john_protection(direct)),
                                          repaired, repair_massawa_in_town(repaired),
                                          smooth_town_walking(move_patches_out_of_video_buffer(
                                              open_buildings_at_all_hours(keep_daylight(
                                                  patch_ali_voyage(protect_ali_ships(combat)))), original,
                                              teleport=True, facing=True)))):
                    raise ValueError("游戏副本的 MAIN.EXE 版本不匹配。")
                if image in (pinned, combat):
                    prepare_provisions(exe, pin_ports=True, ali_combat=image == combat)
            run_game(args, lock, profile=profile)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


def run_game(args, lock, *, profile=None):
    image = (args.game_copy / "MAIN.EXE").read_bytes()
    image_shift = unpack_from("<H", image, 0x16)[0]

    with tempfile.TemporaryDirectory(prefix="sea2-live-") as tmp, socket.socket() as listener:
        folder = Path(tmp)
        (folder / "game").symlink_to(args.game_copy.resolve(), target_is_directory=True)
        lock.seek(0)
        lock.truncate()
        teleport = args.auto if profile is None else bool(profile.keys() & {"teleport", "route"})
        if teleport:
            lock.write(str(folder))
        lock.flush()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        port = listener.getsockname()[1]
        master, slave = pty.openpty()
        try:
            game = subprocess.Popen(
                [args.emulator, "-nopromptfolder", "-noautoexec", "-set", f"dosbox mcp_server={port}",
                 "-set", "sdl autolock=true", "-set", "sdl mouse_emulation=locked",
                 "-c", "mount c game", "-c", "c:", "-c", "koukai2", "-c", "exit"],
                cwd=folder, stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, TERM="xterm"),
            )
        finally:
            os.close(slave)
        stop = threading.Event()
        reader = threading.Thread(target=drain_terminal, args=(master, stop), daemon=True)
        reader.start()
        try:
            (folder / "dosbox.pid").write_text(str(game.pid))
            connection, _ = listener.accept()
            with connection:
                if focus_file := os.environ.get("SEA2_FOCUS_FILE"):
                    try:
                        Path(focus_file).write_text(str(game.pid))
                    except OSError:
                        pass
                connection.settimeout(8)
                debugger = Debugger(connection, folder, image_shift)
                try:
                    if profile is not None:
                        auto_refill(debugger, game, teleport_folder=folder if teleport else None, profile=profile)
                    elif args.auto:
                        story = StoryPilot((args.game_copy / "ZA_DAT.DAT").read_bytes()) if args.story else None
                        auto_refill(debugger, game, story, teleport_folder=folder)
                    else:
                        prompt(debugger)
                finally:
                    debugger.stream.close()
        except (EOFError, KeyboardInterrupt):
            pass
        except OSError:
            # The debugger socket can close before DOSBox-X finishes exiting.
            try:
                exit_status = game.wait(timeout=2)
            except subprocess.TimeoutExpired:
                exit_status = None
            if exit_status != 0:
                raise
        finally:
            if game.poll() is None:
                game.terminate()
                try:
                    game.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    game.kill()
                    game.wait()
            stop.set()
            reader.join()
            os.close(master)


if __name__ == "__main__":
    main()
