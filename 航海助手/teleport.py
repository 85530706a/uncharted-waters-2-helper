"""Move one saved fleet to a harbor, keeping the game's ordinary sailing mode."""

import fcntl
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from shutil import copymode
from struct import pack, pack_into, unpack_from
from tempfile import NamedTemporaryFile

import layout
from live import COAST_TELEPORT
from patches import replace_game_file
from sea2 import HEADER_SIZE, HEROES, SAVE, SLOT_SIZE, read_slots, write_copy
from story_destinations import CoastMap, read_ports, route_targets

ROOT = Path(__file__).resolve().parent
GAME = ROOT / "游戏副本"
PINNED_PORTS = (2, 76, 13, 0, 98, 99)


def choose(prompt, options, title="港口瞬移"):
    command = '''
on run argv
    set selected to choose from list (items 3 thru -1 of argv) with title (item 2 of argv) with prompt (item 1 of argv)
    if selected is false then return ""
    return item 1 of selected
end run
'''
    return subprocess.check_output(
        ["osascript", "-e", command, prompt, title, *options], text=True,
    ).strip()


def map_origin(ship_x, ship_y):
    return (
        ship_x // 720 * 720 + max(0, min(ship_x % 720 - 11, 696)),
        max(0, min(ship_y - 11, 1056)),
    )


def choose_destination(game_pid=None):
    ports = read_ports()
    order = (*PINNED_PORTS, *(port for port in ports if port not in PINNED_PORTS))
    options = {f"{ports[port][-1]}  ·  {port:03d}": ports[port] for port in order}
    return choose_live(options, game_pid)


def choose_live(options, game_pid, *, story=False):
    picker = [str(ROOT / "港口瞬移.app" / "Contents" / "MacOS" / "pick-port")]
    if game_pid is not None:
        picker.extend(("--game-pid", str(game_pid)))
    if story:
        picker.append("--story")
    target = subprocess.check_output([*picker, *options], text=True).strip()
    return options.get(target)


def queue_live_destination(lock, *, story=False):
    lock.seek(0)
    session = lock.read().strip()
    if not session:
        raise ValueError("当前窗口还不支持游戏中瞬移。请保存退出，再从航海修改器勾选「港口瞬移」启动。")
    folder = Path(session)
    if not folder.name.startswith("sea2-live-") or not (folder / "game").is_symlink() or (
        folder / "game"
    ).resolve() != GAME.resolve():
        raise ValueError("当前游戏的瞬移通道未就绪。请从航海修改器勾选「港口瞬移」启动。")
    try:
        game_pid = int((folder / "dosbox.pid").read_text())
    except (FileNotFoundError, ValueError):
        game_pid = None
    if story:
        routes = folder / "story-destinations.json"
        try:
            choices = json.loads(routes.read_text())
        except FileNotFoundError as error:
            raise ValueError("请勾选「主线目的地导航」启动修改版，再进入存档或新游戏。") from error
        if not choices:
            raise ValueError("当前章节没有固定港口或已取得的藏宝图。")
        target = choose_live(choices, game_pid, story=True)
        if isinstance(target, list):
            if not (folder / "coast-teleport").exists():
                raise ValueError("当前打开的是更新前的游戏窗口。请保存后退出一次，勾选「主线目的地导航」重新启动。")
            ship_x, ship_y = target
            queue_transfer(folder, COAST_TELEPORT, ship_x, ship_y)
            return
        destination = read_ports().get(target)
    else:
        destination = choose_destination(game_pid)
    if destination is None:
        return
    port, _, _, ship_x, ship_y, name = destination
    queue_transfer(folder, port + 1, ship_x, ship_y)
    print(f"已选 {name}。在街上或航行时会自动瞬移。对话或菜单打开时，退出后生效。", flush=True)


def queue_transfer(folder, marker, ship_x, ship_y):
    try:
        replace_game_file(folder / "teleport.request", pack(
            "<B4H", marker, ship_x, ship_y, *map_origin(ship_x, ship_y),
        ))
    except FileNotFoundError as error:
        raise ValueError("选港期间游戏已退出，请重新打开港口瞬移。") from error


def choose_story_destination(choices):
    if not choices:
        raise ValueError("当前章节没有固定港口或已取得的藏宝图。若线索只指定地区，请先在该地区触发下一段对白。")
    name = choose("选择要前往的地点。港口会保留原有剧情触发条件。", list(choices), "主线目的地")
    return choices.get(name)


def place_saved_fleet(data, slot, port, ship_x, ship_y):
    base = HEADER_SIZE + (slot.number - 1) * SLOT_SIZE
    fleets = [base + layout.FLEETS + i * layout.FLEET_SIZE for i in range(layout.FLEET_COUNT)]
    status = layout.FLEET_SHIPS + layout.SHIP_SLOT_STATUS
    active = [
        fleet for fleet in fleets
        if data[fleet + layout.FLEET_OWNER] == slot.hero
        and any(data[fleet + status + ship * layout.SHIP_SLOT_SIZE] & layout.SHIP_SLOT_STATE_MASK
                == layout.SHIP_SLOT_ACTIVE for ship in range(layout.FLEET_SHIP_LIMIT))
    ]
    if len(active) != 1:
        raise ValueError("这个存档里找不到可瞬移的主角船队。")
    fleet = active[0]
    result = bytearray(data)
    result[(slot.number - 1) * layout.SLOT_HEADER_SIZE + layout.SLOT_HEADER_PORT] = port
    # The byte after the current port and the flags at +0x0f are unconfirmed.
    result[base + layout.CURRENT_PORT:base + layout.CURRENT_PORT + 2] = bytes((port, 0xff))
    result[base + 0x0f] &= 0xc0
    mode = base + layout.MAP_MODE
    result[mode] = (result[mode] & 7) | (0x80 if port == 0xff else 0x20)
    pack_into("<2H", result, base + layout.MAP_ORIGIN, *map_origin(ship_x, ship_y))
    pack_into("<6H", result, fleet, ship_x, ship_y, 0, 0, ship_x, ship_y)
    # Fleet +0x0c and +0x18..+0x1a are unconfirmed sailing fields; reset as in a fresh departure.
    pack_into("<H", result, fleet + 0x0c, 0)
    result[fleet + 0x18:fleet + 0x1b] = b"\0\0\x08"
    if port != 0xff:
        result[base + layout.PORTS + port * layout.PORT_SIZE + layout.PORT_FLAGS] |= 0x50
    return bytes(result)


def move_save(data, slot, destination):
    port, port_x, port_y, ship_x, ship_y, _ = destination
    base = HEADER_SIZE + (slot.number - 1) * SLOT_SIZE
    if unpack_from("<HH", data, base + layout.PORTS + port * layout.PORT_SIZE) != (port_x, port_y):
        raise ValueError("港口位置与中文版存档不符。")
    return place_saved_fleet(data, slot, port, ship_x, ship_y)


def main(*, story=False):
    with (GAME / ".sea2-session.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            queue_live_destination(lock, story=story)
            return False
        save = GAME / "KOUKAI2.DAT"
        data = save.read_bytes()
        slots = {f"第 {s.number} 槽 · {HEROES[s.hero]} · {s.date.strip()}": s for s in read_slots(data)}
        selected = choose("选要瞬移的存档。修改前会备份。", list(slots))
        if not selected:
            return False
        slot = slots[selected]
        if story:
            base = HEADER_SIZE + (slot.number - 1) * SLOT_SIZE
            bank = (GAME / f"SNR{slot.hero + 1}.DAT").read_bytes()
            coast = CoastMap(SAVE.with_name("WORLDMAP.LZW").read_bytes(), SAVE.with_name("MAIN.EXE").read_bytes())
            ports = read_ports()
            target = choose_story_destination(route_targets(data[base:base + SLOT_SIZE], bank, ports, coast))
            if target is None:
                return False
            if isinstance(target, tuple):
                updated = place_saved_fleet(data, slot, 0xff, *target)
                destination_name = f"藏宝点外海（{target[0]}, {target[1]}）"
            else:
                updated = move_save(data, slot, ports[target])
                destination_name = ports[target][-1]
        else:
            destination = choose_destination()
            if destination is None:
                return False
            updated = move_save(data, slot, destination)
            destination_name = destination[-1]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        write_copy(save, save.with_name(f"KOUKAI2.瞬移前.{stamp}.DAT"), data)
        with NamedTemporaryFile(dir=GAME, delete=False) as file:
            staged = Path(file.name)
            try:
                file.write(updated)
                file.close()
                copymode(save, staged)
                if save.read_bytes() != data:
                    raise ValueError("存档在选择时更新了，请重新打开港口瞬移。")
                staged.replace(save)
            finally:
                staged.unlink(missing_ok=True)
        print(f"已将第 {slot.number} 槽移到 {destination_name}。请读取该槽。")
        if story:
            subprocess.run(["osascript", "-e", 'on run argv\n display alert (item 1 of argv)\nend run',
                            f"已将第 {slot.number} 槽移到 {destination_name}。请读取该槽。"], check=True)
        return True


if __name__ == "__main__":
    try:
        main(story="--story-destinations" in sys.argv[1:])
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        subprocess.run(["osascript", "-e", 'on run argv\n display alert (item 1 of argv)\nend run', str(error)])
