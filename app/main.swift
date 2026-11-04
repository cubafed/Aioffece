// ИИ-Офис: native window around the office UI served by the local hub (hub/server.py).
import Cocoa
import WebKit

let officeURL = URL(string: ProcessInfo.processInfo.environment["AI_OFFICE_URL"] ?? "http://127.0.0.1:4747/")!

/// A thin strip along the top edge that drags the window (the web view covers the transparent title bar).
final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSMenuDelegate {
    var window: NSWindow!
    var status: NSStatusItem!
    var agents: [[String: Any]] = []
    var online = false
    var web: WKWebView!
    var retry: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let cfg = WKWebViewConfiguration()
        cfg.applicationNameForUserAgent = "AIOfficeApp"     // the page shifts its top-left card clear of the traffic lights
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "ИИ-Офис"
        window.isReleasedWhenClosed = false                 // reopened from the menu bar after a close
        window.titlebarAppearsTransparent = true            // the office runs up under the title bar
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 720, height: 480)
        let root = NSView()
        web.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(web)
        let strip = DragStrip()
        strip.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(strip)
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor), web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            web.topAnchor.constraint(equalTo: root.topAnchor), web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            strip.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 80), strip.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            strip.topAnchor.constraint(equalTo: root.topAnchor), strip.heightAnchor.constraint(equalToConstant: 10),
        ])
        window.contentView = root
        window.setFrameAutosaveName("AIOfficeMain")
        if !window.setFrameUsingName("AIOfficeMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        buildMenu()
        buildStatusItem()
        load()
        NSApp.activate(ignoringOtherApps: true)
    }

    func load() {
        retry?.invalidate()
        web.load(URLRequest(url: officeURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5))
    }

    // Hub not running yet (or restarting): show a hint and keep retrying.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        web.loadHTMLString("""
        <html><body style="font:15px -apple-system;display:grid;place-items:center;height:100vh;margin:0;background:#dfe5db;color:#15171c">
        <div style="text-align:center;max-width:460px"><h2>Хаб офиса не отвечает</h2>
        <p>Он запускается сам при входе в систему. Перезапуск из терминала:</p>
        <code>launchctl kickstart -k gui/$(id -u)/ru.ai-office.hub</code>
        <p style="color:#646b76">Пробую подключиться снова каждые 3 секунды…</p></div></body></html>
        """, baseURL: nil)
        retry = Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { [weak self] _ in self?.load() }
    }

    // Links to other sites open in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.navigationType == .linkActivated, let url = action.request.url, url.host != officeURL.host {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    // Closing the window keeps the office in the menu bar; the Dock icon or the menu brings it back.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showOffice(); return true
    }
    @objc func showOffice() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }

    // MARK: menu bar: how many agents work, who waits for you, approve without opening the window
    func buildStatusItem() {
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        status.button?.image = NSImage(systemSymbolName: "building.2", accessibilityDescription: "ИИ-Офис")
        status.button?.imagePosition = .imageLeading
        let menu = NSMenu()
        menu.delegate = self
        status.menu = menu
        poll()
        Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in self?.poll() }
    }
    func poll() {
        URLSession.shared.dataTask(with: officeURL.appendingPathComponent("api/state")) { [weak self] data, _, _ in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.online = json != nil
                self.agents = (json?["agents"] as? [[String: Any]]) ?? []
                self.paintStatus()
            }
        }.resume()
    }
    func count(_ st: String) -> Int { agents.filter { $0["status"] as? String == st }.count }
    func paintStatus() {
        let working = count("working"), waiting = agents.filter { $0["approval"] is String }.count
        var title = online ? " \(working)" : " –"
        if waiting > 0 { title += " · ждут \(waiting)" }
        status.button?.title = title
        status.button?.image = NSImage(systemSymbolName: waiting > 0 ? "building.2.fill" : "building.2", accessibilityDescription: "ИИ-Офис")
        status.button?.contentTintColor = waiting > 0 ? .systemOrange : nil
    }
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        func label(_ text: String) { let i = NSMenuItem(title: text, action: nil, keyEquivalent: ""); i.isEnabled = false; menu.addItem(i) }
        if !online { label("Хаб не отвечает") }
        else { label("Работают \(count("working")) · ждут \(count("waiting")) · всего \(agents.count)") }
        let waiting = agents.filter { $0["approval"] is String }
        if !waiting.isEmpty {
            menu.addItem(.separator()); label("Ждут разрешения")
            for a in waiting {
                let holder = NSMenuItem(title: "\(a["name"] as? String ?? "?") — \(String((a["task"] as? String ?? "").prefix(60)))", action: nil, keyEquivalent: "")
                let sub = NSMenu()
                for (title, decision) in [("Одобрить", "allow"), ("Отклонить", "deny")] {
                    let i = NSMenuItem(title: title, action: #selector(decide(_:)), keyEquivalent: "")
                    i.target = self
                    i.representedObject = ["id": a["approval"] as? String ?? "", "decision": decision]
                    sub.addItem(i)
                }
                holder.submenu = sub
                menu.addItem(holder)
            }
        }
        let working = agents.filter { $0["status"] as? String == "working" }
        if !working.isEmpty {
            menu.addItem(.separator()); label("Работают")
            for a in working.prefix(12) {
                let i = NSMenuItem(title: "\(a["name"] as? String ?? "?") — \(String((a["task"] as? String ?? "").prefix(60)))", action: #selector(showOffice), keyEquivalent: "")
                i.target = self
                menu.addItem(i)
            }
        }
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Открыть офис", action: #selector(showOffice), keyEquivalent: "o"); open.target = self; menu.addItem(open)
        menu.addItem(NSMenuItem(title: "Выйти", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }
    @objc func decide(_ sender: NSMenuItem) {
        guard let body = sender.representedObject as? [String: String] else { return }
        var req = URLRequest(url: officeURL.appendingPathComponent("api/decide"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Office")          // the hub's CSRF guard
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in DispatchQueue.main.async { self?.poll() } }.resume()
    }

    @objc func reload() { load() }

    func buildMenu() {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem()
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            holder.submenu = menu
            main.addItem(holder)
        }
        func item(_ title: String, _ action: Selector, _ key: String, _ target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.target = target
            return i
        }
        let full = item("Во весь экран", #selector(NSWindow.toggleFullScreen(_:)), "f")
        full.keyEquivalentModifierMask = [.command, .control]
        submenu("ИИ-Офис", [
            item("О программе", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("Скрыть", #selector(NSApplication.hide(_:)), "h"),
            item("Выйти", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("Правка", [
            item("Отменить", Selector(("undo:")), "z"),
            item("Вырезать", #selector(NSText.cut(_:)), "x"),
            item("Копировать", #selector(NSText.copy(_:)), "c"),
            item("Вставить", #selector(NSText.paste(_:)), "v"),
            item("Выделить всё", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("Вид", [item("Обновить", #selector(reload), "r", self), full])
        submenu("Окно", [
            item("Свернуть", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Закрыть", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
