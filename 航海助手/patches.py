"""Build patched copies of MAIN.EXE, OPEN.EXE and the story banks for the trainer."""

import hashlib
import tempfile
from pathlib import Path
from struct import pack, pack_into, unpack_from

from sea2 import ORIGINAL_EXE_SHA256, SAVE, patch_provisions

TRAINER_GATES = {
    "boarding": (0x83,),
    "sea_guard": (0xd3, 0xea),
    "fast": (0x110,),
    "hazards": (0x121, 0x134, 0x141, 0x150, 0x173),
}
TELEPORT_SEGMENT = 0x18


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


def patch_ali_voyage(image):
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
        (0x36bd6, "a2acbd8b1ed4c2", 0, 0x9a),  # Speed after the heading check.
        (0x1ee6d, "8a45022ae4", 17, 0x9a),  # Storm hull damage.
        (0x1e6fa, "290483c609", 36, 0x9a),  # Scurvy casualties.
        (0x1e24e, "29045e5f8be55dcb", 49, 0xea),  # Food/water-related casualties.
        (0x360c4, "ff5ef60bc0", 64, 0x9a),  # Harmful sea events.
        (0x1eaf3, "8bde8b46fc", 94, 0x9a),  # Missing ship.
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
