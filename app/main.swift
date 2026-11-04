// ИИ-Офис: native window around the office UI served by the local hub (hub/server.py).
import Cocoa
import WebKit

let officeURL = URL(string: ProcessInfo.processInfo.environment["AI_OFFICE_URL"] ?? "http://127.0.0.1:4747/")!

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var retry: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "ИИ-Офис"
        window.minSize = NSSize(width: 720, height: 480)
        window.contentView = web
        window.setFrameAutosaveName("AIOfficeMain")
        if !window.setFrameUsingName("AIOfficeMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        buildMenu()
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

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

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
