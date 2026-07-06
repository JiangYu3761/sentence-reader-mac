# Click Mobile App Shell Construction Plan

更新日期：2026-07-01

## 施工判断

当前要做的是正确的事：把现有 iPad / Android 浏览器访问升级成沉浸式 App 入口，而不是重写移动端阅读器。

这件事直接服务于 Click 的核心目标：

- Mac 继续作为数据中心和 Reader API 服务端。
- iPad / Android 只做更好的本地入口工作台。
- 书库、正文、笔记、红标、查词、语音入口继续复用现有 `/library` 和 `/lan/reader`。
- 录音作为独立 Recordings 总仓库资产保存在 Mac 本地。
- Hermes 作为处理器和对话入口，不拥有录音资产。

## 本轮主任务

当前移动工作台路线已从单一 P1 壳推进到本地可验收的 P1.1-P6 闭环：

```text
Click Mobile Workspace P1.1-P6
```

Android 第一轮原本只预留目录和施工标准；在 iPad 构建被 Xcode iOS platform/runtime 环境阻塞后，Android P1 已切换为当前移动壳主线，并以本地 debug APK 构建为验收目标。

Mobile Workspace 已从“只读书壳”升级为三入口工作台。移动端第一屏是：

```text
阅读
录音
Hermes
```

阅读继续走现有 `/library` 和 `/lan/reader`。录音走 `/recordings` 并保存到独立 `~/Documents/Recordings` 总仓库。Hermes 走 `/hermes`，通过 Mac 本地同源入口转发到 Hermes Runtime。具体边界见 `docs/hermes_mobile_gateway_construction.md`。

## 旧设备 Lite 兜底

2026-07-04 增加了 Click Mobile/Web 老设备 Lite 自动分流。它的目标是让少数旧机器或功能机可以打开核心阅读功能，而不是降低现代 iPhone、iPad、Android WebView 的体验。

分流规则保持保守：

- 默认进入现代 `/home`、`/library`、`/lan/reader`。
- `?ui=modern` 强制现代版，并写入 `click_ui=modern` cookie。
- `?ui=lite` 强制 Lite 版，并写入 `click_ui=lite` cookie。
- 只有 KaiOS、Opera Mini、老 UC、Android 4.x 及以下、旧 iOS Safari、Windows Phone 等高置信旧设备 UA 才自动进入 Lite。
- 现代页最前置 ES5 能力检测缺少 `fetch` / `Promise` / `querySelector` / `addEventListener` 时才跳到 Lite。
- 不用宽泛的 `Mobile` / `Safari` / `Chrome` 规则，避免新机器误跳。

Lite 路由：

```text
/home-lite
/library-lite
/reader-lite
/reader-lite/toc
POST /reader-lite/red
POST /reader-lite/note
POST /reader-lite/audio-note
```

Lite 首页仍然只有三个入口：

```text
阅读 -> /library-lite
录音 -> /recordings
Hermes -> /hermes
```

Lite 阅读器只换旧设备前端呈现，不改 Reader API schema，不改 PostgreSQL 表结构。它按 EPUB/Readium 的 `spine` / `toc` / `readingOrder` 思路分层：Lite Manifest 只生成轻量阅读清单，Lite TOC 只显示目录，不逐章打开正文；Lite 正文页只在打开指定章节时提取正文，并跳过 EPUB 页内的 `上一篇 / 回目录 / 下一篇` 导航块。用户从目录点第几篇，就进入第几篇，不再因为正文里出现“回目录”而跳回目录或简介。

默认 Lite 阅读页只显示正文：不显示书库入口、目录入口、书名、章节名、页码说明或“已跳过”提示。正文采用屏幕分页：服务端提供整章句子和稳定 sentence id，前端用 ES5 JS 根据当前屏幕高度、正文宽度、字体和行距逐句测量，屏幕能容纳多少句就显示多少句，并为旧浏览器底部导航栏预留安全区，避免最后一行被遮挡。左右贴边热区和左右滑动用于上一页/下一页；到本章边界时再进入上一章/下一章。目录仍保留在独立 `/reader-lite/toc` 页面，从目录点第几篇就进入第几篇。默认页面不提前铺开标红、备注、语音备注表单；用户点选某一句后，才在该句附近显示一行式标红、文字备注和语音备注入口。标红/取消标红是瞬时动作，完成后退出选中态并回到阅读；文字备注和语音备注保留选中态，便于继续编辑或上传。标红和文字备注复用 `reader.annotations` 的现有 annotation/note 链路；语音备注使用 `<input type="file" accept="audio/*" capture>` 上传音频，保存到现有 `reader.audio_notes` 链路，并复用 Mac 端 voice pipeline 后台转写。Lite 不替代现代 `/library` 和 `/lan/reader`，只作为旧设备兜底。

Lite 书库不再用一个含糊的“打开”按钮承担所有含义。每本可读书显示两个轻量入口：`继续读` 和 `目录`。`继续读` 优先读取现有 `reader.reading_positions`，把保存的 `chapter_locator` 映射回 Lite reading order 的章节，并使用保存的 `page_index` 回到屏幕分页页码；没有保存进度时才自动打开第一段正文。Lite 翻页时通过 `/reader-lite/position` 轻量写回 `page_index`、`total_pages` 和当前页首句，不新建表，不改 PostgreSQL schema。

## P1.1-P6 验收边界

| 阶段 | 当前边界 |
| --- | --- |
| P1.1 录音总仓库 | 新录音 canonical path 是 `~/Documents/Recordings`，不按年月做主目录；旧 Click Knowledge Inbox 录音路径只做 legacy read-only |
| P2 设备审核 | Android / iPad 壳生成稳定 `device_id`；Click Local Hub 提供 request / pending / approve / revoke / status；token 只保存在本地配置 |
| P3 录音管理 | `/recordings` 支持列表、音频、metadata、标题/分类/标签编辑、隐藏、reprocess dry-run |
| P4 Hermes 语音消息 | `/hermes` 支持文字和临时语音消息；语音消息进入 VoiceInbox，不进入长期录音资产 |
| P5 edge-tts | 本地 edge-tts 可用时生成语音回复，默认 voice 是 `zh-CN-YunjianNeural`；不可用时仍返回文字 |
| P6 Android APK | 构建 debug APK 和 sha256；这是手机测试产物，不是签名发布版 |

不做的事保持不变：不重写阅读器、不新增移动端数据库、不写入 Hermes memory / session / repo、不写入 Marketplace OS、不做公网账号系统、不做实时电话和打断机制。

## 任务分层

| 层级 | 任务 | 本轮是否做 |
| --- | --- | --- |
| 战略任务 | 确认移动端只做 App 壳，不做第二套阅读器 | 已确认 |
| 验证任务 | iPad 真机/模拟器能全屏打开 Mac `/library` | 已有本地代码，受 Xcode 环境阻塞 |
| 基础设施任务 | 新增 iOS shell 工程、静态 smoke、构建脚本 | 本轮必须做 |
| 基础设施任务 | 新增 Android shell 工程、静态 smoke | 已追加 |
| 优化任务 | 自动发现 Mac、二维码、精细沉浸交互 | 后续 |
| 噪音任务 | 移动端本地数据库、移动端导入 EPUB、云同步 | 禁止 |

## 目标形态

```text
Click Mobile App
-> SwiftUI shell
or Android Activity shell
-> WKWebView / WebView
-> http://<mac-lan-ip>:18180/home
-> 阅读 / 录音 / Hermes
```

用户看到的是 Click App，不是 Safari。

## 建议目录

```text
apps/
  ios/
    ClickShell/
      ClickShell.xcodeproj
      ClickShell/
        ClickShellApp.swift
        ContentView.swift
        ConnectionStore.swift
        ConnectionView.swift
        ReaderWebView.swift
        ShellToolbar.swift
        Assets.xcassets/
        Info.plist
      README.md
  android/
    ClickShell/
      README.md
scripts/
  mobile_shell_static_smoke.py
  build_ios_click_shell.sh
docs/
  mobile_app_shell_plan.md
  mobile_app_shell_construction.md
  hermes_mobile_gateway_construction.md
```

Android 已成为当前手机测试主线；iPad 壳保留并继续对齐设备 ID/token、三入口和全屏 WebView 结构，但仍不写成已签名发布。

## iPad P1 功能清单

必须实现：

1. App 名称显示为 `Click`。
2. 首次打开进入连接页。
3. 输入 Mac IP 或完整 URL。
4. 端口默认 `18180`。
5. 连接前检查 `/health`。
6. 连接成功后打开 `/home`。
7. 点阅读进入 `/library`，点书后继续使用现有 `/lan/reader`。
8. 点录音进入 `/recordings`。
9. 点 Hermes 进入 `/hermes`。
10. 保存上次连接地址。
11. 重新打开 App 自动尝试上次地址。
12. 连接失败时显示人话提示。
13. 可以回到首页、阅读、录音、Hermes。
14. 可以刷新当前页。
15. 可以重新设置 Mac 地址。
16. WebView 全屏，不显示 Safari / Chrome 地址栏。
17. 不破坏现有 iPad 浏览器访问方式。

## iPad P1 UI 原则

移动端壳的 UI 要克制，不能再给阅读页增加一堆按钮。

默认状态：

- 内容全屏。
- 不常驻大工具栏。
- 不遮挡 `/lan/reader` 现有顶部/底部交互。

Shell 控制入口：

- 连接页：完整显示地址输入和连接按钮。
- 阅读/书库页：只保留一个轻量入口，用于打开 shell 菜单。
- Shell 菜单里放：首页、阅读、录音、Hermes、刷新、更换 Mac 地址、连接状态。

如果实现成本需要压缩，第一版可以使用底部小菜单，但必须满足：

- 不盖住正文最后一行。
- 不盖住 `/lan/reader` 的句子操作条。
- 不把“返回书库 / 刷新 / 设置”做成高存在感常驻按钮。

## iPad P1 技术设计

### Swift 文件职责

| 文件 | 职责 |
| --- | --- |
| `ClickShellApp.swift` | App 入口 |
| `ContentView.swift` | 根据连接状态切换连接页和 WebView |
| `ConnectionStore.swift` | 保存 Mac 地址、端口、最近成功 URL |
| `ConnectionView.swift` | 输入地址、连接、错误提示 |
| `ReaderWebView.swift` | WKWebView 包装、导航状态、权限配置 |
| `ShellToolbar.swift` | 轻量返回书库、刷新、设置入口 |

### 连接规则

用户可以输入：

```text
<mac-lan-ip>
<mac-lan-ip>:18180
http://<mac-lan-ip>:18180
```

内部统一成：

```text
http://<host>:18180
```

默认打开：

```text
http://<host>:18180/home
```

健康检查：

```text
GET http://<host>:18180/health
```

### Info.plist 要求

iPad 壳需要允许局域网 HTTP 和本地网络访问：

```text
NSLocalNetworkUsageDescription
NSAppTransportSecurity
NSAllowsArbitraryLoadsInWebContent
```

如果要验证语音备注，还需要：

```text
NSMicrophoneUsageDescription
```

第一版可以先声明麦克风权限，但不改 `/lan/reader` 语音逻辑。

### WKWebView 配置

必须配置：

- JavaScript 可用。
- `allowsBackForwardNavigationGestures = true`。
- inline media 可用。
- 加载失败要回到 shell 错误状态，而不是白屏。
- 不拦截现有 `/library` 和 `/lan/reader` 逻辑。

暂不做：

- 注入自定义 JS 改阅读器交互。
- 重写查词卡片。
- 重写标红/笔记/语音逻辑。

## iPad P1 构建与安装

本轮以构建通过为硬验收：

```bash
xcodebuild \
  -project apps/ios/ClickShell/ClickShell.xcodeproj \
  -scheme ClickShell \
  -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO \
  build
```

真机安装需要用户设备和 Apple 签名，不作为本轮自动化硬门槛。

真机安装路线：

1. Xcode 打开 `apps/ios/ClickShell/ClickShell.xcodeproj`。
2. 选择用户的 iPad。
3. 配置 Team。
4. Run 到设备。

后续分发路线：

- 自用：Xcode 直装。
- 少量朋友：Ad Hoc。
- 更多测试用户：TestFlight。

## Android P1

iPad 构建被本机 Xcode iOS platform/runtime 阻塞后，Android P1 已落地为本地工程 scaffold，并可使用用户目录内的 Java、Gradle、Android SDK 产出本地 debug APK。这个 APK 只用于手机测试，不等同于签名发布版。

目录：

```text
apps/android/ClickShell/README.md
apps/android/ClickShell/settings.gradle
apps/android/ClickShell/build.gradle
apps/android/ClickShell/app/build.gradle
apps/android/ClickShell/app/src/main/AndroidManifest.xml
apps/android/ClickShell/app/src/main/java/com/click/shell/MainActivity.java
```

Android P1 使用：

```text
native Android Activity + WebView
```

必须保留与 iPad 相同的产品边界：

- 不做 Android 本地数据库。
- 不导入 EPUB 到 Android。
- 不重写阅读器。
- 默认打开 Mac `/home`。
- 点书进入 `/lan/reader`。
- 首页提供阅读 / 录音 / Hermes 三入口。
- 只生成本地 debug APK；不宣称正式发布 APK 已完成。

## Smoke 设计

新增：

```text
scripts/mobile_shell_static_smoke.py
```

验收内容：

1. `docs/mobile_app_shell_construction.md` 存在。
2. `apps/ios/ClickShell` 存在。
3. iOS 工程包含 `WKWebView`。
4. iOS 工程包含 `/library`。
5. iOS 工程包含 `/health`。
6. iOS Info.plist 包含局域网/HTTP 权限说明。
7. iOS UI 文案包含 `Click`。
8. Android 目录存在，且如果本地 scaffold 已实现，必须明确 debug APK 只是本地构建目标。
9. 不得宣称 Android 已经有签名发布 APK。
10. 不得宣称 iPad 已经进入 App Store 正式分发。
11. 不出现真实局域网 IP。

## 验收命令

iPad P1 完成后必须运行：

```bash
python3 scripts/mobile_shell_static_smoke.py
python3 scripts/android_shell_static_smoke.py
xcodebuild -project apps/ios/ClickShell/ClickShell.xcodeproj -scheme ClickShell -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
python3 scripts/public_repo_privacy_smoke.py
python3 scripts/public_readme_platform_smoke.py
python3 -m compileall scripts -q
```

如果 Mac Reader API 正在运行，还要验证：

```bash
curl -fsS http://127.0.0.1:18180/health
curl -fsS http://127.0.0.1:18180/library >/tmp/click-library.html
```

真机验收单独记录，不假装自动化已经覆盖。

## 不该做的事

第一轮禁止：

- 不做 Android 签名发布 APK。
- 不做 iPad 本地 EPUB 导入。
- 不做 iPad 本地 PostgreSQL。
- 不做移动端离线阅读。
- 不做 Bonjour 自动发现。
- 不做二维码配对。
- 不改 `/lan/reader` 核心阅读逻辑。
- 不改 Reader API schema。
- 不推 GitHub，除非用户明确要求。

## 完成定义

iPad P1 可以说“完成”必须同时满足：

1. 本地代码存在于 `apps/ios/ClickShell`。
2. 静态 smoke 通过。
3. `xcodebuild` generic iOS build 通过。
4. App 默认连接 `/library`，不是 `/lan/reader`。
5. App 壳里没有浏览器地址栏。
6. 连接失败不是白屏。
7. 文档写清楚真机安装限制。
8. 未破坏现有 Mac / iPad browser / Reader API 验收。

## 2026-06-30 本机验收状态

本地 iPad P1 壳代码、静态 smoke、Swift 源码类型检查、`/health`、`/library` live check 均已通过。用户指定的 `xcodebuild -destination 'generic/platform=iOS'` 仍未通过，当前阻塞不是 ClickShell 源码，而是本机 Xcode destination 环境：`xcodebuild -showdestinations` 只返回不可用的 `Any iOS Device`，并提示 `iOS 26.5 is not installed`; `xcrun simctl list runtimes` 和 `xcrun simctl list devices available` 均为空。

Android P1 scaffold 已在同日追加：本地 Android Activity + WebView 工程存在，静态 smoke 可验证连接页、`/health`、`/library`、`/lan/reader`、cleartext LAN HTTP、麦克风权限声明、轻量菜单和“只做本地 debug APK，不做签名发布 APK”的边界。用户目录内的 Java、Gradle、Android SDK 可用于生成本地 debug APK；真机安装和同局域网阅读仍需 Android 手机测试。

因此，本轮不能把 iPad 壳写成“已真机可安装完成”。下一步不是继续改阅读器或 WebView，而是在 Xcode Settings > Components 安装可用 iOS platform/runtime，或连接真实 iPad 并配置 Apple signing team 后重新运行：

```bash
xcodebuild -project apps/ios/ClickShell/ClickShell.xcodeproj -scheme ClickShell -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
```

## 下一轮施工提示词

```text
继续推进 Click iPad Mobile App Shell P1。如果已经完成并通过验收，就不要重复施工，只报告完成状态；如果没完成，就直接实施到可验收版本。本轮不要做 Android APK，不要重写阅读器，不要改 Reader API schema，不要推 GitHub。

项目路径：
<project-root>

按 docs/mobile_app_shell_construction.md 执行：
1. 新增 apps/ios/ClickShell iPad SwiftUI + WKWebView 壳。
2. 首次打开显示连接页，输入 Mac IP 和端口，端口默认 18180。
3. 连接前检查 /health。
4. 连接成功后进入 /library。
5. 点击书后继续使用现有 /lan/reader。
6. 保存上次连接地址，重开自动尝试。
7. 提供轻量入口：回到书库、刷新、更换 Mac 地址、连接状态。
8. 默认全屏沉浸，不显示浏览器地址栏，不做高存在感常驻工具栏。
9. Info.plist 配好局域网 HTTP、Local Network、麦克风权限说明。
10. 新增 apps/android/ClickShell/README.md，只写 Android 后续边界，不生成 APK。
11. 新增 scripts/mobile_shell_static_smoke.py。
12. 更新必要文档，但不要把移动端写成已完成。

必须运行：
- python3 scripts/mobile_shell_static_smoke.py
- xcodebuild -project apps/ios/ClickShell/ClickShell.xcodeproj -scheme ClickShell -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
- python3 scripts/public_repo_privacy_smoke.py
- python3 scripts/public_readme_platform_smoke.py
- python3 -m compileall scripts -q
- 能跑的话 curl http://127.0.0.1:18180/health 和 /library

最后报告：
1. 完成了什么
2. 改了哪些文件
3. iPad 壳是否能构建
4. 是否进入 /library
5. 是否没有浏览器地址栏
6. Android 是否只是预留、未误写成完成
7. 哪些测试通过
8. 没完成的真机安装限制
9. 本轮是否没有推 GitHub
```
