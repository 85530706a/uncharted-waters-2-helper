"""Run João's opening scenes and first voyage in the Chinese DOS release."""

import time
from struct import pack, unpack, unpack_from


# Relative CS:IP of the returns from town input, town timer and VM opcode C7.
TOWN_RETURNS = ((0xbb08, 0x0fea), (0xbc55, 0x0fea))
DIALOGUE_RETURN = (0x5113, 0x2e09)
STORY_CHOICE_RETURN = (0x4359, 0x2e09)
BUILDING_MENU_RETURNS = ((0x453f, 0x0fea), (0xb5ee, 0x0fea))
HARBOR_MENU_RETURN = (0x8c50, 0x1fd9)
DEPARTURE_CHOICE_RETURN = (0x7c73, 0x1fd9)
PORT_CALL_RETURN = (0xc0ce, 0x0fea)
ARRIVAL_REPORT_RETURN = (0xbe5a, 0x0fea)
SEA_MENU_RETURN = (0x10f6, 0x1fd9)
SEA_TICK_RETURNS = (0xb3e9, 0xb3fb, 0xb40c, 0xb415, 0xb4b5)
BORDEAUX = 27
BORDEAUX_APPROACH = (169, 312)
SHIP_SLOTS, SUPPLY_BASE = 0x2b, 0x52d4
ENTER, ESCAPE, UP = b"\x0d\x1c", b"\x1b\x01", b"\x00\x48"
LISBON_EVENTS = (
    (0, 7, "拜访公爵"),
    (5, 1, "前往酒馆"),
    (2, 10, "去教堂招募恩里科"),
    (3, 2, "到造船厂领船"),
    (1, 7, "夜访母亲"),
    (10, 3, "港口出航教学"),
)


class StoryPilot:
    def __init__(self, doors):
        self.doors = doors
        self.pending = None
        self.started = 0
        self.next_line = 0
        self.reported = None
        self.departure_requested = False
        self.departure_confirmed = False
        self.destination = None
        self.port_confirmed = False

    def say(self, message):
        if self.reported != message:
            print(f"剧情助手 · {message}", flush=True)
            self.reported = message

    def tick(self, debugger, segment, load_segment, hero, state, fleet=None):
        now = time.monotonic()
        if hero == 0 and state[0x0a] == 0xff and state[0x30] == 0 and state[0x31] <= 1:
            self.pending = None
            self.departure_requested = False
            self.say("等待首航遇见多明哥。")
            scene = input_scene(debugger, load_segment)
            if now >= self.next_line and scene in ("dialogue", "sea_menu"):
                tail = free_key_slot(debugger)
                if tail is not None:
                    send_key(debugger, tail, ENTER if scene == "dialogue" else ESCAPE)
                    self.next_line = now + 0.9
            return
        if hero == 0 and state[0x30:0x32] == b"\x01\x00" and fleet is not None:
            port = state[0x0a]
            scene = input_scene(debugger, load_segment)
            docking = (port, scene) == (BORDEAUX, "port_call") and self.destination == BORDEAUX
            if port == BORDEAUX and scene == "town":
                self.say("已抵达波尔多。")
            elif (docking and not self.port_confirmed
                  or (port, scene) == (BORDEAUX, "arrival_report") and self.port_confirmed
                  or (port, scene) == (0xff, "sea_menu")):
                if now >= self.next_line:
                    tail = free_key_slot(debugger)
                    if tail is not None:
                        send_key(debugger, tail, ESCAPE if scene == "sea_menu" else ENTER)
                        if docking:
                            self.port_confirmed = True
                        self.next_line = now + 1.2
            elif port == 0xff and scene == "sea" and self.destination is None:
                set_destination(debugger, segment, fleet)
                self.destination = BORDEAUX
                self.say("首航剧情已完成，正在自动前往波尔多。")
            return
        if (hero, state[0x0a], state[0x30], state[0x31]) != (0, 0, 0, 0):
            self.pending = None
            self.departure_requested = False
            self.departure_confirmed = False
            self.destination = None
            self.port_confirmed = False
            self.say("当前进度可手动继续。自动路线目前覆盖约翰的开场和首航。")
            return

        flags, = unpack_from("<I", state, 0x32)
        if self.pending is None:
            remaining = [event for event in LISBON_EVENTS if not flags & (1 << event[0])]
            if not remaining:
                remaining = [LISBON_EVENTS[-1]]
            event = next((event for event in remaining if can_visit(event[0], state[9], flags)), None)
            if event is None:
                next_tick = next((tick for tick in range(state[9] + 1, 72)
                                  if any(can_visit(flag, tick, flags) for flag, _, _ in remaining)), None)
                if next_tick is not None and input_scene(debugger, load_segment) == "town":
                    # Keep the daily loop's clock and the saved clock in sync.
                    debugger.write(segment, 0x0535, bytes((next_tick,)))
                    debugger.write(segment, 0x0cad, bytes((next_tick,)))
                    self.say(f"快进到 {next_tick // 3:02d}:{next_tick % 3 * 20:02d}，继续下一段剧情。")
                return

        scene = input_scene(debugger, load_segment)
        if self.pending is not None:
            flag, _, label = self.pending
            if scene == "town" and flag != 10 and flags & (1 << flag):
                self.say(f"已确认完成{label}。")
                self.pending = None
            elif now - self.started > 90:
                self.say(f"等待{label}的完成标记，可在游戏里手动继续。")
            elif now >= self.next_line:
                key = None
                if scene == "dialogue" or (scene == "story_choice" and flag == 10):
                    key = ENTER
                elif flag == 10 and scene == "departure_choice" and self.departure_requested and not self.departure_confirmed:
                    key = ENTER
                elif flag == 10 and scene == "harbor_menu" and flags & 0x400:
                    if fleet is not None and not self.departure_requested:
                        tail = free_key_slot(debugger)
                        if tail is not None and prepare_crew(debugger, segment, fleet):
                            self.departure_requested = True
                            send_key(debugger, tail, ENTER)
                            self.next_line = now + 1.2
                            self.say("里斯本出航教学已完成，准备出港。")
                elif scene == "building_menu" and flags & (1 << flag):
                    key = ESCAPE
                if key is not None:
                    tail = free_key_slot(debugger)
                    if tail is not None:
                        send_key(debugger, tail, key)
                        if scene == "departure_choice":
                            self.departure_confirmed = True
                        self.next_line = now + 0.9
            return

        if scene != "town":
            return
        tail = free_key_slot(debugger)
        if tail is None:
            return
        _, building, label = event
        x, y = self.doors[building * 2:building * 2 + 2]
        camera_x = max(0, min(x - 12, 72))
        camera_y = max(0, min(y - 13, 72))
        # Stand one tile below the entrance, then let the normal town dispatcher enter it.
        position = pack("<4H", camera_x, camera_y, x - camera_x, y - camera_y + 1)
        debugger.write(segment, 0x0cb4, position)
        send_key(debugger, tail, UP)
        self.pending = event
        self.started = now
        self.next_line = now + 1.2
        self.say(label)


def prepare_crew(debugger, segment, fleet):
    ships = debugger.read(segment, fleet + SHIP_SLOTS, 90)
    supplies = debugger.read(segment, SUPPLY_BASE, 300)
    active = False
    for slot in range(10):
        ship = slot * 9
        if ships[ship + 8] & 0x30 != 0x10:
            continue
        active = True
        crew, = unpack_from("<H", ships, ship)
        navigation = slot * 30 + 9
        if crew < 20:
            debugger.write(segment, fleet + SHIP_SLOTS + ship, pack("<H", 20))
        if supplies[navigation] < 50:
            lookout = min(supplies[navigation - 1], 50)
            debugger.write(segment, SUPPLY_BASE + navigation - 1, bytes((lookout, 50)))
    return active


def set_destination(debugger, segment, fleet):
    # The original Auto Sail command uses these fields to start its graph search.
    coordinates = debugger.read(segment, 0x5f54 + BORDEAUX * 22, 4)
    x, y = unpack("<2H", debugger.read(segment, fleet, 4))
    mode = 0x4000
    if max(abs(x - BORDEAUX_APPROACH[0]), abs(y - BORDEAUX_APPROACH[1])) <= 24:
        # Use the game's local destination mode to approach the port from its river.
        coordinates = pack("<2H", *BORDEAUX_APPROACH)
        mode = 0x6000
    flags = debugger.read(segment, 0x1005, 1)[0]
    debugger.write(segment, fleet + 4, coordinates)
    debugger.write(segment, fleet + 0x0c, pack("<H", mode))
    debugger.write(segment, fleet + 0x1b, b"\x00")
    debugger.write(segment, 0x1005, bytes(((flags & ~0x20) | 0x80,)))


def can_visit(flag, tick, flags):
    if flag == 5:
        return tick < 12 or tick >= 24
    if flag == 2:
        return tick >= 12
    if flag == 3:
        return 12 <= tick < 60
    if flag == 1:
        return tick >= 66
    if flag == 10:
        return flags & 0x2f == 0x2f
    return True


def input_scene(debugger, load_segment):
    line = debugger.command("EXEC EV CS IP SS SP").splitlines()[-1]
    cs, ip, ss, sp = (int(word, 16) for word in line.split())
    file_pc = (cs - load_segment) * 16 + ip + 0x5200
    stack = debugger.read(ss, sp, min(192, 0x10000 - sp))

    dialogue_ip, dialogue_cs = DIALOGUE_RETURN
    if pack("<HH", dialogue_ip, load_segment + dialogue_cs) in stack:
        return "dialogue"
    choice_ip, choice_cs = STORY_CHOICE_RETURN
    if pack("<HH", choice_ip, load_segment + choice_cs) in stack:
        return "story_choice"
    for scene, (ip, cs) in (
        ("departure_choice", DEPARTURE_CHOICE_RETURN),
        ("port_call", PORT_CALL_RETURN),
        ("arrival_report", ARRIVAL_REPORT_RETURN),
        ("harbor_menu", HARBOR_MENU_RETURN),
        ("sea_menu", SEA_MENU_RETURN),
    ):
        if pack("<HH", ip, load_segment + cs) in stack:
            return scene
    if all(pack("<HH", menu_ip, load_segment + menu_cs) in stack for menu_ip, menu_cs in BUILDING_MENU_RETURNS):
        return "building_menu"
    if 0x20b99 <= file_pc < 0x20d01:
        return "town"
    if any(pack("<HH", town_ip, load_segment + town_cs) in stack for town_ip, town_cs in TOWN_RETURNS):
        return "town"
    if 0x20484 <= file_pc < 0x204ba or any(
        pack("<HH", ip, load_segment + 0x0fea) in stack for ip in SEA_TICK_RETURNS
    ):
        return "sea"
    return None


def free_key_slot(debugger):
    head, tail = unpack("<2H", debugger.read(0x40, 0x1a, 4))
    if head == tail and 0x1e <= tail <= 0x3c and not tail % 2:
        return tail
    return None


def send_key(debugger, tail, key):
    debugger.write(0x40, tail, key)
    debugger.write(0x40, 0x1c, pack("<H", 0x1e if tail == 0x3c else tail + 2))
