import AppKit

func matchingPorts(_ ports: [String], query: String) -> [String] {
    let transform = StringTransform("Traditional-Simplified")
    let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return ports }
    let term = trimmed.applyingTransform(transform, reverse: false) ?? trimmed
    return ports.filter { port in
        let name = port.applyingTransform(transform, reverse: false) ?? port
        return name.localizedStandardContains(term)
    }
}

final class PortPicker: NSObject, NSSearchFieldDelegate {
    let ports: [String]
    let alert = NSAlert()
    let search = NSSearchField()
    let results = NSPopUpButton()

    init(ports: [String], story: Bool) {
        self.ports = ports
        super.init()
        alert.messageText = story ? "选择主线目的地" : "选择瞬移目的港"
        alert.informativeText = story ? "选中藏宝点可直达附近水面。抵达后在游戏里选择「登陆」。" :
            "输入港名的一部分或三位编号，再确认下方港口。"
        alert.addButton(withTitle: "瞬移")
        alert.addButton(withTitle: "取消")

        let form = NSView(frame: NSRect(x: 0, y: 0, width: 350, height: 64))
        search.frame = NSRect(x: 0, y: 36, width: 350, height: 24)
        search.placeholderString = "搜索目的地"
        search.setAccessibilityLabel("搜索目的地")
        search.delegate = self
        results.frame = NSRect(x: 0, y: 2, width: 350, height: 26)
        results.setAccessibilityLabel("匹配的目的地")
        form.addSubview(search)
        form.addSubview(results)
        alert.accessoryView = form
        refresh()
    }

    func controlTextDidChange(_ notification: Notification) {
        refresh()
    }

    func refresh() {
        let matches = matchingPorts(ports, query: search.stringValue)
        results.removeAllItems()
        results.addItems(withTitles: matches.isEmpty ? ["没有找到目的地"] : matches)
        results.isEnabled = !matches.isEmpty
        alert.buttons[0].isEnabled = !matches.isEmpty
    }

    func choose() -> String? {
        alert.layout()
        alert.window.initialFirstResponder = search
        alert.window.makeFirstResponder(search)
        guard alert.runModal() == .alertFirstButtonReturn, results.isEnabled else { return nil }
        return results.titleOfSelectedItem
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let menu = NSMenu()
let editItem = NSMenuItem()
let editMenu = NSMenu(title: "编辑")
editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
editItem.submenu = editMenu
menu.addItem(editItem)
app.mainMenu = menu
app.activate()
var arguments = Array(CommandLine.arguments.dropFirst())
var gamePID: Int32?
if arguments.count >= 2, arguments[0] == "--game-pid" {
    gamePID = Int32(arguments[1])
    arguments.removeFirst(2)
}
let story = arguments.first == "--story"
if story { arguments.removeFirst() }
let picker = PortPicker(ports: arguments, story: story)
if let destination = picker.choose() {
    print(destination)
    if let gamePID, gamePID > 0, let game = NSRunningApplication(processIdentifier: gamePID) {
        if #available(macOS 14.0, *) {
            app.yieldActivation(to: game)
            _ = game.activate(from: .current, options: [.activateAllWindows])
        } else {
            _ = game.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        }
    }
}
