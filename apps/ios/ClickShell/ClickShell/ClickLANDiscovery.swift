import Darwin
@preconcurrency import Foundation

@MainActor
final class ClickLANDiscovery:
    NSObject,
    @preconcurrency NetServiceBrowserDelegate,
    @preconcurrency NetServiceDelegate
{
    private static let serviceType = "_click-reader._tcp."
    private static let expectedContract = "click.reader_runtime.v1"

    private let browser = NetServiceBrowser()
    private var services: [NetService] = []
    private var continuation: CheckedContinuation<URL?, Never>?
    private var timeoutTask: Task<Void, Never>?
    private var verificationInFlight = Set<String>()
    private var finished = false
    private var deviceID = ""
    private var accessToken = ""

    func findVerifiedBaseURL(
        deviceID: String,
        accessToken: String
    ) async -> URL? {
        let token = accessToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !deviceID.isEmpty, !token.isEmpty else { return nil }
        self.deviceID = deviceID
        self.accessToken = token
        return await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                self.continuation = continuation
                browser.delegate = self
                browser.searchForServices(
                    ofType: Self.serviceType,
                    inDomain: "local."
                )
                timeoutTask = Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(8))
                    guard !Task.isCancelled else { return }
                    self?.finish(nil)
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.finish(nil)
            }
        }
    }

    func netServiceBrowser(
        _ browser: NetServiceBrowser,
        didFind service: NetService,
        moreComing: Bool
    ) {
        guard !finished else { return }
        services.append(service)
        service.delegate = self
        service.resolve(withTimeout: 3)
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        guard !finished,
              hasExpectedContract(sender),
              let address = sender.addresses?
                .compactMap(Self.numericPrivateIPv4)
                .first else {
            return
        }
        let key = "\(address):\(sender.port)"
        guard verificationInFlight.insert(key).inserted,
              let baseURL = URL(string: "http://\(address):\(sender.port)") else {
            return
        }
        Task { @MainActor [weak self] in
            guard let self else { return }
            let verified = await Self.verify(
                baseURL: baseURL,
                deviceID: deviceID,
                accessToken: accessToken
            )
            if verified {
                finish(baseURL)
            }
        }
    }

    func netService(
        _ sender: NetService,
        didNotResolve errorDict: [String: NSNumber]
    ) {
        sender.stop()
    }

    private func finish(_ result: URL?) {
        guard !finished else { return }
        finished = true
        timeoutTask?.cancel()
        timeoutTask = nil
        browser.stop()
        services.forEach { $0.stop() }
        services.removeAll()
        verificationInFlight.removeAll()
        let active = continuation
        continuation = nil
        active?.resume(returning: result)
    }

    private func hasExpectedContract(_ service: NetService) -> Bool {
        guard let data = service.txtRecordData() else { return false }
        let fields = NetService.dictionary(fromTXTRecord: data)
        let contract = fields["contract"].flatMap {
            String(data: $0, encoding: .utf8)
        }
        let transport = fields["transport"].flatMap {
            String(data: $0, encoding: .utf8)
        }
        return contract == Self.expectedContract && transport == "http"
    }

    private nonisolated static func numericPrivateIPv4(_ data: Data) -> String? {
        let address = data.withUnsafeBytes { rawBuffer -> String? in
            guard let pointer = rawBuffer.baseAddress?
                .assumingMemoryBound(to: sockaddr.self) else {
                return nil
            }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let result = getnameinfo(
                pointer,
                socklen_t(data.count),
                &host,
                socklen_t(host.count),
                nil,
                0,
                NI_NUMERICHOST
            )
            guard result == 0 else { return nil }
            return String(cString: host)
        }
        guard let address else { return nil }
        let values = address.split(separator: ".").compactMap { Int($0) }
        guard values.count == 4,
              values.allSatisfy({ (0...255).contains($0) }),
              values[0] == 10
                || (values[0] == 172 && (16...31).contains(values[1]))
                || (values[0] == 192 && values[1] == 168) else {
            return nil
        }
        return address
    }

    private nonisolated static func verify(
        baseURL: URL,
        deviceID: String,
        accessToken: String
    ) async -> Bool {
        guard let url = URL(
            string: "/v1/android/readium/health",
            relativeTo: baseURL
        )?.absoluteURL else {
            return false
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.5
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(deviceID, forHTTPHeaderField: "X-Click-Device-ID")
        request.setValue(accessToken, forHTTPHeaderField: "X-Click-Access-Token")
        request.setValue(
            "Bearer \(accessToken)",
            forHTTPHeaderField: "Authorization"
        )
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  data.count <= 65_536,
                  let json = try JSONSerialization.jsonObject(with: data)
                    as? [String: Any] else {
                return false
            }
            return json["ok"] as? Bool == true
                && json["schema"] as? String == "click.android.readium.health.v1"
                && json["reader_api"] as? String == "available"
        } catch {
            return false
        }
    }
}
