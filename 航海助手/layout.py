"""繁体中文版 DOS 存档与游戏内存的数据布局。

存档 KOUKAI2.DAT 由文件头和 10 个存档槽组成。游戏运行时，内存里从
数据段 LIVE_BASE 起放着同样结构的当前进度，所以下面的槽内偏移对存档和
内存都适用：存档里加槽起点，内存里加 LIVE_BASE。

标注「含义未确认」的字段只按观察到的取值使用，改动时保持原样。
"""

# ---- 存档文件 ----
HEADER_SIZE = 0x97
SLOT_COUNT = 10
SLOT_SIZE = 0x8074
SLOT_HEADER_SIZE = 15  # 文件头里每槽一条：日期 +1..+12，所在港口 +13。
SLOT_HEADER_DATE = slice(1, 12)
SLOT_HEADER_PORT = 13

# ---- 内存 ----
LIVE_BASE = 0xca4  # 当前进度在数据段里的起点。
FLEET_POINTER = 0xc2d4  # 数据段绝对偏移：玩家舰队记录的地址，只在内存里有。

# ---- 槽内：基本状态 ----
CALENDAR = 6  # 4 字节日期相关字段，片头和标题画面时不在有效范围，用于判断是否已载入角色。
CURRENT_PORT = 0x0a  # 所在港口编号，海上为 0xff。
STORY_PROGRESS = 0x30  # 章节、分支各 1 字节，随后 4 字节剧情旗标。
CHART_MASK = 0x144  # 世界地图已探索位图，每行 12 字节。
CHART_COUNT = 0x36a  # 已探索格数。
CHART_EXTRA_MASK = 0x3a2  # 另一块探索位图，含义未完全确认。
MAP_MODE = 0x361  # 低 3 位保留；瞬移进港写 0x20，停在海上写 0x80。
MAP_ORIGIN = 0x362  # 地图视口左上角，2 个 uint16。
FAME = 0x5b6  # 每位主角 3 个 uint16：商业、海盗、冒险。
FAME_STRIDE = 14
GOLD = 0x60a  # uint32
HERO = 0x611  # 当前主角编号，对应 sea2.HEROES。

# ---- 槽内：人物 ----
OFFICERS = 0x612
OFFICER_SIZE = 50
OFFICER_COUNT = 120  # 0–5 为主角，79 起为可招募的航海士。
RECRUIT_FIRST = 79
OFFICER_ABILITIES = 28  # 8 项能力，各 1 字节。
OFFICER_FLEET = 44  # 所属舰队编号，0xff 表示未入队。
OFFICER_EXTRA_STATS = (36, 37)  # 整备舰队时至少设为 100，含义未确认。
# +43、+45、+46 含义未确认：新船长写入 100、0xff、2；可招募者 +45 小于 100 且 +46 为 0。

# ---- 槽内：舰队 ----
PERSONNEL = slice(0x2177, 0x2195)  # 人物菜单使用的航海士编号，0xff 表示空位。
PERSONNEL_WAGES = 0x2195  # 与人物名单同序，工资以 10 枚金币为单位。
INVENTORY = slice(0x21b3, 0x21c7)  # 持有物品编号。
FLEETS = 0x21d2
FLEET_SIZE = 0x85
FLEET_COUNT = 70
FLEET_OWNER = 0x2a  # 舰队所属人物编号。
FLEET_SHIPS = 0x2b  # 10 个船位。
FLEET_SHIP_LIMIT = 10
# 船位：水手数 uint16，随后 5 字节含义未确认，再后是船只编号和状态。
SHIP_SLOT_SIZE = 9
SHIP_SLOT_INSTANCE = 7  # 对应 SHIPS 里的记录编号。
SHIP_SLOT_STATUS = 8
SHIP_SLOT_STATE_MASK = 0x30
SHIP_SLOT_ACTIVE = 0x10  # 与 STATE_MASK 相与后为此值表示船在队里，为 0 表示空位。

# ---- 槽内：每个船位的补给 ----
SUPPLIES = 0x4630
SUPPLY_SIZE = 30
# 前 4 个 uint16 为淡水、食物、修船资材、炮弹；淡水和食物以 0.1 桶计。
SUPPLY_CARGO = 12  # 5 个 uint16 的货物数量。
SUPPLY_CAPTAIN = 27  # 船长的人物编号，0xff 表示无船长。

# ---- 槽内：船只 ----
SHIPS = 0x4bee
SHIP_SIZE = 22
SHIP_COUNT = 65
SHIP_PLAYER_LIMIT = 40  # 玩家船只只用前 40 条记录。
SHIP_NAME = slice(0, 15)
SHIP_TYPE = 15  # 0xff 表示空记录。
SHIP_CANNONS = 17  # 0 表示没有舰炮，不需要补炮弹。
SHIP_CAPACITY = 20  # uint16

# ---- 槽内：港口与发现物 ----
PORTS = 0x52b0
PORT_SIZE = 22  # 前 2 个 uint16 是港口坐标。
PORT_FLAGS = 21
PORT_CHARTED = 0x10  # 地图上已标出。
DISCOVERIES = 0x72e8
DISCOVERY_SIZE = 7  # x、y uint16，物品编号，1 字节未确认，旗标。
DISCOVERY_COUNT = 98
DISCOVERY_FLAGS = 6
