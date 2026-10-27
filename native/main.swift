import Cocoa
import WebKit
import UniformTypeIdentifiers
import Darwin

private let applicationName = "余料仓库"

// 这个窗口保留现有网页界面。数量计算和数据库写入仍由同一个本地服务处理，
// 原生程序只管理窗口、服务进程，以及 Mac 的文件打开/保存面板。
final class WarehouseApp: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, NSMenuItemValidation
{
    private var window: NSWindow!
    private var webView: WKWebView!
    private var loadingOverlay: NSView?
    private var helper: Process?
    private var helperLog: FileHandle?
    private var readyTimer: Timer?
    private var readyFile: URL?
    private var startupDeadline = Date()
    private var baseURL: URL?
    private var dataDirectory: URL!
    private var logURL: URL!
    private var instanceLock: Int32 = -1
    private var isTerminating = false
    private var hasReportedFailure = false
    private var isExporting = false
    private var exportTask: URLSessionDataTask?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        configureMenus()
        do {
            let override = try dataDirectoryOverride()
            if let override = override {
                dataDirectory = URL(fileURLWithPath: (override as NSString).expandingTildeInPath,
                                    isDirectory: true).standardizedFileURL
            } else {
                dataDirectory = try FileManager.default.url(
                    for: .applicationSupportDirectory, in: .userDomainMask,
                    appropriateFor: nil, create: true
                ).appendingPathComponent(applicationName, isDirectory: true)
            }
            try FileManager.default.createDirectory(
                at: dataDirectory, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            logURL = dataDirectory.appendingPathComponent("server.log")
            guard try acquireInstanceLock() else {
                // 同一个数据目录只开一个窗口。测试可以用另一个 --data-dir 独立启动。
                NSRunningApplication.runningApplications(withBundleIdentifier: "cn.upinking.leftoverwarehouse")
                    .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier })?
                    .activate(options: [.activateAllWindows])
                NSApp.terminate(nil)
                return
            }
            configureWindow()
            try startHelper(allowLegacyMigration: override == nil)
        } catch {
            reportFailure("无法启动仓库", error.localizedDescription)
        }
    }

    private func dataDirectoryOverride() throws -> String? {
        let arguments = CommandLine.arguments
        for (index, argument) in arguments.enumerated() {
            if argument == "--data-dir" {
                guard index + 1 < arguments.count, !arguments[index + 1].isEmpty,
                      !arguments[index + 1].hasPrefix("--") else {
                    throw startupError("--data-dir 后面需要一个有效的文件夹路径。")
                }
                return arguments[index + 1]
            }
            if argument.hasPrefix("--data-dir=") {
                let value = String(argument.dropFirst("--data-dir=".count))
                guard !value.isEmpty else { throw startupError("数据文件夹路径不能为空。") }
                return value
            }
        }
        return nil
    }

    private func startupError(_ message: String) -> NSError {
        NSError(domain: "cn.upinking.leftoverwarehouse", code: 1,
                userInfo: [NSLocalizedDescriptionKey: message])
    }

    private func acquireInstanceLock() throws -> Bool {
        let path = dataDirectory.appendingPathComponent(".application.lock").path
        instanceLock = Darwin.open(path, O_CREAT | O_RDWR | O_CLOEXEC, mode_t(0o600))
        guard instanceLock >= 0 else { throw startupError("无法锁定数据文件夹，请检查访问权限。") }
        if flock(instanceLock, LOCK_EX | LOCK_NB) == 0 { return true }
        let lockError = errno
        Darwin.close(instanceLock)
        instanceLock = -1
        if lockError == EWOULDBLOCK || lockError == EAGAIN { return false }
        throw startupError("数据文件夹锁定失败，请检查磁盘状态。")
    }

    private func configureWindow() {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 850)
        let size = NSSize(width: min(1220, visible.width - 60), height: min(820, visible.height - 70))
        window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = applicationName
        window.minSize = NSSize(width: 800, height: 550)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.setFrameAutosaveName("WarehouseMainWindow")
        if !window.setFrameUsingName("WarehouseMainWindow") { window.center() }

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.autoresizingMask = [.width, .height]

        let loadingView = NSView(frame: NSRect(origin: .zero, size: size))
        // 先将 WebView 放进窗口再加载，避免在不可见视图中完成导航后首次显示为空白。
        webView.frame = loadingView.bounds
        loadingView.addSubview(webView)
        let progress = NSProgressIndicator()
        progress.style = .spinning
        progress.controlSize = .regular
        progress.startAnimation(nil)
        let label = NSTextField(labelWithString: "正在打开仓库…")
        label.font = .systemFont(ofSize: 14)
        label.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [progress, label])
        stack.orientation = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        loadingView.addSubview(stack)
        loadingOverlay = stack
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: loadingView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: loadingView.centerYAnchor)
        ])
        window.contentView = loadingView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func configureMenus() {
        let main = NSMenu()
        let applicationMenu = NSMenu(title: applicationName)
        applicationMenu.addItem(menuItem("关于余料仓库", #selector(showAbout), ""))
        applicationMenu.addItem(.separator())
        let services = NSMenu(title: "服务")
        let servicesItem = NSMenuItem(title: "服务", action: nil, keyEquivalent: "")
        servicesItem.submenu = services
        applicationMenu.addItem(servicesItem)
        NSApp.servicesMenu = services
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(NSMenuItem(title: "隐藏余料仓库", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
        let hideOthers = NSMenuItem(title: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        applicationMenu.addItem(hideOthers)
        applicationMenu.addItem(NSMenuItem(title: "全部显示", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: ""))
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(NSMenuItem(title: "退出余料仓库", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        addMenu(applicationMenu, to: main)

        let file = NSMenu(title: "文件")
        file.addItem(menuItem("导出备份…", #selector(exportBackup), "s"))
        file.addItem(menuItem("打开数据文件夹", #selector(openDataDirectory), ""))
        file.addItem(.separator())
        file.addItem(NSMenuItem(title: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        addMenu(file, to: main)

        let edit = NSMenu(title: "编辑")
        edit.addItem(NSMenuItem(title: "撤销", action: Selector(("undo:")), keyEquivalent: "z"))
        let redo = NSMenuItem(title: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(redo)
        edit.addItem(.separator())
        edit.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        edit.addItem(NSMenuItem(title: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        edit.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        addMenu(edit, to: main)

        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(NSMenuItem(title: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        windowMenu.addItem(NSMenuItem(title: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: ""))
        windowMenu.addItem(.separator())
        windowMenu.addItem(menuItem("刷新", #selector(refresh), "r"))
        windowMenu.addItem(NSMenuItem(title: "全部置于顶层", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: ""))
        NSApp.windowsMenu = windowMenu
        addMenu(windowMenu, to: main)
        NSApp.mainMenu = main
    }

    private func menuItem(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    private func addMenu(_ submenu: NSMenu, to main: NSMenu) {
        let item = NSMenuItem(title: submenu.title, action: nil, keyEquivalent: "")
        item.submenu = submenu
        main.addItem(item)
    }

    private func startHelper(allowLegacyMigration: Bool) throws {
        guard let resources = Bundle.main.resourceURL else {
            throw startupError("应用资源不完整，请重新构建余料仓库.app。")
        }
        let executable = resources.appendingPathComponent("backend/warehouse-server")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw startupError("应用缺少内置仓库服务：\(executable.path)")
        }
        if !FileManager.default.fileExists(atPath: logURL.path) {
            guard FileManager.default.createFile(atPath: logURL.path, contents: nil,
                                               attributes: [.posixPermissions: 0o600]) else {
                throw startupError("无法创建服务日志，请检查数据文件夹权限。")
            }
        }
        helperLog = try FileHandle(forWritingTo: logURL)
        try helperLog?.seekToEnd()
        readyFile = FileManager.default.temporaryDirectory.appendingPathComponent("warehouse-ready-\(UUID().uuidString).json")

        let process = Process()
        process.executableURL = executable
        process.currentDirectoryURL = executable.deletingLastPathComponent()
        process.arguments = ["--data-dir", dataDirectory.path, "--ready-file", readyFile!.path,
                             "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        if allowLegacyMigration,
           let legacy = Bundle.main.object(forInfoDictionaryKey: "WarehouseLegacyDatabasePath") as? String,
           FileManager.default.isReadableFile(atPath: (legacy as NSString).expandingTildeInPath) {
            process.arguments! += ["--legacy-db", (legacy as NSString).expandingTildeInPath]
        }
        process.standardOutput = helperLog
        process.standardError = helperLog
        process.standardInput = FileHandle.nullDevice
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self, !self.isTerminating, !self.hasReportedFailure else { return }
                let detail = self.readReadyObject()?["message"] as? String
                self.reportFailure("仓库服务已停止", detail ?? "服务退出状态：\(process.terminationStatus)。请查看日志后重新打开应用。")
            }
        }
        helper = process
        try process.run()
        startupDeadline = Date().addingTimeInterval(15)
        readyTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            self?.pollReadyFile()
        }
    }

    private func readReadyObject() -> [String: Any]? {
        guard let file = readyFile, let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return object
    }

    private func pollReadyFile() {
        guard !isTerminating, !hasReportedFailure else { return }
        if let object = readReadyObject() {
            if object["status"] as? String == "error" {
                reportFailure("无法打开仓库", object["message"] as? String ?? "本地服务启动失败，请查看日志。")
                return
            }
            if object["status"] as? String == "ready", let port = object["port"] as? Int,
               (1...65535).contains(port), let url = URL(string: "http://127.0.0.1:\(port)") {
                readyTimer?.invalidate()
                readyTimer = nil
                baseURL = url
                removeReadyFile()
                webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
                return
            }
        }
        if Date() > startupDeadline {
            reportFailure("仓库启动超时", "15 秒内未能启动本地服务。请查看 server.log；重新打开应用不会清空已有数据。")
        }
    }

    private func isLocalURL(_ url: URL) -> Bool {
        guard let origin = baseURL else { return false }
        return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isLocalURL(url), url.path == "/api/backup" {
            // download 属性也走原生保存面板，取消导航以免把仓库页面换成空白页。
            decisionHandler(.cancel)
            exportBackup(nil)
            return
        }
        if isLocalURL(url) {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated || navigationAction.targetFrame == nil {
                openExternalURL(url)
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = navigationResponse.response.url, isLocalURL(url) else {
            decisionHandler(.cancel)
            return
        }
        if url.path == "/api/backup" {
            decisionHandler(.cancel)
            exportBackup(nil)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard baseURL != nil else { return }
        loadingOverlay?.removeFromSuperview()
        loadingOverlay = nil
        window.makeFirstResponder(webView)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if !isTerminating { webView.reloadFromOrigin() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled, !isTerminating else { return }
        reportFailure("无法加载仓库界面", error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled, !isTerminating else { return }
        reportFailure("仓库界面加载失败", error.localizedDescription)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocalURL(url) {
                if url.path == "/api/backup" { exportBackup(nil) }
                else { webView.load(navigationAction.request) }
            } else { openExternalURL(url) }
        }
        return nil
    }

    private func openExternalURL(_ url: URL) {
        guard ["https", "http", "mailto"].contains(url.scheme?.lowercased() ?? "") else { return }
        NSWorkspace.shared.open(url)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.title = "选择 JSON 文件"
        panel.prompt = "选择"
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.allowedContentTypes = [.json]
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    @objc private func exportBackup(_ sender: Any?) {
        guard let origin = baseURL, !isExporting, !isTerminating else { return }
        isExporting = true
        let panel = NSSavePanel()
        panel.title = "导出仓库备份"
        panel.prompt = "保存备份"
        panel.allowedContentTypes = [.json]
        panel.canCreateDirectories = true
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "yyyy-MM-dd-HHmmss"
        panel.nameFieldStringValue = "余料仓库备份-\(formatter.string(from: Date())).json"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }
            guard response == .OK, let destination = panel.url else { self.isExporting = false; return }
            let url = origin.appendingPathComponent("api/backup")
            self.exportTask = URLSession.shared.dataTask(with: URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)) { [weak self] data, response, error in
                var failure = error
                if failure == nil {
                    if let response = response as? HTTPURLResponse, response.statusCode == 200,
                       let data = data, data.count <= 64 * 1024 * 1024 {
                        do {
                            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                                  object["format"] as? String == "leftover-warehouse-backup" else {
                                throw NSError(domain: "WarehouseBackup", code: 1, userInfo: [NSLocalizedDescriptionKey: "服务返回的文件不是有效仓库备份。"])
                            }
                            try data.write(to: destination, options: .atomic)
                        } catch { failure = error }
                    } else {
                        failure = NSError(domain: "WarehouseBackup", code: 2, userInfo: [NSLocalizedDescriptionKey: "未能取得完整备份，请重试。"])
                    }
                }
                DispatchQueue.main.async {
                    guard let self = self, !self.isTerminating else { return }
                    self.isExporting = false
                    self.exportTask = nil
                    if let failure = failure { self.showOperationError("备份保存失败", failure.localizedDescription) }
                }
            }
            self.exportTask?.resume()
        }
    }

    @objc private func openDataDirectory(_ sender: Any?) {
        guard let directory = dataDirectory else { return }
        NSWorkspace.shared.open(directory)
    }

    @objc private func refresh(_ sender: Any?) {
        guard baseURL != nil else { return }
        webView.reloadFromOrigin()
    }

    @objc private func showAbout(_ sender: Any?) {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: applicationName,
            .applicationVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0.0",
            .version: "",
            .credits: NSAttributedString(string: "本地单用户电子元件库存管理")
        ])
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(exportBackup) { return baseURL != nil && !isExporting }
        if menuItem.action == #selector(refresh) { return baseURL != nil }
        if menuItem.action == #selector(openDataDirectory) { return dataDirectory != nil }
        return true
    }

    private func showOperationError(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        if let window = window { alert.beginSheetModal(for: window) }
        else { alert.runModal() }
    }

    private func reportFailure(_ title: String, _ message: String) {
        guard !isTerminating, !hasReportedFailure else { return }
        hasReportedFailure = true
        readyTimer?.invalidate()
        readyTimer = nil
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = title
        alert.informativeText = message + (logURL == nil ? "" : "\n\n日志位置：\(logURL!.path)")
        let canOpenLog = logURL != nil && FileManager.default.isReadableFile(atPath: logURL.path)
        if canOpenLog { alert.addButton(withTitle: "查看日志并退出") }
        alert.addButton(withTitle: "退出")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn, canOpenLog { NSWorkspace.shared.open(logURL) }
        NSApp.terminate(nil)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if let window = window { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func windowWillClose(_ notification: Notification) { NSApp.terminate(nil) }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        readyTimer?.invalidate()
        exportTask?.cancel()
        if let process = helper, process.isRunning {
            process.terminate()
            let deadline = Date().addingTimeInterval(2)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.03) }
            if process.isRunning { Darwin.kill(process.processIdentifier, SIGKILL) }
        }
        try? helperLog?.close()
        removeReadyFile()
        if instanceLock >= 0 { flock(instanceLock, LOCK_UN); Darwin.close(instanceLock); instanceLock = -1 }
    }

    private func removeReadyFile() {
        if let file = readyFile { try? FileManager.default.removeItem(at: file) }
        readyFile = nil
    }
}

let application = NSApplication.shared
let delegate = WarehouseApp()
application.delegate = delegate
application.run()
