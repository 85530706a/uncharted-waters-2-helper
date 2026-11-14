"""Find candidate story ports and read held treasure maps from the current game."""

from pathlib import Path
from struct import unpack_from

from sea2 import story_entries

ROOT = Path(__file__).resolve().parent
MAP_NAMES = (
    "纯金面具", "翡翠祭坛", "古代神雕像", "黑曜石石板", "暗黑神水晶",
    "火焰壶", "破坏神宝剑", "金质奖章", "圣人手杖",
)


def read_ports():
    ports = {}
    for row in (ROOT / "港口坐标.txt").read_text().splitlines():
        port, x, y, sea_x, sea_y, name = row.split("\t")
        ports[int(port)] = (int(port), int(x), int(y), int(sea_x), int(sea_y), name)
    return ports


def route_targets(state, bank, ports, coast=None):
    """Return selectable labels and their harbor IDs for a save-slot body."""
    targets = {}
    inventory = set(state[0x21b3:0x21c7])
    for offset in range(0x72e8, 0x72e8 + 98 * 7, 7):
        x, y, item, _, flags = unpack_from("<HHBBB", state, offset)
        if (not 90 <= item < 90 + len(MAP_NAMES) or item - 10 not in inventory
                or item in inventory or not flags & 0x80
                or not (0 <= x < 2160 and 0 <= y < 1080)):
            continue
        port, _, _, sea_x, sea_y, name = min(
            ports.values(),
            key=lambda row: min(abs(row[3] - x), 2160 - abs(row[3] - x)) ** 2 + (row[4] - y) ** 2,
        )
        dx = (x - sea_x + 1080) % 2160 - 1080
        dy = y - sea_y
        east_west = f"向{'东' if dx > 0 else '西'} {abs(dx)} 格" if dx else ""
        north_south = f"向{'南' if dy > 0 else '北'} {abs(dy)} 格" if dy else ""
        direction = "、".join(part for part in (east_west, north_south) if part) or "就在港口外"
        treasure = MAP_NAMES[item - 90]
        water = coast.water_near(x, y) if coast is not None else None
        if water is not None:
            targets[f"直达藏宝点 · {treasure}（{x}, {y}）"] = water
        targets[f"{treasure} · {name.split(' / ')[0]}出海后{direction}（{x}, {y}）"] = port

    chapter, subsection = state[0x30:0x32]
    for entry in story_entries(bank, chapter, subsection):
        port = entry.get("port")
        if port not in ports or "building" not in entry:
            continue
        building = entry["building"]
        if port == 0 and building == "特殊设施" and state[0x611] in (0, 4):
            building = "法雷尔公爵宅"
        name = ports[port][-1].split(" / ")[0]
        targets[f"剧情候选 · {name} · {building}"] = port
    return targets


class CoastMap:
    """Read the terrain cells used by the DOS sea renderer."""

    def __init__(self, archive, main):
        # LS11 archive layout and coding: https://github.com/yinchuan/LSReader
        if archive[:4] != b"LS11":
            raise ValueError("世界地图格式不匹配。")
        self.regions = []
        self.templates = main[0x47e04:0x47e04 + 8 * 144]
        for index in range(3):
            compressed, size, offset = unpack_from(">III", archive, 0x110 + 12 * index)
            bits = iter((byte >> shift) & 1 for byte in archive[offset:offset + compressed]
                        for shift in range(7, -1, -1))

            def code():
                width = 1
                while next(bits):
                    width += 1
                return (1 << width) - 2 + sum(next(bits) << shift for shift in range(width - 1, -1, -1))

            data = bytearray()
            while len(data) < size:
                value = code()
                if value < 256:
                    data.append(archive[16 + value])
                    continue
                distance = value - 256
                length = code() + 3
                if not 0 < distance <= len(data) or len(data) + length > size:
                    raise ValueError("世界地图压缩数据损坏。")
                for _ in range(length):
                    data.append(data[-distance])
            self.regions.append(data)

    def tile(self, x, y):
        region, local_x = divmod(x % 2160, 720)
        data = self.regions[region]
        block = y // 24 * 30 + local_x // 24
        cursor = 2700 + unpack_from("<H", data, block * 2)[0]
        code = data[cursor]
        tile_index = y % 24 // 2 * 12 + local_x % 24 // 2
        if code < 128:
            mask = int.from_bytes(data[cursor + 1:cursor + 19], "big")
            if (mask >> (143 - tile_index)) & 1:
                return data[cursor + 19 + bin(mask >> (144 - tile_index)).count("1")]
        return self.templates[((code & 127) - 120) * 144 + tile_index]

    def water_near(self, x, y):
        # An all-zero tile is open water. Check the ship's full 2×2 footprint.
        candidates = ((dx, dy) for dx in range(-16, 17) for dy in range(-16, 17))
        for dx, dy in sorted(candidates, key=lambda delta: delta[0]**2 + delta[1]**2):
            ship_x, ship_y = (x + dx) % 2160, y + dy
            if 0 <= ship_y < 1079 and all(self.tile(ship_x + i, ship_y + j) == 0
                                          for i in (0, 1) for j in (0, 1)):
                return ship_x, ship_y
        return None
