import Foundation

@MainActor
final class ConnectionStore: ObservableObject {
    @Published var hostInput: String
    @Published var portInput: String
    @Published var accessTokenInput: String
    @Published private(set) var connectedBaseURL: URL?
    @Published var statusMessage: String = ""
    @Published var isChecking = false

    private let defaults: UserDefaults
    private let hostKey = "ClickShell.hostInput.v1"
    private let portKey = "ClickShell.portInput.v1"
    private let accessTokenKey = "mobile-access-token"
    private let deviceIDKey = "ClickShell.deviceID.v1"
    private let baseURLKey = "ClickShell.connectedBaseURL.v1"
    private let remoteTTSBaseURL: URL?
    private let homePath = "/home"
    private let libraryPath = "/library"
    private let recordingsPath = "/recordings"
    private let hermesPath = "/hermes"
    private let healthPath = "/health"
    private let protectedHealthPath = "/v1/android/readium/health"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        remoteTTSBaseURL = Self.validatedRemoteTTSOrigin(
            Bundle.main.object(forInfoDictionaryKey: "ClickRemoteTTSOrigin") as? String
        )
        hostInput = defaults.string(forKey: hostKey) ?? ""
        portInput = defaults.string(forKey: portKey) ?? "18180"
        accessTokenInput = KeychainStore.string(for: accessTokenKey) ?? ""
        if defaults.string(forKey: deviceIDKey) == nil {
            defaults.set("ios-\(UUID().uuidString)", forKey: deviceIDKey)
        }
        applyTrustedUSBBootstrapIfNeeded()
    }

    var hasSavedAddress: Bool {
        !hostInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var hasAccessToken: Bool {
        !accessTokenInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    func clickTTSConfiguration(
        bookID: String,
        locatorJSON: String = ""
    ) -> ClickTTSConfiguration? {
        guard hasAccessToken,
              let baseURL = remoteTTSBaseURL ?? connectedBaseURL else {
            return nil
        }
        return ClickTTSConfiguration(
            baseURL: baseURL,
            deviceID: deviceID,
            accessToken: accessTokenInput.trimmingCharacters(in: .whitespacesAndNewlines),
            bookID: bookID,
            locatorJSON: locatorJSON
        )
    }

    func homeURL(for baseURL: URL) -> URL {
        urlWithAccess(path: homePath, baseURL: baseURL)
    }

    func libraryURL(for baseURL: URL) -> URL {
        urlWithAccess(path: libraryPath, baseURL: baseURL)
    }

    func recordingsURL(for baseURL: URL) -> URL {
        urlWithAccess(path: recordingsPath, baseURL: baseURL)
    }

    func hermesURL(for baseURL: URL) -> URL {
        urlWithAccess(path: hermesPath, baseURL: baseURL)
    }

    func healthURL(for baseURL: URL) -> URL {
        URL(string: healthPath, relativeTo: baseURL)?.absoluteURL ?? baseURL.appendingPathComponent("health")
    }

    func resetConnection() {
        connectedBaseURL = nil
        statusMessage = ""
    }

    func connect() async {
        guard let baseURL = normalizedBaseURL() else {
            statusMessage = "请输入 Mac 的局域网地址。"
            return
        }

        await connect(to: baseURL)
    }

    func restoreConnectionOrDiscover() async {
        guard !isChecking else { return }
        if let savedURL = normalizedBaseURL() {
            await connect(to: savedURL, showFailure: false)
            if connectedBaseURL != nil {
                return
            }
        }
        guard hasAccessToken else { return }
        isChecking = true
        statusMessage = "正在寻找已配对的 Mac…"
        defer { isChecking = false }
        let discovery = ClickLANDiscovery()
        guard let baseURL = await discovery.findVerifiedBaseURL(
            deviceID: deviceID,
            accessToken: accessTokenInput
        ) else {
            statusMessage = "当前离线；阅读、朗读和录音仍可使用。"
            return
        }
        connectedBaseURL = baseURL
        persist(baseURL: baseURL)
        statusMessage = "已自动连接"
    }

    func authorizedRequest(
        path: String,
        baseURL: URL,
        method: String = "GET"
    ) -> URLRequest {
        let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
            ?? baseURL.appendingPathComponent(
                path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            )
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(deviceID, forHTTPHeaderField: "X-Click-Device-ID")
        let token = accessTokenInput.trimmingCharacters(in: .whitespacesAndNewlines)
        if !token.isEmpty {
            request.setValue(token, forHTTPHeaderField: "X-Click-Access-Token")
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func connect(to baseURL: URL, showFailure: Bool = true) async {
        isChecking = true
        statusMessage = "正在连接 Click 服务..."
        defer { isChecking = false }

        let path = hasAccessToken ? protectedHealthPath : healthPath
        var request = authorizedRequest(path: path, baseURL: baseURL)
        request.timeoutInterval = 5

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                connectedBaseURL = nil
                if showFailure {
                    statusMessage = "没有连上 Click；本地阅读仍可使用。"
                }
                return
            }
            if hasAccessToken {
                guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      json["ok"] as? Bool == true,
                      json["schema"] as? String == "click.android.readium.health.v1" else {
                    connectedBaseURL = nil
                    if showFailure {
                        statusMessage = "发现的服务没有通过已配对身份验证。"
                    }
                    return
                }
            }
            guard baseURL.isPrivateIPv4Origin || !hasAccessToken else {
                connectedBaseURL = nil
                return
            }
            connectedBaseURL = baseURL
            persist(baseURL: baseURL)
            statusMessage = "已连接"
        } catch {
            connectedBaseURL = nil
            if showFailure {
                statusMessage = "连接失败；本地阅读仍可使用。"
            }
        }
    }

    private func persist(baseURL: URL) {
        hostInput = baseURL.host ?? hostInput.trimmingCharacters(in: .whitespacesAndNewlines)
        portInput = String(baseURL.port ?? 18180)
        defaults.set(hostInput, forKey: hostKey)
        defaults.set(portInput, forKey: portKey)
        defaults.set(baseURL.absoluteString, forKey: baseURLKey)
        let token = accessTokenInput.trimmingCharacters(in: .whitespacesAndNewlines)
        if token.isEmpty {
            KeychainStore.remove(accessTokenKey)
        } else {
            try? KeychainStore.set(token, for: accessTokenKey)
        }
    }

    /// Xcode/devicectl can provision a freshly installed development build over
    /// the already trusted USB debugging channel. The values exist only in this
    /// launch environment; the credential is immediately moved into Keychain.
    /// Release builds never accept this bootstrap path.
    private func applyTrustedUSBBootstrapIfNeeded() {
#if DEBUG
        guard accessTokenInput
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .isEmpty else {
            return
        }
        let environment = ProcessInfo.processInfo.environment
        let credential = (
            environment["CLICK_IPAD_USB_CREDENTIAL"] ?? ""
        ).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !credential.isEmpty,
              let rawOrigin = environment["CLICK_IPAD_USB_ORIGIN"],
              let origin = URL(string: rawOrigin),
              origin.scheme?.lowercased() == "http",
              origin.user == nil,
              origin.password == nil,
              origin.query == nil,
              origin.fragment == nil,
              origin.path.isEmpty || origin.path == "/",
              origin.isPrivateIPv4Origin else {
            return
        }
        accessTokenInput = credential
        try? KeychainStore.set(credential, for: accessTokenKey)
        hostInput = origin.host ?? ""
        portInput = String(origin.port ?? 18180)
        defaults.set(hostInput, forKey: hostKey)
        defaults.set(portInput, forKey: portKey)
        defaults.set(origin.absoluteString, forKey: baseURLKey)
#endif
    }

    var deviceID: String {
        if let existing = defaults.string(forKey: deviceIDKey), !existing.isEmpty {
            return existing
        }
        let created = "ios-\(UUID().uuidString)"
        defaults.set(created, forKey: deviceIDKey)
        return created
    }

    private func urlWithAccess(path: String, baseURL: URL) -> URL {
        let raw = URL(string: path, relativeTo: baseURL)?.absoluteURL ?? baseURL.appendingPathComponent(path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
        guard var components = URLComponents(url: raw, resolvingAgainstBaseURL: false) else {
            return raw
        }
        var query = components.queryItems ?? []
        query.append(URLQueryItem(name: "device_id", value: deviceID))
        query.append(URLQueryItem(name: "device_name", value: "iPad Click"))
        components.queryItems = query
        return components.url ?? raw
    }

    private func normalizedBaseURL() -> URL? {
        var raw = hostInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else {
            return nil
        }
        if !raw.contains("://") {
            raw = "http://\(raw)"
        }
        guard var components = URLComponents(string: raw) else {
            return nil
        }
        components.scheme = "http"
        components.path = ""
        components.query = nil
        components.fragment = nil
        if components.port == nil {
            let port = Int(portInput.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 18180
            components.port = port
        }
        return components.url
    }

    private static func validatedRemoteTTSOrigin(_ rawValue: String?) -> URL? {
        guard
            let rawValue,
            !rawValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            var components = URLComponents(
                string: rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
            ),
            components.scheme?.lowercased() == "https",
            components.host?.isEmpty == false,
            components.user == nil,
            components.password == nil,
            components.query == nil,
            components.fragment == nil,
            components.path.isEmpty || components.path == "/"
        else {
            return nil
        }
        components.scheme = "https"
        components.path = ""
        return components.url
    }
}

private extension URL {
    var isPrivateIPv4Origin: Bool {
        guard let host,
              !host.isEmpty,
              host.range(of: #"^\d{1,3}(\.\d{1,3}){3}$"#, options: .regularExpression) != nil else {
            return false
        }
        let values = host.split(separator: ".").compactMap { Int($0) }
        guard values.count == 4, values.allSatisfy({ (0...255).contains($0) }) else {
            return false
        }
        return values[0] == 10
            || (values[0] == 172 && (16...31).contains(values[1]))
            || (values[0] == 192 && values[1] == 168)
    }
}
