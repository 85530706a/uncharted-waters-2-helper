"""Use the DOSBox-X local debugger to lock selected resources in a game copy."""

import argparse
import fcntl
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

import layout
from layout import FLEET_COUNT, FLEET_POINTER, FLEET_SHIP_LIMIT, FLEET_SIZE, LIVE_BASE, SHIP_COUNT, SHIP_SIZE, SUPPLY_SIZE
from patches import TELEPORT_SEGMENT, TRAINER_GATES, prepare_trainer, replace_game_file
from sea2 import FAME, HEROES, HEADER_SIZE, SAVE, SLOT_SIZE, read_slots
from story_destinations import CoastMap, read_ports, route_targets

DATA_SEGMENT_DELTA = 0x3815
# Data-segment addresses of the current game, derived from the save-slot layout.
GOLD = LIVE_BASE + layout.GOLD
FAME_BASE = LIVE_BASE + layout.FAME
FLEET_BASE = LIVE_BASE + layout.FLEETS
SHIP_BASE = LIVE_BASE + layout.SHIPS
SUPPLY_BASE = LIVE_BASE + layout.SUPPLIES
OFFICER_BASE = LIVE_BASE + layout.OFFICERS
TRAINER_SWITCHES = (*TRAINER_GATES, "supplies", "cannon", "duel", "duel_guard", "teleport",
                    "daylight", "opening", "pins", "map", "route", "discoveries")
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
            offset = FAME_BASE + hero * layout.FAME_STRIDE + FAME.index(name) * 2
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


def ship_slot_offset(fleet, slot):
    return fleet + layout.FLEET_SHIPS + slot * layout.SHIP_SLOT_SIZE


def ship_slot_active(state, ship_slot):
    return state[ship_slot + layout.SHIP_SLOT_STATUS] & layout.SHIP_SLOT_STATE_MASK == layout.SHIP_SLOT_ACTIVE


def refill_writes(state):
    fleet = player_fleet(state)
    if fleet is None:
        return []

    writes = []
    for slot in range(FLEET_SHIP_LIMIT):
        ship_slot = ship_slot_offset(fleet, slot)
        instance = state[ship_slot + layout.SHIP_SLOT_INSTANCE]
        if not ship_slot_active(state, ship_slot) or instance >= SHIP_COUNT:
            continue
        ship = SHIP_BASE + instance * SHIP_SIZE
        capacity, = unpack_from("<H", state, ship + layout.SHIP_CAPACITY)
        supply = SUPPLY_BASE + slot * SUPPLY_SIZE
        stores = unpack_from("<4H", state, supply)
        cargo = sum(unpack_from("<5H", state, supply + layout.SUPPLY_CARGO))
        room = capacity * 10 - (stores[0] + stores[1] + (stores[2] + stores[3] + cargo) * 10)
        if room <= 0:
            continue
        # Water and food are recorded in tenths of a barrel, other stores in whole units.
        updated = list(stores)
        for field, target, unit in (
            (0, 10, 1), (1, 10, 1), (2, 5, 10), (3, 10 if state[ship + layout.SHIP_CANNONS] else 0, 10),
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
        writes.extend(refill_writes(state))
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
    def officer(index):
        return OFFICER_BASE + index * layout.OFFICER_SIZE

    fleet_id = state[officer(slot.hero) + layout.OFFICER_FLEET]
    if fleet_id >= FLEET_COUNT:
        raise ValueError("这份存档还没有玩家舰队。")
    fleet = FLEET_BASE + fleet_id * FLEET_SIZE
    if state[fleet + layout.FLEET_OWNER] != slot.hero:
        raise ValueError("存档的舰队归属无法确认。")

    slots = [ship_slot_offset(fleet, index) for index in range(FLEET_SHIP_LIMIT)]
    owned = [index for index, offset in enumerate(slots) if ship_slot_active(state, offset)]
    if not owned:
        raise ValueError("请使用已有旗舰的存档。")
    empty = [index for index in range(FLEET_SHIP_LIMIT)
             if state[slots[index] + layout.SHIP_SLOT_STATUS] & layout.SHIP_SLOT_STATE_MASK == 0]
    selected = owned + empty[:max(0, ships - len(owned))]
    if len(selected) < ships:
        raise ValueError("空闲船位不足，请先处理存档里未出航的船。")
    instances = [state[slots[index] + layout.SHIP_SLOT_INSTANCE] for index in owned]
    if len(set(instances)) != len(owned) or any(index >= layout.SHIP_PLAYER_LIMIT for index in instances):
        raise ValueError("存档的船只编号无法确认。")
    free = iter(index for index in range(layout.SHIP_PLAYER_LIMIT)
                if state[SHIP_BASE + index * SHIP_SIZE + layout.SHIP_TYPE] == 0xff
                and index not in instances)
    instances += [next(free, None) for _ in range(len(selected) - len(owned))]
    if None in instances:
        raise ValueError("存档的空闲船只记录不足。")

    captain_offsets = [SUPPLY_BASE + index * SUPPLY_SIZE + layout.SUPPLY_CAPTAIN for index in selected]
    candidates = [index for index in range(layout.RECRUIT_FIRST, layout.OFFICER_COUNT)
                  if state[officer(index) + layout.OFFICER_FLEET] == 0xff
                  and state[officer(index) + 45] < 100
                  and state[officer(index) + 46] == 0]
    random.shuffle(candidates)
    if sum(state[offset] == 0xff for offset in captain_offsets) > len(candidates):
        raise ValueError("可招募的空闲航海士不足。")

    roster_start = LIVE_BASE + layout.PERSONNEL.start
    roster_end = LIVE_BASE + layout.PERSONNEL.stop
    roster = set(state[roster_start:roster_end])
    vacancies = iter(offset for offset in range(roster_start, roster_end) if state[offset] == 0xff)

    def enlist(member):
        """Add a fleet member to the personnel menu so the game lists and pays them."""
        if member == slot.hero or member in roster:
            return
        vacancy = next(vacancies, None)
        if vacancy is None:
            raise ValueError("航海士名单已满，请先腾出名额再整备舰队。")
        state[vacancy] = member
        state[LIVE_BASE + layout.PERSONNEL_WAGES + vacancy - roster_start] = 2
        roster.add(member)

    for index, instance, captain_offset in zip(selected, instances, captain_offsets):
        ship = SHIP_BASE + instance * SHIP_SIZE
        supply = captain_offset - layout.SUPPLY_CAPTAIN
        if index not in owned:
            name = f"Reale {index + 1}".encode()
            state[ship + layout.SHIP_NAME.start:ship + layout.SHIP_NAME.stop] = name.ljust(
                layout.SHIP_NAME.stop - layout.SHIP_NAME.start, b"\0")
            state[supply:supply + SUPPLY_SIZE] = bytes(22) + bytes([255]) * 6 + b"\x64\0"
        captain = state[captain_offset]
        if captain == 0xff:
            captain = candidates.pop()
            state[captain_offset] = captain
            record = officer(captain)
            # +43 and +45..+46 are unconfirmed; +44 is the fleet assignment.
            state[record + 43:record + 47] = bytes((100, fleet_id, 255, 2))
        elif captain >= layout.OFFICER_COUNT or state[officer(captain) + layout.OFFICER_FLEET] != fleet_id:
            raise ValueError("存档的船长归属无法确认。")
        enlist(captain)
        record = officer(captain)
        abilities = record + layout.OFFICER_ABILITIES
        state[abilities:abilities + 8] = bytes([100]) * 8
        for offset in layout.OFFICER_EXTRA_STATS:
            state[record + offset] = max(100, state[record + offset])

        # La Reale with the battle loadout from the supported DOS release.
        cargo = sum(unpack_from("<5H", state, supply + layout.SUPPLY_CARGO))
        capacity = max(260, cargo + 70)
        # Type 0x15, an unconfirmed byte, 40 cannons, an unconfirmed uint16, then capacity.
        state[ship + layout.SHIP_TYPE:ship + SHIP_SIZE] = pack("<BBBHH", 0x15, 0x42, 40, 150, capacity)
        position = slots[index]
        status = state[position + layout.SHIP_SLOT_STATUS] & ~layout.SHIP_SLOT_STATE_MASK & 0xff
        state[position:position + layout.SHIP_SLOT_SIZE] = pack(
            "<H7B", crew, 60, 60, 95, 100, 40, instance, layout.SHIP_SLOT_ACTIVE | status)
        water = (capacity - cargo - 60) * 5
        pack_into("<4H", state, supply, water, water, 5, 40)
        state[supply + 8:supply + 10] = b"\x19\x32"
        pack_into("<H", state, supply + 10, crew + capacity + 25)

    # Officers can belong to the fleet without commanding a ship; list them too.
    for member in range(layout.OFFICER_COUNT):
        if state[officer(member) + layout.OFFICER_FLEET] == fleet_id:
            enlist(member)
    return data[:start] + state[LIVE_BASE:] + data[start + SLOT_SIZE:]


def trainer_chart_writes(state, ports):
    writes = []
    for relative, mask in (
        (layout.CHART_MASK, (b"\xff" * 11 + b"\xc0") * 45),
        (layout.CHART_EXTRA_MASK, b"\xff" * 77 + b"\xfc"),
    ):
        for index in range(0, len(mask), 48):
            offset = LIVE_BASE + relative + index
            bits = mask[index:index + 48]
            current = state[offset:offset + len(bits)]
            updated = bytes(value | visible for value, visible in zip(current, bits))
            if updated != current:
                writes.append((offset, updated))
    count = LIVE_BASE + layout.CHART_COUNT
    if unpack_from("<H", state, count)[0] < 4050:
        writes.append((count, pack("<H", 4050)))
    for port, x, y in ports:
        record = LIVE_BASE + layout.PORTS + port * layout.PORT_SIZE
        flag = state[record + layout.PORT_FLAGS]
        if not flag & layout.PORT_CHARTED and unpack_from("<HH", state, record) == (x, y):
            writes.append((record + layout.PORT_FLAGS, bytes([flag | layout.PORT_CHARTED])))
    return writes


def trainer_discovery_writes(state):
    writes = []
    start = LIVE_BASE + layout.DISCOVERIES
    for record in range(start, start + layout.DISCOVERY_COUNT * layout.DISCOVERY_SIZE, layout.DISCOVERY_SIZE):
        x, y, treasure, _, flags = unpack_from("<HHBBB", state, record)
        # The engine selects 50 ordinary sites per campaign. Disabled sites
        # also serve as the pool for treasure placement and later events.
        if 90 <= treasure <= 99 and flags & 0xe0 == 0x60:
            # Old unlocks could put treasure sites in the ordinary report list.
            # Restore their reserved status and remove the spurious discovery.
            writes.append((record + layout.DISCOVERY_FLAGS, bytes([(flags & 0x0f) | 0x80])))
            continue
        if treasure or flags & 0x80 or not (0 < x < 2160 and 0 < y < 1080):
            continue
        discovered = flags | 0x60
        if discovered != flags:
            writes.append((record + layout.DISCOVERY_FLAGS, bytes([discovered])))
    return writes


def game_hero(state):
    calendar = state[layout.CALENDAR:layout.CALENDAR + 4]
    if not (1 <= calendar[0] < 100 and calendar[1] < 12 and calendar[2] < 31 and calendar[3] < 72):
        raise ValueError("请先在游戏里创建角色或读档。")
    hero = state[layout.HERO]
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
        state = self.read(segment, LIVE_BASE, layout.OFFICERS)
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
                gold, = unpack_from("<I", state, layout.GOLD)
                trade, piracy, adventure = unpack_from("<3H", state, layout.FAME + hero * layout.FAME_STRIDE)
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


def auto_refill(debugger, game, profile, *, teleport_folder=None):
    fast = "fast" in profile
    ports = ()
    if "map" in profile:
        rows = Path(__file__).with_name("港口坐标.txt").read_text().splitlines()
        ports = [tuple(map(int, row.split("\t", 3)[:3])) for row in rows]
    unlock_discoveries = "discoveries" in profile
    route_file = teleport_folder / "story-destinations.json" if "route" in profile else None
    if route_file is not None:
        route_ports = read_ports()
        route_coast = CoastMap(SAVE.with_name("WORLDMAP.LZW").read_bytes(),
                               SAVE.with_name("MAIN.EXE").read_bytes())
        route_banks = [(teleport_folder / "game" / f"SNR{hero + 1}.DAT").read_bytes()
                       for hero in range(len(HEROES))]
        last_routes = None
        (teleport_folder / "coast-teleport").touch()
    fast_segment = None
    while game.poll() is None:
        # The emulator executes each memory command on its own thread.
        # BREAK/RUN interrupts guest timing and causes audible dropouts.
        try:
            segment = debugger.data_segment()
            state = debugger.read(segment, 0, FLEET_POINTER + 2)
            hero = game_hero(state[LIVE_BASE:LIVE_BASE + layout.OFFICERS])
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
                if any(ship_slot_active(state, ship_slot_offset(fleet, ship)) for ship in range(FLEET_SHIP_LIMIT)):
                    deliver_teleport_request(debugger, teleport_folder, segment, coast=route_file is not None)
            at_sea = fast and state[LIVE_BASE + layout.CURRENT_PORT] == 0xff and fleet is not None
            code_segment = segment - DATA_SEGMENT_DELTA + 0x1000
            if at_sea and fast_segment != code_segment:
                # File offset 0x204b7: retry the voyage's FMDRV timer query.
                debugger.write(code_segment, 0xb2b7, b"\x90\x90")
                fast_segment = code_segment
            elif not at_sea and fast_segment == code_segment:
                debugger.write(code_segment, 0xb2b7, b"\x75\xf7")
                fast_segment = None
            writes = trainer_writes(state, hero, profile)
            if ports:
                writes.extend(trainer_chart_writes(state, ports))
            if unlock_discoveries:
                writes.extend(trainer_discovery_writes(state))
            for offset, data in writes:
                debugger.write(segment, offset, data)
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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="终端里手动锁定数值")
    mode.add_argument("--trainer", action="store_true", help="按修改器开关启动")
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
            if profile is not None:
                if args.fleet_slot is not None:
                    save_path = args.game_copy / SAVE.name
                    saved = save_path.read_bytes()
                    outfitted = outfit_fleet_save(saved, args.fleet_slot, ships=args.fleet_ships, crew=args.fleet_crew)
                prepare_trainer(args.game_copy, set(profile))
                if args.fleet_slot is not None and outfitted != saved:
                    replace_game_file(save_path, outfitted)
            else:
                prepare_trainer(args.game_copy, set())
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
        teleport = profile is not None and bool(profile.keys() & {"teleport", "route"})
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
                        auto_refill(debugger, game, profile, teleport_folder=folder if teleport else None)
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
