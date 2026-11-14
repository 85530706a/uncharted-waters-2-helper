import AppKit

let app = NSApplication.shared
let bundle = Bundle.main
let workspace = bundle.bundleURL.deletingLastPathComponent()
let mode = bundle.bundleIdentifier?.split(separator: ".").last.map(String.init) ?? ""

final class GameLaunch {
    let process = Process()
    let focusDirectory = FileManager.default.temporaryDirectory
        .appendingPathComponent("sea2-focus-\(UUID().uuidString)", isDirectory: true)
    var focusFile: URL { focusDirectory.appendingPathComponent("dosbox.pid") }

    init(mode: String, arguments: [String] = []) throws {
        process.executableURL = workspace.appendingPathComponent("launch_gui.sh")
        process.arguments = [mode] + arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        if mode == "game" || mode == "trainer" {
            try FileManager.default.createDirectory(at: focusDirectory, withIntermediateDirectories: false)
            var environment = ProcessInfo.processInfo.environment
            environment["SEA2_FOCUS_FILE"] = focusFile.path
            process.environment = environment
        }
    }

    deinit {
        try? FileManager.default.removeItem(at: focusDirectory)
    }

    func focusGame() {
        let deadline = Date().addingTimeInterval(15)
        while process.isRunning && Date() < deadline {
            if let value = try? String(contentsOf: focusFile, encoding: .utf8),
               let pid = Int32(value),
               let game = NSRunningApplication(processIdentifier: pid),
               game.activationPolicy != .prohibited {
                if #available(macOS 14.0, *) {
                    app.activate()
                    app.yieldActivation(to: game)
                    if game.activate(from: .current, options: [.activateAllWindows]) { return }
                } else if game.activate(options: [.activateAllWindows, .activateIgnoringOtherApps]) {
                    return
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
    }
}

func showError(_ error: Error) {
    app.activate(ignoringOtherApps: true)
    let alert = NSAlert(error: error)
    alert.messageText = "航海助手启动失败"
    alert.runModal()
}

final class TrainerWindow: NSObject, NSApplicationDelegate {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 530, height: 604),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
    let options: [(String, String)] = [
        ("supplies", "航海补给锁定"), ("hazards", "防止海上灾害造成损失"),
        ("sea_guard", "海战无敌"), ("boarding", "接舷肉搏一击必杀"),
        ("cannon", "舰炮一击击沉"),
        ("duel_guard", "船长单挑无敌"), ("duel", "船长单挑一击必杀"),
        ("fast", "加快海上航行"), ("daylight", "白天画面、全天营业"),
        ("opening", "片头可立即按回车跳过"),
        ("pins", "委任航行常用港口置顶"), ("teleport", "启用游戏内港口瞬移"),
        ("map", "全开地图及已知港口"),
        ("route", "主线目的地导航"), ("discoveries", "全开村落及发现物"),
    ]
    let values: [(String, String, Int)] = [
        ("gold", "金钱", 600_000_000), ("trade", "商业声望", 50_000),
        ("piracy", "海盗声望", 50_000), ("adventure", "冒险声望", 50_000),
    ]
    var switches: [NSButton] = []
    var inputs: [NSTextField] = []
    let start = NSButton(title: "启动修改版", target: nil, action: nil)
    let status = NSTextField(labelWithString: "使用游戏副本现有存档。")
    let fleet = NSButton(checkboxWithTitle: "整备战斗舰队", target: nil, action: nil)
    let fleetFields = [("存档槽", 1, 10), ("船数", 10, 10), ("每船水手", 150, 150)]
    var fleetInputs: [NSTextField] = []
    var launch: GameLaunch?

    func applicationDidFinishLaunching(_ notification: Notification) {
        window.title = "大航海时代 2 · 航海修改器"
        window.isReleasedWhenClosed = false
        guard let view = window.contentView else { return }

        let heading = NSTextField(labelWithString: "航海与战斗")
        heading.font = .boldSystemFont(ofSize: 16)
        heading.frame = NSRect(x: 24, y: 500, width: 470, height: 24)
        view.addSubview(heading)

        for (index, option) in options.enumerated() {
            let control = NSButton(checkboxWithTitle: option.1, target: nil, action: nil)
            control.frame = NSRect(x: 24 + (index % 2) * 240, y: 468 - (index / 2) * 29,
                                   width: 234, height: 24)
            view.addSubview(control)
            switches.append(control)
        }

        let stats = NSTextField(labelWithString: "金钱与声望")
        stats.font = .boldSystemFont(ofSize: 16)
        stats.frame = NSRect(x: 24, y: 219, width: 480, height: 24)
        view.addSubview(stats)
        let hint = NSTextField(labelWithString: "低于填写值时自动补回。留空按游戏规则变化。")
        hint.textColor = .secondaryLabelColor
        hint.frame = NSRect(x: 24, y: 195, width: 480, height: 20)
        view.addSubview(hint)

        for (index, field) in values.enumerated() {
            let x = 24 + (index % 2) * 240
            let y = 158 - (index / 2) * 36
            let label = NSTextField(labelWithString: field.1)
            label.frame = NSRect(x: x, y: y, width: 70, height: 22)
            let input = NSTextField()
            input.placeholderString = "0–\(field.2)"
            input.stringValue = String(field.2)
            input.frame = NSRect(x: x + 75, y: y, width: 137, height: 24)
            label.toolTip = field.1
            input.setAccessibilityLabel(field.1)
            view.addSubview(label)
            view.addSubview(input)
            inputs.append(input)
        }
        fleet.frame = NSRect(x: 24, y: 556, width: 155, height: 24)
        view.addSubview(fleet)
        for (index, field) in fleetFields.enumerated() {
            let x = 168 + index * 112
            let label = NSTextField(labelWithString: field.0)
            label.frame = NSRect(x: x, y: 557, width: 64, height: 22)
            let input = NSTextField(string: String(field.1))
            input.frame = NSRect(x: x + 64, y: 555, width: 40, height: 24)
            input.setAccessibilityLabel(field.0)
            view.addSubview(label)
            view.addSubview(input)
            fleetInputs.append(input)
        }
        let fleetHint = NSTextField(labelWithString: "启动前整备指定存档。随机招募缺位船长，并开启全舰队炮击和接舷秒杀。")
        fleetHint.textColor = .secondaryLabelColor
        fleetHint.frame = NSRect(x: 24, y: 527, width: 482, height: 20)
        view.addSubview(fleetHint)

        status.frame = NSRect(x: 24, y: 73, width: 480, height: 22)
        status.textColor = .secondaryLabelColor
        view.addSubview(status)
        start.target = self
        start.action = #selector(startGame)
        start.frame = NSRect(x: 370, y: 24, width: 136, height: 32)
        start.bezelStyle = .rounded
        start.keyEquivalent = "\r"
        view.addSubview(start)
        let teleport = NSButton(title: "港口瞬移…", target: self, action: #selector(openTeleport))
        teleport.frame = NSRect(x: 20, y: 24, width: 126, height: 32)
        teleport.bezelStyle = .rounded
        view.addSubview(teleport)
        let route = NSButton(title: "主线目的地…", target: self, action: #selector(openRoute))
        route.frame = NSRect(x: 151, y: 24, width: 140, height: 32)
        route.bezelStyle = .rounded
        view.addSubview(route)
        window.center()
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    @objc func openTeleport() {
        NSWorkspace.shared.open(workspace.appendingPathComponent("港口瞬移.app"))
    }

    @objc func openRoute() {
        do {
            let route = try GameLaunch(mode: "route")
            try route.process.run()
            DispatchQueue.global().async { route.process.waitUntilExit() }
        } catch {
            showError(error)
        }
    }

    @objc func startGame() {
        guard launch == nil else { return }
        var arguments: [String] = []
        for (option, control) in zip(options, switches) where control.state == .on {
            arguments += ["--enable", option.0]
        }
        if fleet.state == .on {
            let flags = ["--fleet-slot", "--fleet-ships", "--fleet-crew"]
            for (index, field) in fleetFields.enumerated() {
                let input = fleetInputs[index].stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
                guard let value = Int(input), (1...field.2).contains(value) else {
                    let alert = NSAlert()
                    alert.messageText = "请输入 \(field.0) 的整数"
                    alert.informativeText = "允许范围为 1–\(field.2)。"
                    alert.beginSheetModal(for: window)
                    return
                }
                arguments += [flags[index], String(value)]
            }
        }
        for (field, input) in zip(values, inputs) {
            let text = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty { continue }
            guard let value = Int(text), (0...field.2).contains(value) else {
                let alert = NSAlert()
                alert.messageText = "请输入 \(field.1) 的整数"
                alert.informativeText = "允许范围为 0–\(field.2)，也可以留空。"
                alert.beginSheetModal(for: window)
                return
            }
            arguments += ["--\(field.0)", String(value)]
        }
        do {
            let session = try GameLaunch(mode: "trainer", arguments: arguments)
            session.process.terminationHandler = { [weak self] process in
                let success = process.terminationStatus == 0
                DispatchQueue.main.async {
                    self?.launch = nil
                    self?.start.isEnabled = true
                    self?.status.stringValue = success ? "游戏已退出，可以重新选择开关。" : "启动失败，可以重试。"
                }
            }
            try session.process.run()
            launch = session
            start.isEnabled = false
            status.stringValue = "修改版正在运行。更改开关后，请退出游戏再启动。"
            DispatchQueue.main.async { session.focusGame() }
        } catch {
            showError(error)
        }
    }

}

if mode == "trainer" {
    app.setActivationPolicy(.regular)
    let trainer = TrainerWindow()
    app.delegate = trainer
    withExtendedLifetime(trainer) { app.run() }
} else {
    do {
        let launch = try GameLaunch(mode: mode)
        try launch.process.run()
        if mode == "game" {
            app.setActivationPolicy(.accessory)
            launch.focusGame()
        }
        launch.process.waitUntilExit()
    } catch {
        app.setActivationPolicy(.accessory)
        showError(error)
    }
}
