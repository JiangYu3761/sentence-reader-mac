# Sentence Reader Interaction Contract

Updated: 2026-07-05

## Decision

Sentence Reader uses the rolled-back stable sentence-first interaction contract inside the reading surface, with one additive selected-text action bar.

This does not change macOS or iPadOS globally. It only decides which events the Sentence Reader reading surface claims before WebKit or the operating system default menu handles them. On the Mac native reader, secondary click/two-finger tap is the no-selection whole-sentence red-highlight path, double-click is the sentence note path, and single-click English word lookup remains separate. Only after deliberate正文 drag selection, Click shows a compact `复制 / 标红 / 备注` action bar. The action bar is the selected-text path; active selection + two-finger tap is not.

## Priority Rules

### 中文快捷命令说明

| 场景 | 操作 | 结果 |
| --- | --- | --- |
| Mac 正文句子 | 单击句子 | 聚焦句子；已有备注时显示备注预览 |
| Mac 英文单词 | 单击英文词 | 查词 |
| Mac 正文句子 | 快速双击句子 | 打开备注流程 |
| Mac 正文句子 | 慢慢点两次 | 两次单击，不等于双击 |
| Mac 正文句子 | 双指点按 | 整句标红 / 取消标红 |
| Mac 正文拖拽选区 | 自动出现小标签 | `复制 / 标红 / 备注` 只作用于选中文字 |
| Mac 英文单词 | `Option` + 双击 | 备用查词路径 |
| Mac 选中文字 | 小标签 `复制` | 复制选中文字；Click 不把 `Command+C` 接管为阅读命令 |
| Mac 阅读页 | `Command+Z` | 撤回上一步红标操作；支持整句红标和选中文字红标 |
| Mac 应用 | `Command+Q` | 退出 Sentence Reader |
| iPad 正文句子 | 点击句子 | 显示句子操作栏 |
| iPad 英文单词 | 点击英文词 | 查词 |
| iPad 正文句子 | 快速双击句子 | 打开备注流程 |
| iPad 正文句子 | 慢慢点两次 | 两次点击，不等于双击 |
| iPad 句子操作栏 | 点 `红标` | 整句标红 / 取消标红 |
| iPad 阅读页 | 左右滑动 | 翻页 |
| 桌面 Web / Windows 路线 | 聚焦句子后 `N` | 添加备注 |
| 桌面 Web / Windows 路线 | 聚焦句子后 `R` | 整句标红 / 取消标红 |
| 桌面 Web / Windows 路线 | 聚焦句子后 `V` | 添加语音备注 |
| 桌面 Web / Windows 路线 | `Esc` | 关闭弹窗、抽屉或取消聚焦 |

`双击` means two quick clicks/taps inside the system double-click interval. Two slow clicks/taps are intentionally treated as two separate single-click actions.

| Area | Gesture / Key | Owner | Result |
| --- | --- | --- | --- |
| Sentence text | Single click / tap | Sentence Reader | Focus the sentence and show note preview if it already has a note |
| English word in sentence text | Single click / tap | Sentence Reader | Look up the clicked word after a short delay, cancelled by double click / double tap |
| Sentence text | Double click / double tap | Sentence Reader | Open sentence note flow |
| Sentence text | Option + double click | Sentence Reader, then dictionary/vocab flow | Backup lookup path for pointer devices |
| Mac sentence text | Two-finger tap | Sentence Reader | Sentence Reader owns it and toggles red highlight for the whole sentence |
| Mac selected sentence text | Selection action bar `复制` | Sentence Reader + native pasteboard | Copy exact selected text |
| Mac selected sentence text | Selection action bar `标红` | Sentence Reader | Red-highlight exact selected text fragments |
| Mac selected sentence text | Selection action bar `备注` | Sentence Reader | Open the existing note panel for selected text |
| Mac selected sentence text | Two-finger tap / secondary click | Sentence Reader event guard | Do not red-highlight selected fragments and do not open the WebKit right-click menu; use the action bar instead |
| Mac reading surface | Command+Z | Sentence Reader | Undo the last red-highlight operation, including selected-fragment red highlights |
| iPad sentence action bar | Red button | Sentence Reader | Toggle whole-sentence red highlight |
| Active text selection in Mac reading surface | Command+C | System/WebKit copy path | Not a Click reading command |
| Mac app | Command+Q | System-style app command | Quit Sentence Reader |
| Active text selection outside sentence text | Context menu | System/WebKit | Show copy/search/share actions |
| Inputs, textareas, buttons, controls | Click, context menu, keyboard | System/WebKit | Preserve editing, copy, paste, focus, button activation |
| Page surface | Horizontal wheel/swipe or page keys | Sentence Reader | Turn page once with cooldown |
| Desktop Web / Windows route focused sentence | N / R / V | Sentence Reader | Note, red highlight, or voice note |
| Overlays/sheets | Esc | Sentence Reader first | Close sheet/toast/focus before falling through |

## Hard Rule

If there is no active正文 text selection, a two-finger tap on `.sr-sentence` toggles whole-sentence red highlight for the sentence under the pointer.

If the user deliberately creates an active正文 text selection by dragging, Sentence Reader shows the selected-text action bar. Generic selection events do not open the bar: double-click word selection, secondary-click/right-click selection, lookup side effects, keyboard selection, and WebKit `selectionchange` are not enough. `selectionchange` may only hide the bar when the selection collapses. `复制` copies exact selected text through a native pasteboard message, `标红` stores exact selected fragments with `range_locator.mode=text_selection`, and `备注` reuses the existing note panel with `range_locator.mode=text_selection_note`. If the selected fragments exactly match existing selected-text red fragments, the button label must change to `取消标红` and remove those exact fragments instead of adding another red highlight. Active selection + two-finger tap is not a selected-text path, and `Command+C` is not a Click-owned reading command.

Whole-sentence red highlight remains available for ordinary no-selection two-finger taps. Selected-fragment red highlight uses the existing `reader.annotations` table and existing `red_highlight` kind, with `range_locator.mode=text_selection`, so Reader API and PostgreSQL schema are unchanged. `Command+Z` is the undo path for red-highlight operations and must remove the matching selected-fragment `red_highlight` annotation instead of only changing the WebView paint.

No-selection two-finger tap on a sentence must be a closed event path: once Sentence Reader accepts it for whole-sentence red highlight, the following `contextmenu` / `auxclick` sequence must be swallowed and must not show the WebKit right-click menu. The hit test must use both DOM target and pointer coordinates, so tapping on line-height gaps or the edge of a sentence line still resolves to the nearby `.sr-sentence` instead of falling through to WebKit. After red highlight, Click clears WebKit's residual selection over several ticks so one or two selected characters do not remain on screen. If the selected-text action bar is visible, a two-finger tap on正文 is also swallowed so the user keeps the action bar path instead of getting a system menu.

Normal editor behavior inside text fields or note editors remains editor/system behavior, not a Click reading command.

Note ownership is independent from lookup and red highlight. A normal double-click on a sentence cancels pending lookup, clears any正文 selection/cache, and opens the sentence note editor for that sentence. The selected-text action bar `备注` reuses the same note panel UI and saves `range_locator.mode=text_selection_note`. `Option` + double-click is the explicit backup lookup path. Editing a note does not change red highlights, and toggling red does not delete the note.

Double-click note also suppresses the selected-text action bar for the short WebKit selection window caused by double-click word selection. The `复制 / 标红 / 备注` action bar is for deliberate正文 drag selection, not for the transient selection WebKit creates during double-click.

For runtime debugging, the Mac reader writes non-content interaction traces to `~/Library/Application Support/SentenceReader/Logs/native_input.log`. The log records route names, indexes, hit/candidate booleans, and save success/failure only; it must not record selected正文 content or full sentence text.

English lookup is intentionally attached to the clicked word, not the whole sentence. If the click is actually the first click of a double-click, the pending lookup is cancelled and the double-click note flow wins.

## Selected-Text Action Bar

Selected-text `复制 / 标红 / 备注` is implemented as a separate action bar after deliberate正文 drag selection. It must not touch the no-selection single-click lookup, double-click note, or two-finger whole-sentence red paths.

## Why This Is Reasonable

Sentence Reader exists because Apple Books and default WebKit reading do not provide the desired whole-sentence annotation workflow. Therefore, sentence-level gestures must win on sentence text.

At the same time, Sentence Reader should not fight the operating system in editing or control areas. Text fields, buttons, settings, file inputs, note editors, and non-sentence selection zones keep normal system behavior.

## Implementation Points

- Mac native reader: `Probe/NativeSentenceReader/SentenceReaderNative.swift`
- iPad/LAN reader: `reader_api/app.py`
- Contract marker: `sentence-reader-interaction-v1`
- English lookup marker: `english-click-lookup` / `english-tap-lookup`
- Mac native input marker: `shouldLetSystemHandleContext` / `toggleRedFromSecondaryEvent` / `sr-selection-action-bar` / `selection-action-bar-copy-button-not-command-c`
- Static guard: `scripts/sentence_reader_interaction_contract_smoke.py`

## Non-Goals

- Do not change global macOS or iPadOS gestures.
- Do not make the system context menu the primary sentence annotation path.
- Do not use long press as the Mac primary red-highlight gesture.
- Do not route Mac reading through `/lan/reader`.
- Do not replace Click's reader with an external reader engine; open-source projects are reference points, not replacement readers.
