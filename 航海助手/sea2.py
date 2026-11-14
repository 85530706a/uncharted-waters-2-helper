"""Inspect this Chinese DOS release's save slots and write separate edited copies."""

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from struct import iter_unpack, pack_into, unpack_from

SAVE = Path(__file__).resolve().parent / ".原版资源" / "KOUKAI2.DAT"
HEADER_SIZE = 0x97
SLOT_SIZE = 0x8074
SLOT_COUNT = 10
FAME = ("trade", "piracy", "adventure")
ORIGINAL_EXE_SHA256 = "16392e537eff22374047f4a6b59917741faaef411cb4a658f065a38e1e9b4806"
HEROES = ("约翰", "卡特琳娜", "奥托", "恩斯特", "彼得", "阿兰")
BUILDINGS = ("交易所", "酒馆", "造船厂", "港口", "旅馆", "王宫", "公会", "特殊设施",
             "银行", "商店", "教堂或清真寺", "占卜馆")
STORY_SELECTORS = {0xa0: "航行日数", 0xa1: "海战前", 0xa2: "海战后", 0xa3: "任意普通港口"}


@dataclass(frozen=True)
class Slot:
    number: int
    date: str
    hero: int
    port: int
    gold: int
    fame: tuple


def read_slots(data):
    if len(data) != HEADER_SIZE + SLOT_SIZE * SLOT_COUNT:
        raise ValueError("存档长度不匹配。此工具只支持 328991 字节的中文版。")
    slots = []
    for index in range(SLOT_COUNT):
        header = index * 15
        date = data[header + 1:header + 12]
        if date == bytes(11):
            continue
        if not re.fullmatch(rb"[ 0-9][0-9]/ [ 0-9][0-9]/[0-9]{4}", date):
            raise ValueError(f"存档 {index + 1} 的日期格式无法识别。")
        port = data[header + 13]
        base = HEADER_SIZE + index * SLOT_SIZE
        hero = data[base + 0x611]
        if hero >= len(HEROES) or (port >= 138 and port != 0xff):
            raise ValueError(f"存档 {index + 1} 的角色或港口编号无法识别。")
        gold, = unpack_from("<I", data, base + 0x60a)
        fame = unpack_from("<3H", data, base + 0x5b6 + hero * 14)
        slots.append(Slot(index + 1, date.decode("ascii"), hero, port, gold, fame))
    return slots


def story_sections(data):
    if data[:4] != b"SNDT":
        raise ValueError("剧情文件缺少 SNDT 标识。")
    sections = []
    for cursor in range(16, len(data) - 3, 4):
        offset, = unpack_from(">I", data, cursor)
        if offset == 0xffffffff and sections:
            return sections
        if not cursor < offset < len(data) or (sections and offset <= sections[-1]):
            raise ValueError("剧情章节的索引超出文件范围。")
        sections.append(offset)
    raise ValueError("剧情章节索引缺少终止标志。")


def story_entries(data, chapter, subsection):
    sections = story_sections(data)
    if not 0 <= chapter < len(sections):
        return []
    start = sections[chapter]
    end = (sections + [len(data)])[chapter + 1]
    size, = unpack_from(">H", data, start)
    table = start + size
    if size < 4 or size % 2 or table > end or data[table - 2:table] != b"\xff\xff":
        return []

    if subsection:
        entries = [start + relative for relative, in iter_unpack(">H", data[start + 2:table - 2])]
        if not 1 <= subsection <= len(entries):
            return []
        table = entries[subsection - 1]
        if not start + size <= table < end:
            raise ValueError("剧情分支的索引超出章节范围。")

    routes = []
    for cursor in range(table, end - 3, 4):
        selector, qualifier, relative = unpack_from(">BBH", data, cursor)
        if (selector, qualifier, relative) == (0xff, 0xff, 0xffff):
            return routes
        if table + relative >= end:
            raise ValueError("剧情入口超出章节范围。")
        route = {"selector": selector, "qualifier": qualifier, "offset": table + relative}
        if selector < 100:
            route["port"] = selector
        elif selector in STORY_SELECTORS:
            route["event"] = STORY_SELECTORS[selector]
        if selector < 100 or selector == 0xa3:
            if qualifier < len(BUILDINGS):
                route["building"] = BUILDINGS[qualifier]
            elif qualifier == 0xff:
                route["building"] = "任意设施"
        routes.append(route)
    raise ValueError("剧情入口表缺少终止标志。")


def edit_slot(data, number, *, gold=None, fame=None):
    slot = next((slot for slot in read_slots(data) if slot.number == number), None)
    if slot is None:
        raise ValueError(f"找不到存档槽 {number}。")
    result = bytearray(data)
    base = HEADER_SIZE + (number - 1) * SLOT_SIZE
    if gold is not None:
        if not 0 <= gold <= 600_000_000:
            raise ValueError("金币必须介于 0 和 600000000。")
        pack_into("<I", result, base + 0x60a, gold)
    for category, value in (fame or {}).items():
        if category not in FAME or not 0 <= value <= 50_000:
            raise ValueError("声望类别无效，或数值超出 0 到 50000。")
        offset = base + 0x5b6 + slot.hero * 14 + FAME.index(category) * 2
        pack_into("<H", result, offset, value)
    return bytes(result)


def patch_provisions(image):
    if hashlib.sha256(image).hexdigest() != ORIGINAL_EXE_SHA256:
        raise ValueError("主程序版本不匹配。请使用这份中文版原始 MAIN.EXE。")
    result = bytearray(image)
    # Skip only the two midnight stock deductions, preserving crew health logic.
    result[0x1e2a9:0x1e2ac] = b"\x90" * 3
    result[0x1e35b:0x1e35d] = b"\x90" * 2
    return bytes(result)


def write_copy(source, output, data):
    if source.resolve() == output.resolve():
        raise ValueError("请为修改后的存档指定不同的输出路径。")
    with output.open("xb") as file:
        try:
            file.write(data)
            file.flush()
        except OSError:
            output.unlink()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", type=Path, default=SAVE, help="输入 KOUKAI2.DAT")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="读取存档槽、金币和三项声望")
    story = commands.add_parser("story", help="读取当前章节和候选事件入口")
    story.add_argument("slot", type=int, choices=range(1, SLOT_COUNT + 1))
    story.add_argument("--data", type=Path, default=SAVE.parent, help="含有 SNR*.DAT 的目录")
    edit = commands.add_parser("edit", help="编辑一个槽位，输出新的存档文件")
    edit.add_argument("slot", type=int, choices=range(1, SLOT_COUNT + 1))
    edit.add_argument("--output", type=Path, required=True)
    edit.add_argument("--gold", type=int)
    for category in FAME:
        edit.add_argument(f"--{category}", type=int)
    provisions = commands.add_parser("patch-provisions", help="生成日常航行不扣粮水的 MAIN.EXE 副本")
    provisions.add_argument("--exe", type=Path, default=SAVE.with_name("MAIN.EXE"))
    provisions.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "patch-provisions":
            write_copy(args.exe, args.output, patch_provisions(args.exe.read_bytes()))
            print(args.output.resolve())
            return

        data = args.save.read_bytes()
        if args.command == "inspect":
            rows = [
                dict(asdict(slot), hero=HEROES[slot.hero], fame=dict(zip(FAME, slot.fame)))
                for slot in read_slots(data)
            ]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return

        if args.command == "story":
            slot = next((slot for slot in read_slots(data) if slot.number == args.slot), None)
            if slot is None:
                raise ValueError(f"找不到存档槽 {args.slot}。")
            base = HEADER_SIZE + (slot.number - 1) * SLOT_SIZE
            chapter, subsection, flags = unpack_from("<BBI", data, base + 0x30)
            bank = (args.data / f"SNR{slot.hero + 1}.DAT").read_bytes()
            result = {"hero": HEROES[slot.hero], "port": slot.port, "chapter_index": chapter,
                      "subsection_index": subsection, "flags": f"{flags:08x}",
                      "candidate_entries": story_entries(bank, chapter, subsection)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        fame = {key: getattr(args, key) for key in FAME if getattr(args, key) is not None}
        if args.gold is None and not fame:
            parser.error("至少指定一项金币或声望数值。")
        result = edit_slot(data, args.slot, gold=args.gold, fame=fame)
        write_copy(args.save, args.output, result)
        print(args.output.resolve())
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
