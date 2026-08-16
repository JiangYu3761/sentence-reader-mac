import SwiftUI

struct ConnectionView: View {
    @ObservedObject var store: ConnectionStore
    @State private var advanced = false

    var body: some View {
        Section("同步") {
            HStack {
                Label(
                    store.connectedBaseURL == nil ? "当前离线" : "已连接",
                    systemImage: store.connectedBaseURL == nil ? "wifi.slash" : "checkmark.circle.fill"
                )
                .foregroundStyle(store.connectedBaseURL == nil ? Color.secondary : Color.green)
                Spacer()
                if store.isChecking {
                    ProgressView()
                }
            }

            Text(
                store.connectedBaseURL == nil
                    ? "阅读和录音仍可离线使用；Mac 可达后再同步。"
                    : "已配对 · 当前同一 Wi-Fi 可同步"
            )
            .font(.footnote)
            .foregroundStyle(.secondary)

            Button(store.isChecking ? "正在检查…" : "检查连接") {
                Task { await store.connect() }
            }
            .disabled(store.isChecking || !store.hasSavedAddress)

            DisclosureGroup("高级连接设置", isExpanded: $advanced) {
                TextField("Mac 局域网地址", text: $store.hostInput)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()

                TextField("端口", text: $store.portInput)
                    .keyboardType(.numberPad)
                SecureField("设备令牌", text: $store.accessTokenInput)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()

                Text("设备 ID：\(store.deviceID)")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Button("保存并连接") {
                    Task { await store.connect() }
                }
                .disabled(store.isChecking)
            }

            if !store.statusMessage.isEmpty {
                Text(store.statusMessage)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
