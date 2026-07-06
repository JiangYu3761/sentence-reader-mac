# Sentence Reader Current Status

Updated: 2026-07-05

## Current Stage

V2.1 is now the daily-use product-grade boundary for this Mac: PostgreSQL data base, Swift data integration, notes loop, multi-book loop, export loop, voice-note persistence, reading stability, Hermes sync, packaged runtime, Hermes ingestion, reviewable Cognitive OS draft generation, explicit draft-to-formal-intake promotion, operator review queue generation, transaction-style active-pack rebuild operation, a safe Reader API/Swift surface, runtime/bootstrap/preflight paths, recognizable Dock-pinned app access, and trusted same-LAN iPad browser reading are all implemented and covered by acceptance checks.

日常可用产品级 in this project means local EPUB reading, source-independent EPUB imports, sentence-level annotations, notes, voice-note persistence, reading position restore, Reader API/PostgreSQL durability, packaged Mac app access, selectable Mac voice transcription with Click's software-layer local path/FunASR as the default and Apple Speech as the backup provider, and same-Wi-Fi iPad browser reading at the current Mac LAN URL, for example `http://<mac-lan-ip>:18180/lan/reader`.

Current platform boundary: macOS App is the primary usable client, and iPad/browser same-LAN access is currently usable through `/library` and `/lan/reader`. Windows is a planned route, not a completed client. The shared Web reader now has the Windows-route keyboard contract (`N` note, `R` red highlight, `V` voice note, `Esc`, arrows/PageUp/PageDown), but the Windows installer, `Click.exe` shell, Start Menu shortcut, and optional desktop shortcut are not implemented yet. The Windows plan is documented in `docs/windows_client_plan.md`: P1 is a Windows browser version using local Reader API plus `http://localhost:18180/library`; P2 is a `Click.exe` shell using WebView2 or Tauri; P3 is an installer with Start Menu shortcut, optional desktop shortcut, runtime, logs, diagnostics, and uninstall cleanup.

iPad native shell boundary on 2026-07-01: `apps/ios/ClickShell` contains the local SwiftUI + WKWebView shell scaffold. It checks `/health`, opens `/home`, generates a stable local device ID, supports an optional local access token, and exposes the Mobile Workspace entries `阅读` / `录音` / `Hermes`. `阅读` keeps the existing `/library` and `/lan/reader` flow, `录音` opens the independent `~/Documents/Recordings` recording surface, and `Hermes` opens the Mac-side Hermes mobile page through the same local workspace gateway. This is not yet a signed or user-installed native iPad product. Final iPad shell acceptance still requires Apple signing and real iPad installation verification.

Android shell boundary on 2026-07-01: `apps/android/ClickShell` contains a local Android Activity + WebView shell. It checks `/health`, opens `/home`, saves the last Mac address, generates a stable local device ID, accepts an optional local access token, allows cleartext same-LAN HTTP, declares microphone permission for WebView recording and voice-message paths, and exposes a compact `首页` / `阅读` / `录音` / `Hermes` / `刷新` / `更换地址` menu. A local debug APK can be built with `scripts/build_android_click_shell.sh`; the build script places the APK and sha256 sidecar in the user's desktop backup folder without nested dated folders. This is a phone-test artifact, not a signed release APK.

Click Mobile Workspace P1.1-P6 on 2026-07-01: the local service exposes `/home`, `/recordings`, `/hermes`, `/v1/recordings/*`, `/v1/mobile/diagnostics`, `/v1/mobile/access/*`, `/v1/runtime/chat`, `/v1/voice/message`, and `/v1/voice/message/<voice_id>` as the phone-facing workspace boundary. New durable recording assets are saved under the independent `~/Documents/Recordings` total recording store using schema `local.recordings.audio_asset.v1`; the old `~/Library/Application Support/Click/KnowledgeInbox/Recordings` path is retained only as a read-only legacy source. Click, Reader, and Hermes are sources, entrances, or processors, not owners of recording assets. Recordings are not written to Reader PostgreSQL, Hermes internal state, or Marketplace OS. Mobile devices can request local access, be approved/revoked through local config files, and use a local token; browser debugging on the Mac remains possible without a device ID. The recordings page supports list/detail/audio, manual title/category/tag editing, non-destructive hide, and reprocess dry-run. Hermes mobile supports text chat, temporary voice-message upload through VoiceInbox, transcript/reply status, and optional edge-tts audio reply using `zh-CN-YunjianNeural` when available. The Android local launcher icon is the self-drawn Click Focus icon, with separate reading, recording, and Hermes entry icon resources.

Click Mobile/Web old-device Lite fallback on 2026-07-04: modern devices continue to default to the modern `/home`, `/library`, and `/lan/reader` flow. Lite is only selected by explicit `?ui=lite`, `click_ui=lite`, high-confidence legacy user agents such as KaiOS / Opera Mini / Android 4.x and below, or the earliest ES5 capability guard failing on `fetch` / `Promise` / `querySelector` / `addEventListener`. `?ui=modern` forces the modern path and stores `click_ui=modern` for correction. Lite routes are `/home-lite`, `/library-lite`, `/reader-lite`, and `/reader-lite/toc`; Lite keeps one set of entries only: 阅读, 录音, Hermes. Lite reading now follows a lightweight EPUB `spine` / `toc` / `readingOrder` split: TOC pages do not open every chapter to extract正文, and explicit chapter links are respected. Lite 书库每本书显示 `继续读` 和 `目录` 两个入口；`继续读` 优先复用 `reader.reading_positions` 的保存章节和 `page_index`，没有位置时才自动进入第一段正文。The concrete reader page only displays正文, with no visible book/library/toc/title/chapter/skip notice chrome; it uses ES5 screen-fit pagination, measuring rendered sentence units against the current viewport so one screen is one page, with a bottom safe reserve so old browser navigation bars do not cover the last line. Left/right edge hit zones and left/right swipe turn pages; at chapter boundaries they move to adjacent chapters. Lite page turns save `page_index`, `total_pages`, and the first sentence through `/reader-lite/position`. It skips EPUB inline navigation blocks such as `上一篇 / 回目录 / 下一篇`, and only shows a compact one-row red/note/audio action strip after a sentence is selected. Red highlight/cancel-red exits the selected action state and returns to reading; note and audio-note keep the selected state for editing/upload. It reuses existing Reader API data, `reader.annotations`, `reader.audio_notes`, `reader.reading_positions`, and the Mac voice pipeline; it does not change Reader API schema, PostgreSQL schema, or the modern reader. Mac native reader normalizes EPUB `details` collapsible sections inside the reading surface by removing default `open`, and converts scripture footnote `aside` blocks such as `2026-2-SITERO` Duokan verse notes into compact `sr-scripture-footnote` details. These verse blocks start collapsed, show a short `经节 ...` summary, and can be opened/closed from the summary or the original verse-reference icon.

Mac native reader selected-text action bar boundary on 2026-07-05: the failed selected-text repair was rolled back first, then rebuilt as one additive feature on top of the stable baseline in `Probe/NativeSentenceReader/SentenceReaderNative.swift`. Current implemented paths are: single-click English word lookup, double-click sentence note through the existing note panel, two-finger tap / secondary click no-selection whole-sentence red highlight, and deliberate正文 drag-selection action bar `复制 / 标红 / 备注`. `复制` uses a native pasteboard message, `标红` stores exact fragments with `range_locator.mode=text_selection`, and `备注` reuses the existing note panel with `range_locator.mode=text_selection_note`. `Command+Z` is the red-highlight undo path for both whole-sentence red and selected-fragment red; selected-fragment undo deletes the matching persisted `text_selection` annotation instead of only changing the WebView paint. The two-finger red path is now closed against WebKit's follow-up `contextmenu` event, so a no-selection sentence red highlight should not also show the right-click menu; when the selected-text action bar is visible, secondary click on正文 is swallowed and the action bar remains the selected-text path. The action bar is now drag-gated: double-click selection, right-click selection, lookup side effects, keyboard selection, and generic `selectionchange` cannot open it; `selectionchange` only hides the bar when the selection collapses. Click must not reintroduce active selection + two-finger tap as the selected-fragment path, and must not make `Command+C` a Click-owned reading-surface command.

Source-independent import rule: after Sentence Reader reports that an EPUB has been imported, the original EPUB path is no longer a runtime dependency. The app copies and verifies the book at `~/Library/Application Support/SentenceReader/Books/<book_hash>/book.epub`, normalizes saved book-library records to that owned path, and only registers the owned path with Reader API/PostgreSQL. The user may delete or move the original source file after import.

The product acceptance boundary is documented in `docs/product_acceptance.md`; the live service smoke is `scripts/sentence_reader_product_readiness_smoke.py`.

Latest verification on 2026-06-26: Library V2 changes the Mac app main interface from a management-panel style book list into a reading-first product home. The app still opens `http://127.0.0.1:18180/library?surface=mac-app` in the main window, but the visible first task is now `继续阅读`, followed by recent books and recent notes/red highlights. Book cards use real EPUB cover extraction when possible and generated book covers otherwise. Clicking a cover/card opens the selected book through `sentence-reader://open-native?book_id=...` into the original native sentence reader; iPad/browser access still uses `/lan/reader`. Notes and red highlights now have dedicated centers, while file paths and advanced status are moved into the details drawer or settings. The reader chrome remains simplified to `书库`, `目录`, `笔记`, `设置`, and `更多`.

Latest Library card interaction refinement on 2026-06-30: book cards no longer reveal high-priority 收藏/单词/详情 actions as a hover overlay and no longer jump upward on hover. The card/cover click remains the primary reading path. 收藏/单词/详情 now live in a low-priority row under the card content. Book removal is moved into explicit management mode: click `管理`, select one or more books, then use the management bar `移出书库`; selecting one book is the single-book path. The details drawer now offers `选择管理` instead of a destructive-looking remove button.

Latest Mac shell refinement on 2026-06-30: the same-window Library WebView no longer occupies the transparent macOS titlebar area. A dedicated 36px draggable titlebar strip sits above the Library/Vocabulary web surface, so the red/yellow/green window-control row can be used naturally for moving the window while book-card clicks remain reserved for reading.

Latest Library recovery refinement on 2026-06-30: the Library dashboard now scans existing owned EPUB copies under `~/Library/Application Support/SentenceReader/Books/*/book.epub` and re-registers them in PostgreSQL if the API index is missing them. This restores previously imported internal books such as `39学科 6阶段精学完整版` without asking the user to locate the original file again. The Mac Library WebView also implements the native EPUB open panel for `<input type=file>`, so `导入 EPUB` opens the system file picker. The top-left brand now shows only the product name `Click`, without a placeholder icon or tagline.

Latest iPad LAN touch verification on 2026-06-26: `/lan/reader` now uses a touch-first interaction model. Tap sentence opens a bottom sentence action bar for red highlight, note, voice note, copy, and cancel; `Aa` controls font size, line height, and side padding; `书库` returns to `/library?book_id=...`. Validation passed `v1_acceptance.sh`, `v21_ipad_lan_acceptance.sh`, Reader API pytest, Python compileall, Swift compile, package build, iPad static/API smoke, Library V2 smoke, live product readiness smoke, and live `/library` + `/lan/reader` marker checks. Current verified LAN URLs: library `http://<mac-lan-ip>:18180/library`, direct reader `http://<mac-lan-ip>:18180/lan/reader`.

Latest iPad compact-chrome refinement on 2026-06-26: `/lan/reader` now reduces visible control space again. The top toolbar is shorter, previous/next controls are arrow buttons, the bottom sentence action bar is a compact floating pill, and正文 no longer permanently reserves a large 96px bottom padding for hidden controls. `v21_ipad_lan_acceptance.sh`, static/API smoke, live product readiness smoke, and live compact chrome marker checks passed after the change. Current verified direct reader URL: `http://<mac-lan-ip>:18180/lan/reader`.

Latest Mac reader notes-rail refinement on 2026-06-27: the native Mac reading surface now opens with the notes rail collapsed by default. The notes rail starts below the reader header and ends above the footer when opened, so its `收起笔记` control is not covered by the main reader buttons. Validation passed Swift compile, immersive chrome static smoke, product static smoke, packaging, and `v18_reading_stability_acceptance.sh`.

Latest Mac selection/pagination refinement on 2026-06-29: the selection/system boundary is now explicit. `Command+C` remains the copy path for active text selection, while Mac two-finger tap on a sentence is the documented Sentence Reader whole-sentence red-highlight shortcut. Native pagination now aligns offsets to device pixels and adds a small left sliver guard on non-first pages to reduce previous-page column remnants after repeated page turns. Validation passed Swift compile, reader stability static smoke, annotation core V2 static smoke, product static smoke, packaging, and `v18_reading_stability_acceptance.sh`.

Latest interaction-router refinement on 2026-06-29: Mac native reader and iPad LAN reader now use a shared `SentenceReaderInteractionRouter` contract, documented in `docs/interaction_contract.md` and locked by `scripts/sentence_reader_interaction_contract_smoke.py`. In plain sentence-reading state, Sentence Reader actions have priority: double-click/double-tap opens the sentence note flow, Mac two-finger tap toggles whole-sentence red highlight, and tap/click focuses the sentence. On iPad, sentence-level red highlight is presented through the bottom sentence action bar. Clicking or tapping an English word now triggers lookup after a short delay; a real double-click/double-tap cancels that pending lookup and opens the note flow. Editing fields and controls still yield to system behavior. Active text selection is copied with `Command+C`; copy support must not make active selection a global system-priority state for reader gestures. `Option` + double-click remains a backup lookup path on pointer devices, and the native app now installs a standard application menu plus a key monitor so `Command+Q` quits Sentence Reader.

Latest vocabulary lookup repair on 2026-06-29: English click/tap lookup no longer depends only on prebuilt book-local vocabulary. If the current book has no `reader.book_vocab_items` entry for the clicked word, Reader API now falls back to `reader.dictionary_entries`, writes a safe dictionary-backed candidate into the current book vocabulary, and then returns a normal vocab item so review/known/edit actions still work. A compact general/business seed dictionary was added in `migrations/reader/005_general_dictionary_seed.sql`; larger ECDICT-style imports remain available through `scripts/sentence_reader_import_ecdict.py` when a CSV source is provided.

Latest online lookup and correction repair on 2026-07-02: daily lookup now treats the local open ECDICT-compatible dictionary as the normal non-model fallback. The Mac-side Hermes/Qwen `online_lookup` fallback is disabled by default and is only available when `SENTENCE_READER_ENABLE_HERMES_ONLINE_LOOKUP=1`, so high-frequency lookup pressure does not fall on the model runtime. The lookup card has an inline correction editor instead of a system prompt; user corrections are saved into the current book glossary with `source='user'`, recorded as `edit_meaning`, and take priority over dictionary or optional online lookup results. Invalid frontend sentence ids are ignored for lookup-event foreign keys instead of failing the save.

Latest Mac native selected-text refinement on 2026-07-05: the active implementation uses WebView JS handlers for `dblclick`, `mousedown`, `auxclick`, `contextmenu`, and drag-gated selection: double-click opens the existing sentence note flow, secondary click/two-finger tap on `.sr-sentence` toggles whole-sentence red only when there is no active正文 selection, and only deliberate正文 drag selection shows the compact `sr-selection-action-bar`. Generic `selectionchange` is not allowed to open the bar. The selected-text action bar is the only selected-text operation path; active selection + two-finger tap is not used for selected-fragment red, and `Command+C` is not installed as a Click reading command. The reader remains Click's own reader; open-source reader projects are reference material only, not replacement engines.

Latest page-turn stability refinement on 2026-07-04: Mac native reader and the LAN/mobile reader both treat horizontal wheel/trackpad page turns as one physical swipe = one page. Wheel-triggered turns opt out of queued page turns during animation, raise the horizontal threshold to `120`, keep a `520ms` inertia lock, and require a `420ms` quiet window before another wheel gesture can turn a page. Keyboard/page-button turns can still use the normal page-turn queue; only touchpad wheel inertia is suppressed.

Latest Life-study vocabulary pipeline status on 2026-06-29: the Genesis bilingual PDF has passed the accuracy-gated path for Genesis first 50 pages and Genesis full run. The 25 Genesis A/B entries have now been user-reviewed as approved in `Genesis-review-overrides.reviewed.json`, dry-run checked, and explicitly applied through `scripts/lifestudy_context_vocab_apply_review.py --apply`. The controlled import writes only A/B entries into the isolated `reader.domain_glossary_entries` and book-specific glossary/vocab boundary, not into `reader.dictionary_entries`. Current imported domain staging count is 25 active reviewed Genesis entries: 19 grade A and 6 grade B. C/D candidates remain report-only and never enter the reader lookup path. Reader API now uses this domain glossary only when the current book context clearly looks like Life-study/生命读经; normal books still fall back to book context and the general dictionary.

Current Life-study book-specific status: `1-01创世记生命读经.epub` has been imported into Sentence Reader's owned app-support library as `book_e0679064039e4e298e9faf3127b65876` with title `创世记生命读经 / Life-study of Genesis`. The same 25 A/B Genesis entries have been written to that book's `reader.book_glossary` and `reader.book_vocab_items` rows and marked `lifestudy_reviewed` after approval. The import script now preserves `source='user'` glossary rows on conflict, so future runs do not overwrite user-corrected meanings.

Latest Life-study review-gate status on 2026-06-29: Genesis now has a dedicated 25-entry human review pack under `reports/lifestudy_vocab_review/`. The reviewed override approves all 25 entries, missing book rows = 0, dictionary pollution = 0, human reviewed precision = 1.0, and `can_expand_next_volume=true`. The stage gate now reports `current_stage=ready_for_next_volume_probe`.

Latest Life-study review-apply status on 2026-06-29: reviewed override application tooling is now implemented in `scripts/lifestudy_context_vocab_apply_review.py`, with `scripts/lifestudy_context_vocab_apply_review_smoke.py` locking the safety contract. The tool rejects pending templates, defaults to dry-run, requires explicit `--apply` for writes, hides rejected domain entries instead of deleting them, marks rejected book vocab as ignored, and preserves user-corrected meanings. The Genesis reviewed override was applied successfully: `domain_reviewed=25`, `approved=25`, and general dictionary pollution remains 0.

Latest Life-study review UI status on 2026-06-29: Reader API now exposes `/lifestudy/vocab/review` plus `/api/lifestudy/vocab/review`, `/api/lifestudy/vocab/review/decision`, and `/api/lifestudy/vocab/review/dry-run`. The page can read the 25 Genesis entries, save approve/correct/reject decisions into `reports/lifestudy_vocab_review/Genesis-review-overrides.reviewed.json`, and run a no-write dry-run. It intentionally does not expose a database apply button; actual writes still require the explicit command-line `--apply` boundary.

Latest Life-study assistant-suggestion status on 2026-06-29: `scripts/lifestudy_context_vocab_review_suggestions.py` now generates `Genesis-review-suggestions.json`, `Genesis-review-suggestions.md`, and `Genesis-review-overrides.assistant-suggested.json`. The suggestion pack is marked `assistant_suggestions_not_human_review`; it can accelerate review but does not count as human-reviewed accuracy and does not write PostgreSQL. The expansion gate now uses reviewed precision >= 0.85 instead of treating every rejection as a permanent blocker.

Latest Life-study stage-gate status on 2026-06-29: `scripts/lifestudy_context_vocab_stage_gate.py` now generates `reports/lifestudy_vocab_review/Genesis-stage-gate.json` and `.md` without writing the database. It confirms Genesis first 50 pages, Genesis full run, controlled import, frontend lookup, and user-reviewed approval are complete. It now allows the next-volume probe.

Latest Life-study corpus expansion status on 2026-06-29: `scripts/lifestudy_corpus_inventory.py` now inventories the Life-study bilingual PDF corpus under the 中英对照 directory and writes `reports/lifestudy_vocab_corpus/lifestudy_corpus_inventory.json`, `.csv`, and `.md`. The inventory finds 53 PDFs, 51 processable volumes, and 2 combined reference PDFs. Genesis, Exodus, and Leviticus full no-write runs are complete. Exodus full run produced 70,872 line pairs, 24,066 aligned text units, 133,934 candidates, and 19 A/B importable no-write candidates. Leviticus full run produced 19,671 line pairs, 6,203 aligned text units, 41,434 candidates, and 12 A/B importable no-write candidates. The remaining 48 processable volumes still need first-50 no-write probes before any full no-write run. The next safe volume is Numbers.

Latest Life-study master vocabulary status on 2026-06-29: `scripts/lifestudy_master_vocab_aggregate.py` now merges all completed full no-write importable packs into one source-traced master vocabulary under `reports/lifestudy_vocab_corpus/lifestudy_master_vocab.json`, `.csv`, and `.md`. The current master file uses Genesis, Exodus, and Leviticus as sources, contains 56 source rows collapsed into 26 unique terms, keeps per-term source volume/page/evidence/status, and marks 1 meaning conflict for review. It remains report-only and does not write PostgreSQL.

Latest Life-study all-word master status on 2026-06-29: `scripts/lifestudy_all_words_master.py` now builds one unified all-book English word table for the full Life-study bilingual PDF corpus, not one table per volume. The output files are `reports/lifestudy_vocab_corpus/lifestudy_all_words_master.json`, `.csv`, and `.md`. The current full run covers all 51 processable volumes, 207,168 per-volume source word rows, 38,166 unique normalized English words, 37,616 content words, 5,297,307 raw English tokens, and 2,521,585 content-word tokens. Each row keeps total frequency, content frequency, content-word flag, source volume/page summary, sample English evidence, sample Chinese context, suggested meaning status, and `import_ready=false`. The table is report-only and does not write PostgreSQL.

Latest Life-study Context Vocabulary V1 status on 2026-06-29: the all-word master has been promoted through a 6-step quality gate into a small front-end usable V1 glossary. `scripts/lifestudy_context_vocab_v1_build.py` now creates a clean all-word master, rejected-noise report, Top 500 review queue, Top 2000 reserve queue, review pack, human-focus list, and importable pack. The current build starts from 38,166 raw unique words, keeps 37,302 raw words, merges them into 33,724 clean lemma groups, rejects 864 obvious non-useful/noisy words, and merges 3,578 variants. The Top 500 queue contains all required terms such as `economy`, `dispensing`, `mingled`, `transformation`, `regeneration`, `sanctification`, `justification`, `divine`, `spirit`, `life`, and `christ`. Only 34 entries have direct aligned Chinese evidence and are marked `import_ready=true`; 466 Top 500 entries remain review-only.

Latest Life-study V1 import status on 2026-06-29: `scripts/lifestudy_context_vocab_v1_apply.py` dry-run and explicit `--apply` are implemented. The explicit apply wrote 34 A-grade reviewed terms into `reader.domain_glossary_entries` with `domain='lifestudy'` and `volume='All'`. It did not write to `reader.dictionary_entries`; dictionary pollution remains 0. The script also supports `--hide` for non-destructive rollback/hiding of this V1 batch.

Latest Life-study V1 lookup status on 2026-06-29: Reader API lookup now checks the Life-study domain glossary before creating or returning dictionary fallback items for Life-study/生命读经 books. Live lookup smoke confirms `economy -> 经纶`, `dispensing -> 分赐`, and `mingled -> 调和` from `lifestudy_domain_glossary`, with source page and A-grade metadata. A non-Life-study book lookup does not use `lifestudy_domain_glossary`, so ordinary books are not polluted by the Life-study glossary.

Latest Life-study single-word lane status on 2026-06-29: the Genesis full pipeline produced 34 single-word candidates, but the original phrase-first import gate correctly kept all of them out of the production glossary as C-grade review items. `scripts/lifestudy_context_vocab_word_review_pack.py` now turns those 34 words into a review-only pack with suggested Chinese meanings and evidence, written to `reports/lifestudy_vocab_review/Genesis-word-review-pack.json`, `.csv`, and `.md`. This fixes the visibility problem: Genesis currently has 25 imported phrase entries plus 34 single-word review candidates, not only 25 processed English terms.

Latest Life-study all-word frequency status on 2026-06-29: `scripts/lifestudy_context_vocab_word_frequency.py` now builds a no-write Genesis full-word report. Genesis has 504,079 raw English word tokens, 9,243 raw unique words, 239,562 content word tokens after stopword removal, and 9,044 unique content words. The report writes `Genesis-word-frequency.json`, `.csv`, `.md`, plus `Genesis-raw-word-frequency.csv`. It gives every content word aligned Chinese context; 34 meanings come from Life-study/context-specific rules, 8,679 are low-confidence local dictionary fallback meanings, and 331 remain unmapped. None of these frequency rows are production import-ready until reviewed.

Latest Life-study all-word Chinese-context candidate status on 2026-06-29: `scripts/lifestudy_all_words_chinese_context_candidates.py` now produces the missing full-corpus review surface that the V1 import pack did not provide. It reads the 51-volume clean all-word master and writes `reports/lifestudy_vocab_corpus/lifestudy_all_words_chinese_context_candidates.json`, `.csv`, `.md`, and summary JSON. The table covers all 33,724 clean lemma rows, keeps the Chinese evidence sentence for every row, and fills `draft_meaning_zh_from_chinese_context` from Chinese-document evidence. It uses three levels: 34 known Life-study terms found directly in Chinese context, 6,321 dictionary-guided terms that still must appear in the Chinese evidence sentence, and 27,369 statistical Chinese-context candidates. This table is report-only and does not write PostgreSQL; only reviewed items may be promoted later. Smoke checks currently lock examples such as `love -> 爱`, `world -> 世界`, `wonderful -> 奇妙`, `economy -> 经纶`, `dispensing -> 分赐`, and `mingled -> 调和`.

Latest Life-study learning/integration guide status on 2026-06-29: `docs/lifestudy_vocab_learning_integration_guide.md` is now the human-facing guide for this vocabulary work. It separates the 33,724-row learning wordbook from the 34-entry front-end safe glossary, documents the 6,321 dictionary-guided candidates and 27,369 statistical candidates, and records the promotion rule: review first, dry-run, then explicitly apply only approved entries into Life-study-scoped glossary tables.

Latest Life-study dictionary-guided Review V2 status on 2026-06-29: `scripts/lifestudy_dictionary_guided_review_v2.py` now reviews the 6,321 dictionary-guided Chinese-context candidates from the full 51-volume wordbook. It produces `lifestudy_dictionary_guided_review_v2.json`, `.csv`, `.md`, summary JSON, and split CSVs for auto-accepted learning candidates, possible front-end-after-human-review candidates, and manual-review candidates. Current counts: 4,116 auto-accepted for learning, 2,205 still needing manual review, 4,102 possible front-end candidates after human review, 14 learning-only generic words, and 0 front-end import-ready rows. The script is no-write and does not change PostgreSQL or the reader lookup path.

Latest Life-study 2,205 needs-review adjudication status on 2026-06-29: `scripts/lifestudy_needs_review_adjudication_v1.py` now processes only `reports/lifestudy_vocab_corpus/lifestudy_dictionary_guided_review_v2_needs_manual_review.csv`, not the 4,116 auto-accepted rows and not the 26 front-end rows. The rule is strict: final Chinese meanings must come from the same `evidence_en` / `evidence_zh_simp` bilingual record, with English variants allowed only when the variant appears in that same English evidence. The page-header cleaner now removes only known Life-study book-title headers instead of arbitrary nearby Chinese text, so valid evidence such as `宇宙的`, `湿气`, and `西罗亚` is preserved. Outputs are `lifestudy_needs_review_adjudication_v1.csv/.json/.md`, plus split CSVs for corrected learning candidates, learning-only, reject, and still-needs-manual-review. Current counts: input 2,205, adjudicated 2,205, corrected learning candidates 3, learning-only 2,197, rejected 5, still-needs-manual-review 0, front-end import-ready 0, database writes 0. Corrections are `sermon -> 讲道`, `misused -> 误用`, and `siloam -> 西罗亚`; rejected examples include `message -> 教训` from header noise and generic `things/case/situation/stuff` meanings. `scripts/lifestudy_needs_review_adjudication_v1_smoke.py` passes and verifies row counts, split totals, no database writes, no front-end import flags, evidence/source fields, and that learning/corrected Chinese meanings appear in the same Chinese evidence.

Latest Life-study 2,205 needs-review frontendization status on 2026-06-30: `scripts/lifestudy_needs_review_frontend_queue.py` and `scripts/lifestudy_needs_review_frontend_adjudication.py` now promote only a small high-confidence slice of the 2,205 needs-review adjudication result into front-end lookup candidates. The queue reads only `lifestudy_needs_review_corrected_learning_candidate.csv` and `lifestudy_needs_review_learning_only.csv`; it excludes the 5 rejected rows, the 0 still-needs-manual-review rows, the old 26 front-end words, and the 4,116 auto-accepted learning rows. Current first-pass counts: 2,200 eligible learning rows, Top300 queue, 20 queue-level front-end candidates, 15 front-end-ready terms after strict evidence adjudication, 10 `frontend_ready`, 5 `frontend_corrected`, 279 `learning_only_keep`, 6 `needs_more_evidence`, 0 general-dictionary writes. The explicit apply wrote 15 B-grade terms only to `reader.domain_glossary_entries` with `source_title='Life-study Needs-review Frontend V1'`; preflight before apply found 85 active Life-study domain terms and dictionary pollution 0, and after apply `scripts/lifestudy_needs_review_frontend_applied_smoke.py` verifies 15 applied terms and dictionary pollution 0. Live lookup smoke verifies `authority -> 权柄`, `vision -> 异象`, `ascension -> 升天`, `parable -> 比喻`, and `holy -> 神圣的` resolve through `lifestudy_domain_glossary` for the imported Life-study book and do not leak into an ordinary book. Mac and iPad/LAN lookup cards now expose Life-study source metadata more clearly: source title, volume/page, evidence sentences, and a copy action on LAN.

2026-06-30 validation for that frontendization pass is complete: Reader API pytest, Python compileall, Swift native compile, app packaging, `v1_acceptance.sh`, `v21_ipad_lan_acceptance.sh`, needs-review front-end queue smoke, applied smoke, live lookup smoke, UI static smoke, and live product readiness smoke all pass. The 15 applied terms are the first trustworthy front-end slice from the 2,205 learning adjudication result; the remaining learning rows are not front-end glossary entries yet. The product identity cleanup now uses `Click` for user-facing app, Dock, menu, and web-surface naming; `SentenceReader` remains only as internal executable/support-path naming for compatibility with existing local data. Current verified iPad URL on this machine is `http://<mac-lan-ip>:18180/lan/reader`; the Reader API is running on `0.0.0.0:18180`.

Latest Life-study front-end candidate Review V2 status on 2026-06-29: `scripts/lifestudy_frontend_candidate_review_v2.py` now narrows the 4,102 possible front-end candidates into a Top 500 human-review pack. The stricter gate requires domain/theological hints and keeps high-frequency generic words out of the suggested front-end list. Current counts: 500 top review rows, 28 `approve_after_human_check` suggestions, 472 `needs_human_review`, and 0 front-end import-ready rows. It also writes `lifestudy_frontend_candidate_review_v2_overrides_template.json/.csv` for future human approve/correct/reject decisions. No PostgreSQL writes are performed.

Latest Life-study front-end candidate adjudication V2 status on 2026-06-29: `scripts/lifestudy_frontend_candidate_adjudication_v2.py` now performs the operator-delegated Codex review for those 28 suggestions instead of asking the user to inspect the table row by row. The no-write adjudication output is `reports/lifestudy_vocab_corpus/lifestudy_frontend_candidate_adjudication_v2.csv/.json/.md`; the next-step candidate pack is `lifestudy_frontend_candidate_adjudication_v2_ready_for_dry_run.csv`. Current counts: 28 reviewed, 21 approved as-is, 5 corrected, 1 learning-only, 1 needs-more-evidence, 26 ready for the next controlled dry-run candidate pack, and 0 front-end import-ready rows. Corrections include `redemption -> 救赎`, `righteousness -> 公义`, `reality -> 实际`, `anointing -> 受膏；膏油的涂抹`, and `priesthood -> 祭司职分`. `living` stays learning-only and `sacrifice` stays held for more evidence. No PostgreSQL writes are performed.

Latest Life-study front-end candidate apply-boundary status on 2026-06-29: `scripts/lifestudy_frontend_candidate_adjudication_apply.py` now provides the controlled dry-run/apply/hide boundary for the 26 Codex-adjudicated first-batch words. It defaults to dry-run, writes only to `reader.domain_glossary_entries` when explicitly run with `--apply`, preserves user-corrected meanings on conflict, supports non-destructive `--hide`, and never writes to `reader.dictionary_entries`. The explicit apply has now been run for those 26 words. Preflight before apply found 59 active Life-study domain terms and 0 dictionary pollution; after apply, the Life-study active domain count is 85 and the 26 new entries are tagged with `metadata.source='lifestudy_frontend_candidate_adjudication_v2'`. `scripts/lifestudy_frontend_candidate_adjudication_apply_smoke.py` and `scripts/lifestudy_frontend_candidate_adjudication_applied_smoke.py` pass, confirming dry-run safety, 26 applied rows, corrected meanings, held-term exclusion, and dictionary pollution 0.

Latest Life-study front-end adjudicated lookup status on 2026-06-29: `scripts/lifestudy_frontend_candidate_adjudication_live_lookup_smoke.py` now verifies that applied adjudicated terms appear through the real Reader API lookup path for the imported Life-study Genesis book and do not leak into an ordinary book. Live checks pass for `redemption -> 救赎`, `righteousness -> 公义`, `reality -> 实际`, `anointing -> 受膏；膏油的涂抹`, and `priesthood -> 祭司职分`; each resolves from `lifestudy_domain_glossary` with B-grade adjudicated metadata.

Latest Life-study phrase/uncommon document status on 2026-06-29: `scripts/lifestudy_context_vocab_phrase_uncommon_pack.py` now builds a review-only Genesis document combining 25 active high-confidence phrases, 150 high-frequency phrase candidates, and 34 uncommon/domain words. Outputs are `Genesis-phrase-uncommon-pack.json`, `.csv`, and `.md`. The document is intentionally smaller than the 9,044-word frequency report and is meant to be the first practical review surface for phrases and uncommon words before expanding beyond Genesis.

Latest voice provider refinement on 2026-06-30: Mac voice notes expose a compact provider picker and now default to the Click software-layer local recognition path (`FunASR`). Apple Speech remains available as the backup provider; if FunASR transcription fails, the same audio note falls back to system speech recognition instead of being treated as a final failure. Product readiness smoke still treats FunASR health as optional unless the selected/default local recognition path is being verified.

Repeat verification on 2026-06-25 13:27 CST passed the hard product checks again: `v1_acceptance.sh`, `v21_ipad_lan_acceptance.sh`, Reader API pytest, Python compileall, Swift compile, app packaging, and live LAN/FunASR readiness. No functional source or PostgreSQL schema change was required.

The current product boundary is intentionally strict:

- the app can view the review queue
- the app can run an operator dry-run
- the app can open a single draft detail report when one exists
- the app can open a Cognitive OS dashboard report before approval
- the app can show a native Cognitive OS dashboard table with status filtering
- the native dashboard can open the Markdown dashboard and selected draft source, but cannot approve drafts
- the API can preflight a selected draft without writing formal intakes
- the API can approve exactly one selected draft only after a matching `APPROVE <candidate_intake_id>` confirmation
- the App can surface approval only for `ready_to_approve` items and requires manual confirmation text before calling approval
- the dashboard must show approval history, quality-gate result, and rollback path instead of hiding mutation consequences
- the app cannot silently approve drafts or mutate active packs
- formal promotion and active-pack rebuild still require the explicit operator approval path

The existing V1 native reading shell already supports:

- bundled EPUB opening
- black reading surface
- compact top bar
- `目录(26)` chapter menu for the current sample book
- horizontal page turn with book-like animation
- edge navigation into previous/next EPUB spine item
- image containment
- reading position memory using `UserDefaults`
- double-click note editor
- note-editor voice input with local FunASR fallback to system speech recognition
- Reader API connection bootstrap
- Reader API-backed reading position save/restore
- Reader API-backed red highlight save/restore
- Reader API-backed note save
- V1.3 hard acceptance script: `scripts/v13_swift_data_acceptance.sh`
- right-side current-book notes rail
- notes search and type filter
- note edit/delete actions
- double-click note to jump back to its sentence
- V1.4 hard acceptance script: `scripts/v14_notes_acceptance.sh`
- open/import EPUB from disk
- local book library stored under `~/Library/Application Support/SentenceReader/Books`
- top-bar book switcher
- per-book Reader API records via `reader.books` and `reader.book_files`
- per-book reading-position fallback keys
- V1.5 hard acceptance script: `scripts/v15_multibook_acceptance.sh`
- top-bar export action for the current book
- Reader API-generated Markdown and JSON annotation exports
- export records persisted through `reader.exports`
- V1.6 hard acceptance script: `scripts/v16_export_acceptance.sh`
- persistent voice-note audio files under `~/Library/Application Support/SentenceReader/AudioNotes`
- Reader API audio-note lifecycle through `reader.audio_notes`
- voice-note pending/transcribed/failed status tracking
- voice-note annotation binding after the note is saved
- FunASR warm service: the App starts `funasr_worker.py --server` in the background after launch, prefers local `/transcribe`, and falls back to the old one-shot worker path when unavailable
- V1.7 hard acceptance script: `scripts/v17_voice_acceptance.sh`
- persistent reading settings through `SentenceReader.readerSettings.v1`
- top-bar `设置` menu for font size, line height, margin, theme, and reset
- WebView CSS-variable bridge through `__sentenceReaderApplySettings`
- dark and warm reading themes
- pagination hotfix: the WebView page surface now uses the full viewport height, allows paragraphs to flow across page breaks, and computes page count from measured content width to avoid trailing blank pages
- immersive chrome hotfix: the top command bar and bottom status bar are hidden by default, the WebView and notes rail use the full window height, and moving the mouse to the top/bottom edge temporarily reveals the controls
- Annotation Core V2 hotfix: drag/multi-line selection plus right-click/two-finger tap can batch-mark covered sentences, note markers restore into the page, single-clicking a note-marked sentence previews the saved note, and colon/semicolon are no longer treated as hard sentence boundaries
- Annotation Core V2 file-level plan: `docs/annotation_core_v2_plan.md`
- Annotation Core V2 smoke: `scripts/sentence_reader_annotation_core_v2_static_smoke.py`
- V1.8 hard acceptance script: `scripts/v18_reading_stability_acceptance.sh`
- top-bar `同步` action for the current book
- Reader API-generated Hermes/Cognitive OS sync payloads using schema `sentence_reader.hermes_sync.v1`
- sync payload files under `~/Library/Application Support/SentenceReader/HermesSync`
- pending sync attempts persisted through `reader.sync_events`
- V1.9 hard acceptance script: `scripts/v19_hermes_sync_acceptance.sh`
- bundled `ReaderRuntime` resources inside `build/Click.app`
- Reader API startup script discovery from bundled runtime first, development path second
- product diagnostics report generation
- non-destructive PostgreSQL backup artifact generation
- restore verification without writing back to PostgreSQL
- V2.0A hard acceptance script: `scripts/v20_product_packaging_acceptance.sh`
- bundled `.venv-reader-api` inside `build/Click.app/Contents/Resources/ReaderRuntime`
- bundled `migrations/reader/001_reader_schema.sql` inside ReaderRuntime
- package-local Reader API launch smoke test
- V2.0B hard acceptance script: `scripts/v20b_runtime_acceptance.sh`
- Hermes ingestion endpoint `POST /sync/hermes/ingest`
- command-line ingestion worker `scripts/sentence_reader_hermes_ingest.py`
- ReaderRuntime copy of the ingestion worker
- incoming asset handoff under `hermes_cognitive_os/incoming/sentence_reader`
- sync events marked `synced` after successful ingestion
- V2.0C hard acceptance script: `scripts/v20c_hermes_ingest_acceptance.sh`
- reviewable reader-intake drafts from incoming assets
- draft payload schema `sentence_reader.book_intake_draft.v1`
- draft quality gate schema `sentence_reader.reader_intake_quality_gate.v1`
- draft output under `hermes_cognitive_os/incoming/sentence_reader_drafts`
- V2.0D hard acceptance script: `scripts/v20d_intake_draft_acceptance.sh`
- explicit draft promotion worker `scripts/sentence_reader_promote_intake_draft.py`
- promotion report schema `sentence_reader.intake_draft_promotion_report.v1`
- promotion requires `--approved` before writing formal `hermes_cognitive_os/intakes/*.json`
- promotion can optionally rebuild active pack and run the Cognitive OS quality gate
- V2.0E hard acceptance script: `scripts/v20e_intake_promotion_acceptance.sh`
- review queue worker `scripts/sentence_reader_review_queue.py`
- review queue schema `sentence_reader.intake_review_queue.v1`
- queue classification: `ready_to_approve`, `needs_review`, `blocked`, `already_promoted`
- queue Markdown and JSON reports
- V2.0F hard acceptance script: `scripts/v20f_review_queue_acceptance.sh`
- active-pack operator `scripts/sentence_reader_active_pack_operator.py`
- active-pack operator report schema `sentence_reader.active_pack_operator_report.v1`
- rollback manifest schema `sentence_reader.active_pack_operator_rollback.v1`
- approved draft -> formal intake -> active pack rebuild -> optional Cognitive OS quality gate
- rollback-on-failure for files changed by the operator run
- V2.0G hard acceptance script: `scripts/v20g_active_pack_operator_acceptance.sh`
- Reader API cognitive queue endpoint `POST /cognitive/review-queue`
- Reader API operator dry-run endpoint `POST /cognitive/operator/dry-run`
- top-bar Swift `认知` action that checks queue state and opens the queue Markdown report
- V2.0H hard acceptance script: `scripts/v20h_cognitive_operator_entry_acceptance.sh`
- Reader API review item endpoint `POST /cognitive/review-item`
- Reader API selected preflight endpoint `POST /cognitive/operator/preflight`
- top-bar Swift `认知` action now opens a single-draft detail report when available
- V2.0I hard acceptance script: `scripts/v20i_review_detail_acceptance.sh`
- Reader API explicit approval endpoint `POST /cognitive/operator/approve`
- approval requires exact confirmation text `APPROVE <candidate_intake_id>`
- approval rejects blocked/not-ready drafts unless explicitly overridden where allowed
- V2.0J hard acceptance script: `scripts/v20j_explicit_approval_acceptance.sh`
- Swift `认知` flow exposes `批准入库...` only for `ready_to_approve` detail items
- Swift approval prompt requires exact `APPROVE <candidate_intake_id>` before calling the approval endpoint
- V2.0K hard acceptance script: `scripts/v20k_approval_ux_acceptance.sh`
- Reader API Cognitive dashboard endpoints `GET /cognitive/dashboard` and `POST /cognitive/dashboard`
- Cognitive dashboard schema `sentence_reader.cognitive_dashboard.v1`
- dashboard Markdown and JSON reports under `~/Library/Application Support/SentenceReader/CognitiveOps/dashboard`
- dashboard combines review queue counts, grouped draft status, approval history, quality-gate result, and rollback manifest paths
- Swift `认知` flow now offers `打开仪表盘` before opening detail or approving
- V2.0L hard acceptance script: `scripts/v20l_cognitive_dashboard_acceptance.sh`
- native Swift Cognitive dashboard window controller `CognitiveDashboardWindowController`
- native dashboard rows parse `CognitiveDashboardDraftRow` and `CognitiveDashboardHistoryRow`
- native status filter: `全部` / `可批准` / `待审` / `阻塞` / `已入库`
- native dashboard actions: `打开Markdown仪表盘`, `打开所选草稿`, `关闭`
- V2.0M hard acceptance script: `scripts/v20m_native_cognitive_dashboard_acceptance.sh`
- runtime portability report script `scripts/sentence_reader_runtime_portability.py`
- packaged `ReaderRuntime/runtime_manifest.json`
- product diagnostics includes runtime portability status
- runtime portability report files under `reports/sentence_reader_runtime_portability_report.json` and `.md`
- current machine is verified ready, while clean-Mac blockers are explicit instead of hidden
- V2.0N hard acceptance script: `scripts/v20n_runtime_portability_acceptance.sh`
- runtime bootstrap script `scripts/sentence_reader_runtime_bootstrap.py`
- runtime bootstrap smoke `scripts/sentence_reader_runtime_bootstrap_smoke.py`
- `run_reader_api.sh` can call bootstrap to select a working Python when bundled runtime Python is unavailable
- user-level Reader API venv bootstrap path under `~/Library/Application Support/SentenceReader/Runtime/.venv-reader-api`
- dependency installation remains explicit through `SENTENCE_READER_BOOTSTRAP_INSTALL_DEPS=1`
- product diagnostics includes runtime bootstrap status
- V2.0O hard acceptance script: `scripts/v20o_runtime_bootstrap_acceptance.sh`
- runtime config script `scripts/sentence_reader_runtime_config.py`
- first-run preflight script `scripts/sentence_reader_first_run_preflight.py`
- first-run preflight smoke `scripts/sentence_reader_first_run_preflight_smoke.py`
- Swift FunASR path resolution now checks UserDefaults, environment variables, runtime_config.json, then the legacy default
- product diagnostics includes first-run preflight status
- V2.0P hard acceptance script: `scripts/v20p_first_run_preflight_acceptance.sh`
- top-bar `环境` action opens a native runtime environment window
- runtime environment window can rerun first-run preflight from the App
- runtime environment window can save FunASR Python and worker paths to UserDefaults and `runtime_config.json`
- runtime environment window can open the generated preflight report
- V2.0Q hard acceptance script: `scripts/v20q_runtime_settings_acceptance.sh`
- first-run guide appears automatically only when `first_run_ready=false`
- runtime environment window now includes `首启修复引导`
- guide text explains PostgreSQL, Runtime Python/bootstrap, and FunASR recovery steps
- guide can be copied with `复制修复指引`
- config folder can be opened from the environment window
- V2.0R hard acceptance script: `scripts/v20r_first_run_guide_acceptance.sh`
- reading-related app icon generator: `scripts/generate_sentence_reader_icon.py`
- generated `.icns` asset: `assets/SentenceReader.icns`
- packaged App uses `CFBundleIconFile=SentenceReader`
- Dock pin helper: `scripts/pin_sentence_reader_to_dock.py`
- product identity smoke: `scripts/sentence_reader_identity_static_smoke.py`
- V2.0S hard acceptance script: `scripts/v20s_product_identity_acceptance.sh`
- current packaged app has been added to the macOS Dock as `Click`
- V2.1 iPad LAN Reader: trusted same-LAN browser access through `GET /lan/reader`
- iPad LAN Reader can list books, open EPUB spine chapters, serve EPUB assets, save Reader API-backed red highlights, save notes, and save paginated reading position
- iPad LAN Reader hotfix: chapter/book lists are now hidden in a slide-out `目录` drawer instead of permanently occupying the reading page
- iPad LAN Reader hotfix: page turning now uses `turnPage` with button, keyboard, and horizontal touch-swipe support; page position is stored as `lan_reader_paginated`
- iPad LAN Reader hotfix: chapter HTML is reduced to body content before rendering, and the page uses full `100dvh` height plus measured column pagination to avoid cropped text and trailing blank pages
- iPad LAN Reader product-readiness hotfix: pagination now measures real sentence/image element rectangles through `measuredContentWidth`, reducing false blank pages from CSS column scroll width
- iPad LAN Reader note preview: clicking a note-marked sentence now opens a readable bottom note panel instead of only using the truncated status text
- iPad LAN Reader voice entry: `语音` now tries browser recording upload to Mac-side FunASR through `POST /lan/audio-notes/transcribe`, falls back to iPad audio file/capture upload, then falls back to browser speech/manual note where needed
- iPad LAN Reader touch interaction refinement: tapping a sentence now opens a bottom action bar for `红标` / `笔记` / `语音` / `复制` / `取消`; the bottom bar is the documented red-highlight path; top toolbar no longer carries sentence-level actions
- iPad LAN Reader reading settings: `Aa` opens font size, line-height, and side-padding controls persisted in browser local storage as `sentenceReaderLanSettings`
- iPad LAN Reader navigation refinement: `书库` returns to `/library?book_id=...`, so the iPad reader is no longer a dead-end page
- iPad LAN Reader live readiness smoke: `scripts/sentence_reader_product_readiness_smoke.py` checks exactly one live `18180` listener, verifies the LAN page is the new drawer/paginated/voice page, and checks FunASR `/health`
- V2.1 hard acceptance script: `scripts/v21_ipad_lan_acceptance.sh`

Still outside the current local-reader scope:

- public internet access, user accounts, HTTPS certificate management, WebSocket live sync, and a native iPad app are not implemented yet.
- Windows browser and desktop clients are planned but not implemented yet; the next boundary is P1 browser verification, not a rewritten Windows reader.
- iPad browser microphone behavior depends on Safari permissions and secure-origin rules; the product fallback is audio capture/upload plus manual note if browser recording is blocked.
- PDF annotation parity is not implemented yet.
- Annotation Core V2 does not change the PostgreSQL schema; multi-sentence locators are stored through existing annotation metadata/range locator fields.

## V1.2 Added This Round

- PostgreSQL migration file: `migrations/reader/001_reader_schema.sql`
- Reader API package skeleton: `reader_api/`
- Reader API dependency list: `requirements-reader-api.txt`
- static Reader API smoke test: `scripts/reader_api_static_smoke.py`
- mock Reader API CRUD pytest: `tests/test_reader_api_mock.py`
- PostgreSQL readiness checker: `scripts/reader_pg_status.py`
- PostgreSQL migration runner: `scripts/reader_pg_migrate.py`
- PostgreSQL CRUD smoke runner: `scripts/reader_pg_smoke.py`
- live Reader API + PostgreSQL smoke runner: `scripts/reader_api_live_smoke.py`
- V1.2 hard data acceptance: `scripts/v12_data_acceptance.sh`
- PostgreSQL setup notes: `docs/postgresql_setup.md`
- V1 acceptance now checks Reader API static contract, compiles Python files, and runs the Reader API mock pytest when dependencies are available.
- Project-local Python environment: `.venv-reader-api/`
- Local development ignore file: `.gitignore`

## Validation Completed This Round

- `scripts/v12_data_acceptance.sh`: passed.
- Postgres.app 2.9.5 / PostgreSQL 18.4 installed at `/Applications/Postgres.app`.
- PostgreSQL data directory: `~/Library/Application Support/SentenceReader/Postgres/data-18`.
- `sentence_reader` database created.
- `reader` schema migration passed.
- direct PostgreSQL CRUD smoke passed.
- live Reader API + PostgreSQL smoke passed.
- `tests/test_reader_api_mock.py`: passed, 7 tests.
- `scripts/v1_acceptance.sh`: passed.
- Reader API static smoke: passed.
- Python compileall for `reader_api` and `scripts`: passed.
- Swift/readium V1 acceptance path: passed through `scripts/v1_acceptance.sh`.
- `scripts/reader_pg_status.py`: works and reports PostgreSQL/schema ready.
- `scripts/reader_api_http_smoke.py`: passed against a running HTTP Reader API.
- Swift native reader compile: passed after Reader API integration.
- `scripts/package_sentence_reader_app.py`: passed after Reader API integration.
- `scripts/v13_swift_data_acceptance.sh`: passed.
- `scripts/reader_api_notes_loop_smoke.py`: passed.
- `scripts/v14_notes_acceptance.sh`: passed.
- `scripts/reader_api_multibook_smoke.py`: added for V1.5.
- `scripts/v15_multibook_acceptance.sh`: passed.
- `scripts/reader_api_export_smoke.py`: passed.
- `scripts/v16_export_acceptance.sh`: passed.
- `scripts/reader_api_audio_notes_smoke.py`: passed.
- `scripts/v17_voice_acceptance.sh`: passed.
- `scripts/reader_stability_static_smoke.py`: passed.
- `scripts/v18_reading_stability_acceptance.sh`: passed.
- `scripts/reader_api_hermes_sync_smoke.py`: passed.
- `scripts/v19_hermes_sync_acceptance.sh`: passed.
- `scripts/sentence_reader_product_static_smoke.py`: passed.
- `scripts/sentence_reader_product_diagnostics.py`: passed.
- `scripts/sentence_reader_backup.py`: passed with `--skip-files` smoke backup.
- `scripts/sentence_reader_restore_verify.py`: passed.
- `scripts/v20_product_packaging_acceptance.sh`: passed.
- `scripts/reader_runtime_launch_smoke.py`: passed.
- `scripts/v20b_runtime_acceptance.sh`: passed.
- `scripts/reader_api_hermes_ingest_smoke.py`: passed.
- `scripts/v20c_hermes_ingest_acceptance.sh`: passed.
- `scripts/reader_intake_draft_smoke.py`: added for V2.0D.
- `scripts/reader_intake_draft_smoke.py`: passed.
- `scripts/v20d_intake_draft_acceptance.sh`: passed.
- Production Cognitive OS dry run: passed with `discovered_count=0`, meaning no real reader incoming assets exist yet.
- `scripts/reader_intake_promotion_smoke.py`: passed.
- `scripts/v20e_intake_promotion_acceptance.sh`: passed.
- Production Cognitive OS promotion dry run: passed with `draft_count=0` and `promoted_count=0`, meaning no real draft was promoted.
- `scripts/reader_review_queue_smoke.py`: passed.
- `scripts/v20f_review_queue_acceptance.sh`: passed.
- Production Cognitive OS review queue dry run: passed with `draft_count=0`, meaning no real draft is waiting for review yet.
- `scripts/reader_active_pack_operator_smoke.py`: passed.
- `scripts/v20g_active_pack_operator_acceptance.sh`: passed.
- Production Cognitive OS active-pack operator dry run: passed with `selected_count=0`, meaning no real active pack rebuild or promotion was performed.
- `scripts/reader_api_cognitive_operator_smoke.py`: passed.
- Swift native reader compile after adding the `认知` entry: passed.
- Reader API static smoke after adding cognitive endpoints: passed.
- Product static smoke after adding API/App cognitive markers: passed.
- `scripts/v20h_cognitive_operator_entry_acceptance.sh`: passed.
- `scripts/reader_api_cognitive_operator_smoke.py`: upgraded to cover one ready draft, review-item detail, selected preflight, and no formal-intake writes.
- Swift native reader compile after adding detail report opening: passed.
- Reader API static smoke after adding review-item/preflight endpoints: passed.
- Product static smoke after adding V2.0I API/App markers: passed.
- `scripts/v20i_review_detail_acceptance.sh`: passed.
- `scripts/reader_api_cognitive_operator_smoke.py`: upgraded to reject bad confirmation and approve one ready draft in a temporary Cognitive OS root.
- Swift native reader compile after V2.0J API changes: passed.
- Reader API static smoke after adding approve endpoint: passed.
- Product static smoke after adding V2.0J markers: passed.
- `scripts/v20j_explicit_approval_acceptance.sh`: passed.
- Swift native reader compile after adding V2.0K approval UX: passed.
- Product static smoke after adding V2.0K Swift markers: passed.
- `scripts/v20k_approval_ux_acceptance.sh`: passed.
- `scripts/reader_api_cognitive_operator_smoke.py`: upgraded to cover the Cognitive dashboard schema, Markdown report, approval history, and rollback visibility.
- Swift native reader compile after adding V2.0L dashboard UX: passed.
- Reader API static smoke after adding dashboard endpoints: passed.
- Product static smoke after adding V2.0L dashboard markers: passed.
- `scripts/v20l_cognitive_dashboard_acceptance.sh`: passed.
- `scripts/sentence_reader_native_cognitive_dashboard_smoke.py`: passed.
- Swift native reader compile after adding V2.0M native dashboard table: passed.
- Product static smoke after adding V2.0M native dashboard markers: passed.
- `scripts/v20m_native_cognitive_dashboard_acceptance.sh`: passed.
- `scripts/sentence_reader_runtime_portability.py`: passed with `current_machine_ready=True`.
- Runtime portability report currently says `clean_mac_ready=False` with blockers: `runtime_python_points_to_xcode`, `runtime_python_points_outside_ReaderRuntime`, `postgres_not_bundled`.
- Product diagnostics with runtime portability: passed.
- Packaged ReaderRuntime launch smoke after adding runtime manifest: passed.
- `scripts/v20n_runtime_portability_acceptance.sh`: passed.
- `scripts/sentence_reader_runtime_bootstrap_smoke.py`: passed.
- Runtime bootstrap report: passed with `startup_ready=True`.
- Product diagnostics with runtime bootstrap: passed.
- Packaged ReaderRuntime launch smoke after bootstrap integration: passed.
- `scripts/v20o_runtime_bootstrap_acceptance.sh`: passed.
- `scripts/v20p_first_run_preflight_acceptance.sh`: passed.
- `scripts/v20q_runtime_settings_acceptance.sh`: passed.
- `scripts/v20r_first_run_guide_acceptance.sh`: passed.
- `scripts/v20s_product_identity_acceptance.sh`: passed.
- `scripts/sentence_reader_annotation_core_v2_static_smoke.py`: passed.
- Swift native reader compile after Annotation Core V2: passed.
- Python compileall for `reader_api`, `scripts`, and `tests` after Annotation Core V2: passed.
- `scripts/v18_reading_stability_acceptance.sh`: passed after Annotation Core V2.
- `scripts/sentence_reader_identity_static_smoke.py`: passed after final package.
- Dock dry-run reports the current packaged app is already present.
- V2.1 iPad LAN Reader plan: `docs/ipad_lan_reader_plan.md`.
- Reader API now exposes same-LAN browser routes under `/lan/reader` and `/lan/books/...`.
- Swift top bar now has an `iPad` entry that shows/copies `http://<local-ip>:18180/lan/reader` and can launch Reader API in LAN mode when the App owns the API process.
- `scripts/sentence_reader_ipad_lan_static_smoke.py`: passed.
- `scripts/reader_api_ipad_lan_smoke.py`: passed with a temporary EPUB.
- Swift native reader compile after iPad LAN entry: passed.
- Product static smoke after iPad LAN entry: passed.
- `scripts/v21_ipad_lan_acceptance.sh`: passed.
- Current LAN test URL on this machine: `http://<mac-lan-ip>:18180/lan/reader`.
- Live LAN API smoke: `http://127.0.0.1:18180/lan/reader` and `http://<mac-lan-ip>:18180/lan/reader` both returned HTTP 200.
- Real default book LAN manifest smoke: returned schema `sentence_reader.lan_manifest.v1` with 50 chapters.

V2.1 current limitation:

- This is trusted same-LAN browser access only.
- It is not public internet access and has no account/login layer yet.
- Current live readiness requires exactly one Reader API process on `18180`, bound to all interfaces. If an old local-only process is found, stop it and restart the LAN service before considering iPad ready.

## Latest Recheck

The current shell sees PostgreSQL through Postgres.app:

```text
psql /Applications/Postgres.app/Contents/Versions/latest/bin/psql
postgres /Applications/Postgres.app/Contents/Versions/latest/bin/postgres
pg_ctl /Applications/Postgres.app/Contents/Versions/latest/bin/pg_ctl
initdb /Applications/Postgres.app/Contents/Versions/latest/bin/initdb
brew not found
docker not found
```

The project-local Reader API environment is ready:

```text
fastapi installed
uvicorn installed
psycopg installed
pytest installed
httpx installed
```

Reader API Python dependencies were installed into the project-local virtual environment:

```text
.venv-reader-api/
```

They are still not installed in the current system Python, by design:

```text
fastapi missing
uvicorn missing
psycopg missing
pytest missing
```

`scripts/v12_data_acceptance.sh` is now the hard gate for V1.2. It first checks PostgreSQL server readiness, then creates/migrates `sentence_reader`, then verifies the `reader` schema and CRUD paths.

The V1.2 hard gate currently reaches this point:

```text
== V1.2 static/API contract ==
reader api static PASS
6 passed
== V1.2 PostgreSQL schema readiness ==
tcp ok=True host=localhost port=5432
database ok=True
```

## V1.5 Status

V1.5 adds the minimum product-grade multi-book loop:

- user-selected EPUB files are copied into app support storage; originals are not moved
- EPUB files are unpacked into an app-owned book directory
- imported books are deduplicated by a stable file hash
- the top bar exposes `打开` and `书籍` controls
- switching books reloads chapters, TOC, notes rail, red highlights, and reading position for that book
- API `/books` lists persisted books and latest file metadata
- API smoke verifies two books keep independent reading positions and annotations

V1.5 is intentionally not a bookshelf design. It is the data and workflow foundation for multi-book reading.

## V1.6 Status

V1.6 adds the minimum product-grade export loop:

- Reader API exposes `POST /books/{book_id}/export`
- exports include current-book notes and red highlights
- Markdown preserves book title, author, book hash, chapter title, source sentence, note text, locator, sentence index, and timestamps
- JSON companion uses schema `sentence_reader.annotations_export.v1`
- generated files are written under `~/Library/Application Support/SentenceReader/Exports` by default
- every Markdown/JSON output is recorded in `reader.exports`
- Swift App top bar exposes `导出` and calls the Reader API contract
- smoke test verifies files exist, contents include note/red source text, and export records exist

V1.6 is intentionally not cloud sync and not AI summary. It is the durable local export boundary.

## V1.7 Status

V1.7 adds the minimum product-grade voice-note persistence loop:

- recorded WAV files are stored in app support instead of temporary storage
- Swift creates a pending audio note before transcription
- successful FunASR or Apple Speech transcription updates the audio note to `transcribed`
- transcription failure updates the audio note to `failed`
- saved text notes bind the audio note to the resulting annotation when possible
- Reader API exposes `POST /audio-notes`, `PATCH /audio-notes/{audio_note_id}`, and `GET /books/{book_id}/audio-notes`
- the native App now warms a local FunASR server on launch using the configured FunASR Python/worker paths
- transcription first tries the warm local `/transcribe` endpoint, then safely falls back to the original subprocess worker and Apple Speech fallback
- the warm service writes logs under `~/Library/Application Support/SentenceReader/Logs/funasr_server.log` and is terminated when the App exits if the App started it
- smoke test verifies pending -> transcribed lifecycle, annotation binding, transcript persistence, and per-book listing

V1.7 now has a local warm FunASR service boundary. It is still not a separately installed system daemon; it runs while Sentence Reader is open and falls back cleanly when unavailable.

## V1.8 Status

V1.8 adds the minimum product-grade reading stability layer:

- reader settings are persisted in `UserDefaults` under `SentenceReader.readerSettings.v1`
- top-bar `设置` menu controls font size, line height, horizontal margins, dark theme, warm theme, and reset
- settings are sanitized to safe ranges before saving
- settings are injected into the WebView through `window.__sentenceReaderApplySettings`
- CSS variables drive font size, line height, margins, column width, column gap, background, text color, and focus color
- page layout reflows after settings change without touching annotations or reading position storage
- page layout now avoids self-reserved bottom space by using full viewport height and tight bottom padding
- pagination now measures actual sentence/image rects instead of trusting trailing CSS multi-column scroll width alone, reducing false blank pages during page turns
- paragraph/list/body text is allowed to cross page breaks so a long paragraph does not leave the lower part of a page empty
- top and bottom reader chrome is now immersive: hidden by default, revealed by edge mouse movement, and auto-hidden after a short delay
- the native title bar is transparent with hidden title text so the app avoids a double top-bar feeling
- static smoke checks still cover horizontal swipe threshold, inertia lock, vertical-wheel suppression, image containment, and chapter-edge bridge

V1.8 is not a full design-system pass. It stabilizes the reading controls that affect long sessions.

## V1.9 Status

V1.9 adds the minimum product-grade Hermes/Cognitive OS handoff boundary:

- Reader API exposes `POST /books/{book_id}/sync/hermes`
- Reader API exposes `GET /books/{book_id}/sync-events`
- sync payloads include book metadata, source sentences, note text, red highlights, locators, sentence indexes, and a cognitive contract
- sync payloads use schema `sentence_reader.hermes_sync.v1`
- sync payloads are written to `~/Library/Application Support/SentenceReader/HermesSync` by default
- every sync handoff writes a pending row to `reader.sync_events`
- Swift App top bar exposes `同步` and calls Reader API instead of calling Hermes directly
- smoke test verifies file generation, payload schema, annotation count, and sync-event persistence

V1.9 is not AI summary generation and not real Hermes ingestion. It is the clean local contract that lets Hermes/Cognitive OS consume reading assets later without coupling the Mac reading UI to model calls.

## V2.0A Status

V2.0A adds the first product-packaging foundation:

- `package_sentence_reader_app.py` now bundles `ReaderRuntime` into `build/Click.app/Contents/Resources/ReaderRuntime`
- bundled runtime includes `reader_api`, `requirements-reader-api.txt`, Reader API startup/migration/status scripts, diagnostics, backup, and restore verification tools
- Swift Reader API startup now checks `ReaderRuntime/scripts/run_reader_api.sh` first and falls back to the development project script path
- `sentence_reader_product_diagnostics.py` writes a JSON report for PostgreSQL, Reader API, app bundle, ReaderRuntime, app-support storage, FunASR paths, and sync-event counts
- `sentence_reader_backup.py` creates a non-destructive backup artifact with `pg_dump --schema=reader` and file inventory
- `sentence_reader_restore_verify.py` validates a backup manifest without restoring or overwriting anything
- `v20_product_packaging_acceptance.sh` gates V1.9, product static contract, package output, diagnostics, backup, and restore verification

V2.0A is not the final installer and not a destructive restore tool. The app still needs a compatible local Python environment for Reader API dependencies. Full restore remains approval-gated because it can overwrite user data.

## V2.0B Status

V2.0B makes the Reader API runtime substantially more self-contained:

- `package_sentence_reader_app.py` now copies `.venv-reader-api` into `ReaderRuntime`
- symlinks are preserved when copying the venv so Xcode Python continues to load its runtime correctly
- `package_sentence_reader_app.py` now copies `migrations/reader/001_reader_schema.sql` into `ReaderRuntime`
- `run_reader_api.sh` prefers `ReaderRuntime/.venv-reader-api/bin/python`
- `reader_runtime_launch_smoke.py` starts `ReaderRuntime/scripts/run_reader_api.sh` from inside the packaged app on a temporary port and verifies `/health`
- product diagnostics now requires package-local runtime Python and core imports when `--require-package` is used

V2.0B is a real improvement over V2.0A: the packaged app no longer merely contains Reader API source code; it contains the verified Python dependency environment and can start the API from the package on this Mac.

V2.0B is still not a universal installer. The bundled venv points to this machine's Xcode Python, and PostgreSQL still depends on the local Postgres.app/tooling. A clean-Mac installer would need a more portable Python/PostgreSQL strategy.

## V2.0C Status

V2.0C closes the first real Hermes/Cognitive OS ingestion loop:

- Reader API exposes `POST /sync/hermes/ingest`
- ingestion can process all pending events or restrict to specific `sync_event_ids`
- source `sentence_reader.hermes_sync.v1` payloads are copied into `hermes_cognitive_os/incoming/sentence_reader`
- each copied asset gets a manifest using schema `sentence_reader.hermes_ingestion_manifest.v1`
- manifest policy states `active_pack_mutation=false` and `requires_human_or_pipeline_review=true`
- successful ingestion marks the original `reader.sync_events` row as `synced`
- missing or invalid payload files mark the event as `failed` and continue the batch
- command-line worker `sentence_reader_hermes_ingest.py` can trigger ingestion through Reader API
- packaged `ReaderRuntime` includes the ingestion worker

V2.0C is not automatic book-model promotion. It safely transfers reader evidence into Hermes Cognitive OS incoming assets. Turning those assets into long-term cognitive models should still go through the existing book intake and quality-gate pipeline.

## V2.0D Status

V2.0D closes the first safe incoming-to-intake-draft loop and has passed acceptance:

- `sentence_reader_intake_draft.py` scans `hermes_cognitive_os/incoming/sentence_reader/*.manifest.json`
- each manifest loads its paired `sentence_reader.hermes_sync.v1` payload
- the script writes reviewable drafts to `hermes_cognitive_os/incoming/sentence_reader_drafts`
- drafts use schema `sentence_reader.book_intake_draft.v1`
- each draft contains a conservative `book_intake_candidate` compatible with the Hermes book intake shape
- each draft includes a quality gate with conclusion: `review_ready`, `needs_review`, or `blocked`
- every draft keeps `promotion_allowed=false` and `active_pack_mutation=false`
- package runtime now includes `sentence_reader_intake_draft.py`
- product diagnostics now reports both incoming reader assets and generated draft assets
- `reader_intake_draft_smoke.py` proves draft generation does not create formal `intakes/*.json`
- `v20d_intake_draft_acceptance.sh` gates V2.0C, draft smoke, and product static contract

V2.0D is deliberately strict. It does not rebuild `compiled_packs/active_cognitive_pack.json`, and it does not write into `hermes_cognitive_os/intakes/`. The next promotion step must be explicit because reader highlights without interpretation are weak evidence.

## V2.0E Status

V2.0E closes the first explicit draft-to-formal-intake promotion loop and has passed acceptance:

- `sentence_reader_promote_intake_draft.py` promotes selected `sentence_reader.book_intake_draft.v1` files into formal Hermes Cognitive OS `intakes/*.json`
- promotion requires the explicit `--approved` flag; without it the worker fails with `missing_approved_flag`
- `review_ready` drafts can be promoted by default
- `needs_review` drafts require the explicit `--allow-needs-review` override
- `blocked` drafts still fail
- existing formal intake files are not overwritten unless `--overwrite` is passed
- active pack rebuild is opt-in through `--rebuild-active-pack`
- Cognitive OS quality gate runs only after an opt-in rebuild, unless `--skip-quality-gate` is explicitly passed
- packaged `ReaderRuntime` includes the promotion worker
- product diagnostics now reports reader drafts, promotion reports, and formal Cognitive OS intakes
- `reader_intake_promotion_smoke.py` proves that unapproved promotion fails and approved promotion writes exactly one formal intake in a temporary Cognitive OS
- production dry run found no real drafts to promote, so no formal production intake was written

V2.0E is still not a final operator UI. It is a safe command boundary. The app can now produce reading assets and the backend can turn reviewed drafts into formal Cognitive OS intakes, but the human review screen/queue is not built yet.

## V2.0F Status

V2.0F adds the first operator review queue and has passed acceptance:

- `sentence_reader_review_queue.py` scans `hermes_cognitive_os/incoming/sentence_reader_drafts/*.draft.json`
- it emits JSON using schema `sentence_reader.intake_review_queue.v1`
- it emits a Markdown review report for human reading
- each draft is classified as `ready_to_approve`, `needs_review`, `blocked`, or `already_promoted`
- ready drafts get suggested approve/rebuild commands that still require `--approved`
- weak drafts surface warnings such as missing notes or placeholder interpretation
- blocked drafts show blocking reasons instead of being silently skipped
- packaged `ReaderRuntime` includes the review queue worker
- product diagnostics now inventories the review queue directory
- `reader_review_queue_smoke.py` proves the queue can separate one ready draft from one weak draft
- production dry run found no real drafts, so the queue report is empty and no promotion happened

V2.0F is not a polished Swift operator UI. It is the safer product boundary before building UI: the system can now tell you what needs review and what command would approve it, without taking action on your behalf.

## V2.0G Status

V2.0G adds the first transaction-style active-pack operator flow and has passed acceptance:

- `sentence_reader_active_pack_operator.py` builds or reads the V2.0F queue and selects explicit drafts, draft ids, or all `ready_to_approve` items
- non-dry-run execution requires `--approved`
- it runs a promotion preflight before writing any formal intake
- it writes rollback artifacts before changing formal intakes or active packs
- it calls the V2.0E promotion worker to write formal `intakes/*.json`
- it calls Hermes Cognitive OS `scripts/build_active_cognitive_pack.py` to rebuild `compiled_packs/active_cognitive_pack.json`
- it runs the Cognitive OS quality gate by default after rebuild when available
- `--skip-quality-gate` exists for smoke tests and controlled exceptional cases
- failure after writes triggers rollback by default for files created or changed by the operator run
- the operator writes `sentence_reader.active_pack_operator_report.v1`
- the rollback manifest uses `sentence_reader.active_pack_operator_rollback.v1`
- packaged `ReaderRuntime` includes the active-pack operator worker
- production dry run found no real drafts and skipped promotion/rebuild cleanly

V2.0G is still not a polished UI. It is the command-level transaction boundary that the future UI should call.

## V2.0H Status

V2.0H adds the first safe in-app/operator surface and has passed its focused smoke checks:

- Reader API exposes `POST /cognitive/review-queue`
- Reader API exposes `POST /cognitive/operator/dry-run`
- both endpoints use the existing V2.0F/V2.0G scripts instead of duplicating operator logic
- queue reports are written under `~/Library/Application Support/SentenceReader/CognitiveOps/review_queue`
- dry-run reports are written under `~/Library/Application Support/SentenceReader/CognitiveOps/operator_dry_runs`
- Swift top bar now has a `认知` button
- the `认知` button checks queue counts, runs a safe dry-run, updates the status line, and can open the Markdown queue report
- static checks now verify the API endpoints and Swift markers
- `reader_api_cognitive_operator_smoke.py` proves an empty temporary Cognitive OS root produces a queue report and a dry-run report without approval or active-pack mutation

V2.0H is not final approval UI. That is deliberate. The correct next UI step is to show draft detail and require explicit human approval before calling the non-dry-run operator.

## V2.0I Status

V2.0I adds the first single-draft review detail and selected preflight boundary:

- Reader API exposes `POST /cognitive/review-item`
- Reader API exposes `POST /cognitive/operator/preflight`
- review item selection supports `draft_path`, `draft_id`, `candidate_intake_id`, or default priority order
- default priority order is `ready_to_approve`, then `needs_review`, then `blocked`
- review-item reports use schema `sentence_reader.cognitive_review_item.v1`
- review-item Markdown includes source evidence, user interpretation, why it matters, proposed model, warnings, blocking reasons, approval policy, and preflight summary
- draft reading is restricted to `hermes_cognitive_os/incoming/sentence_reader_drafts`
- selected preflight runs the existing active-pack operator in dry-run mode
- selected preflight must not write `hermes_cognitive_os/intakes/*.json`
- Swift `认知` now opens the single-draft detail report when a draft exists; otherwise it falls back to the queue report
- `reader_api_cognitive_operator_smoke.py` now creates one ready temporary draft and proves detail/preflight behavior without active-pack mutation

V2.0I still does not add a one-click approval button. The next approval UI must require explicit human intent and should surface rollback/quality-gate consequences before any non-dry-run operator call.

## V2.0J Status

V2.0J adds the first strongly confirmed approval boundary:

- Reader API exposes `POST /cognitive/operator/approve`
- approval is one draft at a time, keyed by `candidate_intake_id`
- approval requires exact confirmation text: `APPROVE <candidate_intake_id>`
- bad confirmation returns an error before any operator command runs
- blocked drafts are rejected
- `needs_review` drafts remain rejected unless `allow_needs_review=true`
- `skip_quality_gate=true` requires `skip_quality_gate_reason`
- approval reruns selected preflight before executing the non-dry-run operator
- approval uses the existing V2.0G active-pack operator with rollback-on-failure
- approval response includes promotion report, active-pack rebuild result, quality-gate result, rollback manifest, and confirmation metadata
- smoke coverage proves a bad confirmation is rejected and a correct confirmation writes exactly one formal intake plus an active pack in a temporary Cognitive OS root

V2.0J is still not a polished approval UI. It is the backend approval contract that a future UI can call. That is intentional: the dangerous write path now exists, so the next UI must make approval intent unmistakable.

## V2.0K Status

V2.0K adds the first App-level approval UX on top of the V2.0J backend contract:

- Swift `认知` flow now detects the current review item status and candidate intake id
- `批准入库...` is only shown when the selected detail item is `ready_to_approve`
- clicking approval opens a second confirmation dialog
- the user must type the exact phrase `APPROVE <candidate_intake_id>`
- if the phrase does not match, the App refuses to call the approval endpoint
- approval calls `POST /cognitive/operator/approve`
- success opens the operator report when available
- failure opens the detail report to keep the user anchored in the evidence
- product static smoke checks for `cognitiveApprove`, `promptCognitiveApproval`, `approveCognitiveDraft`, `APPROVE`, and `确认短语不匹配`

V2.0K is intentionally a small UX surface. It is not a full review dashboard, but it turns the dangerous write path into a deliberate two-step action inside the App.

## V2.0L Status

V2.0L adds the first Cognitive OS operator dashboard on top of the V2.0K approval UX:

- Reader API exposes `GET /cognitive/dashboard`
- Reader API exposes `POST /cognitive/dashboard`
- dashboard reports use schema `sentence_reader.cognitive_dashboard.v1`
- dashboard JSON and Markdown are written under `~/Library/Application Support/SentenceReader/CognitiveOps/dashboard`
- the dashboard reruns the review queue and groups drafts by `ready_to_approve`, `needs_review`, `blocked`, and `already_promoted`
- the dashboard includes safety policy text for exact approval confirmation, dry-run preflight, rollback visibility, and quality-gate visibility
- the dashboard scans operator reports for the same Cognitive OS root and shows approval history
- approval history summarizes approved status, dry-run status, selected count, active-pack rebuild result, quality-gate result, and rollback manifest path
- Swift `认知` now calls the dashboard endpoint and offers `打开仪表盘`
- the App still keeps `批准入库...` behind the V2.0K exact typed confirmation flow
- `reader_api_cognitive_operator_smoke.py` now proves the dashboard is affected by a ready draft and later shows a successful approval history
- `v20l_cognitive_dashboard_acceptance.sh` gates V2.0K, dashboard smoke, Swift compile, Reader API static checks, and product static checks

V2.0L is still a report-first dashboard, not a native table view with filters inside the Swift window. That is the correct boundary for this round: approval is now visible and traceable, but active-pack mutation is still deliberate.

## V2.0M Status

V2.0M turns the V2.0L report-first dashboard into a native App review surface:

- Swift adds `CognitiveDashboardWindowController`
- dashboard draft rows are parsed into `CognitiveDashboardDraftRow`
- approval history rows are parsed into `CognitiveDashboardHistoryRow`
- the top-bar `认知` flow now opens a native `认知审核台` when the dashboard schema is valid
- the native dashboard shows queue counts from `sentence_reader.cognitive_dashboard.v1`
- the native table lists status, book/candidate, quality, model, and warning/blocking issue summary
- status filtering supports `全部`, `可批准`, `待审`, `阻塞`, and `已入库`
- the lower history panel shows recent approval/operator history with rebuild, quality, skipped, and rollback fields
- the window can open the generated Markdown dashboard and selected draft source
- the native dashboard intentionally does not include an approval button; approval remains behind the V2.0K typed-confirm flow
- `sentence_reader_native_cognitive_dashboard_smoke.py` locks the Swift markers
- `v20m_native_cognitive_dashboard_acceptance.sh` gates V2.0L, native dashboard static smoke, Swift compile, product static smoke, and dashboard API smoke

V2.0M is a product usability improvement, not a new mutation path. That is the right design: review is easy, approval is still deliberate.

## V2.0N Status

V2.0N adds a hard runtime portability contract:

- `sentence_reader_runtime_portability.py` writes `sentence_reader.runtime_portability_report.v1`
- the report separates `current_machine_ready` from `clean_mac_ready`
- the report checks packaged ReaderRuntime files, bundled virtualenv, Python dependency imports, PostgreSQL strategy, runtime manifest, and feature portability risks
- `package_sentence_reader_app.py` now writes `ReaderRuntime/runtime_manifest.json` with schema `sentence_reader.runtime_manifest.v1`
- `package_sentence_reader_app.py` now copies `sentence_reader_runtime_portability.py` into ReaderRuntime
- `sentence_reader_product_diagnostics.py` now runs the portability report when `--require-package` is used
- `sentence_reader_product_static_smoke.py` now locks portability script, diagnostics, and package manifest markers
- `docs/runtime_portability.md` records the current strategy and known clean-Mac blockers
- `v20n_runtime_portability_acceptance.sh` gates V2.0M, package creation, portability report, runtime launch smoke, product diagnostics, and product static checks

The current report is deliberately not clean-Mac green:

- `current_machine_ready=true`
- `clean_mac_ready=false`
- blockers: `runtime_python_points_to_xcode`, `runtime_python_points_outside_ReaderRuntime`, `postgres_not_bundled`

This is stronger than the previous state because the risk is now machine-checkable. It is not the final installer.

## V2.0O Status

V2.0O adds a real startup bootstrap/preflight path:

- `sentence_reader_runtime_bootstrap.py` writes `sentence_reader.runtime_bootstrap_report.v1`
- it checks candidate Python runtimes: explicit `READER_API_PYTHON`, user app-support venv, bundled runtime venv, and system `python3`
- it selects the first Python that can import FastAPI, Uvicorn, Psycopg, and HTTPX
- it can create a user-level venv under `~/Library/Application Support/SentenceReader/Runtime/.venv-reader-api` when called with `--repair-python`
- dependency installation requires the explicit `--install-deps` flag
- PostgreSQL remains an explicit preflight: existing server or available Postgres.app tools are accepted
- `run_reader_api.sh` now calls the bootstrap selector if no bundled/runtime Python is executable
- automatic dependency installation is disabled by default
- automatic PostgreSQL installation is disabled by default
- product diagnostics now includes runtime bootstrap status
- `sentence_reader_runtime_bootstrap_smoke.py` proves user-level venv creation and Python selection without installing dependencies
- `v20o_runtime_bootstrap_acceptance.sh` gates V2.0N, package creation, bootstrap smoke, bootstrap report, runtime launch smoke, product diagnostics, and product static checks

V2.0O still does not make the app a universal installer. It turns a previously hidden startup failure into a recoverable, explicit bootstrap flow.

## V2.0P Status

V2.0P makes first-run readiness explicit:

- `sentence_reader_runtime_config.py` writes and reads `sentence_reader.runtime_config.v1`
- FunASR paths can be configured by `SENTENCE_READER_RUNTIME_CONFIG`, `SENTENCE_READER_FUNASR_PYTHON`, `SENTENCE_READER_FUNASR_WORKER`, or the app-support runtime config file
- Swift note transcription no longer depends only on a hard-coded user project path
- Swift still keeps the legacy FunASR path as a compatibility fallback
- `sentence_reader_first_run_preflight.py` writes `sentence_reader.first_run_preflight_report.v1`
- first-run preflight reports PostgreSQL readiness, Reader API startup/migration boundary, runtime bootstrap status, runtime config path, and FunASR readiness/fallback
- product diagnostics now fails package acceptance if first-run preflight cannot run
- `sentence_reader_first_run_preflight_smoke.py` verifies runtime config + preflight behavior without real transcription or paid services
- `package_sentence_reader_app.py` bundles the runtime config and first-run preflight scripts into ReaderRuntime
- `v20p_first_run_preflight_acceptance.sh` gates V2.0O, package creation, runtime config smoke, first-run preflight smoke/report with `first_run_ready=true`, product diagnostics, product static smoke, and Swift compile

V2.0P still does not install PostgreSQL or FunASR automatically. That is intentional: install/repair remains explicit, and missing FunASR falls back to Apple Speech so reading and manual notes are not blocked.

## V2.0Q Status

V2.0Q moves first-run visibility into the App:

- Swift adds `RuntimeEnvironmentWindowController`
- the top bar now includes `环境`
- `环境` opens a native runtime environment window
- the window runs `sentence_reader_first_run_preflight.py` with `--require-first-run-ready`
- App-run reports are written under `~/Library/Application Support/SentenceReader/Diagnostics`
- the window shows PostgreSQL, Reader API, runtime bootstrap, runtime config, and FunASR status
- the window can open the generated Markdown/JSON preflight report
- FunASR Python and worker paths can be saved from the App
- saved paths are written both to `UserDefaults` and to `~/Library/Application Support/SentenceReader/config/runtime_config.json`
- `sentence_reader_runtime_settings_static_smoke.py` locks the App-level runtime settings markers
- `v20q_runtime_settings_acceptance.sh` gates V2.0P, runtime settings static smoke, package creation, first-run preflight, product static smoke, and Swift compile

V2.0Q still does not run a destructive installer. It makes runtime health visible and configurable inside the app, while keeping PostgreSQL/bootstrap repair explicit.

## V2.0R Status

V2.0R makes first-run failure actionable without becoming an installer:

- App launch now runs a background first-run preflight
- the runtime environment window appears automatically only when `first_run_ready=false`
- normal ready state does not interrupt reading
- runtime environment window includes a `首启修复引导` section
- the guide explains PostgreSQL recovery, Runtime Python/bootstrap recovery, and FunASR recovery
- guide text preserves the explicit policy: no hidden dependency install, no automatic PostgreSQL install, no destructive database operation
- users can copy the guide via `复制修复指引`
- users can open the runtime config folder via `打开配置目录`
- `sentence_reader_first_run_guide_static_smoke.py` locks the Swift guide markers
- `v20r_first_run_guide_acceptance.sh` gates V2.0Q, first-run guide static smoke, product static smoke, package creation, and Swift compile

V2.0R still does not make a clean-Mac universal installer. It makes the failure path understandable inside the App.

## V2.0S Status

V2.0S adds product identity and Dock-friendly access:

- `generate_sentence_reader_icon.py` generates a reading-themed icon with an open book and red bookmark
- generated icon files live under `assets/SentenceReader.iconset`
- generated app icon is `assets/SentenceReader.icns`
- `package_sentence_reader_app.py` copies the icon into `Click.app/Contents/Resources`
- `Info.plist` now sets `CFBundleIconFile` to `SentenceReader`
- packaged output prints `app_icon=True`
- `pin_sentence_reader_to_dock.py` adds the packaged app to the Dock with duplicate detection
- `sentence_reader_identity_static_smoke.py` verifies icon, Info.plist, package integration, and Dock script markers
- `v20s_product_identity_acceptance.sh` gates V2.0R, icon generation, package creation, Dock dry-run, identity smoke, and product static smoke
- the current packaged app was added to the macOS Dock

V2.0S still pins the project build app path, not a signed `/Applications` installer. That is good enough for this local product stage, but not final distribution.

## Next Required Step

## V2.0T Status

V2.0T adds the missing product main surface:

- the top bar now has a native `书库` entry
- `书库` opens a real library management window instead of only a quick switch menu
- the library window lists bundled and imported books from `SentenceReader.bookLibrary.v1`
- imported EPUBs are shown as owned internal library copies
- actions include `导入 EPUB`, `打开所选`, `显示内部副本`, and `从书库移除`
- `从书库移除` is deliberately non-destructive: it removes only the library index entry and does not delete internal EPUB copies, reading positions, highlights, notes, audio notes, or PostgreSQL data
- the old `书籍` quick switch remains as a fast menu, but it is no longer the only book-management surface
- `check_native_reader.py` and product static smoke now lock the library window markers

This fixes a real product gap. Before V2.0T the app had a reading window plus a switcher, but not a main/library management interface. Now it has a functional book management surface. It is still not a polished visual bookshelf.

## Library UI Productization Status

The simplified native book table is no longer the primary product surface. Library V2 is the primary product home.

Current state:

- Reader API exposes `GET /library` as a reading-first product home.
- Reader API exposes `GET /api/library/dashboard` with `ui_version=library_v2`, books, current/recent reading, progress, reading state, cover URL, note counts, red highlight counts, audio note counts, recent notes/red highlights, file status, and internal-copy status.
- Reader API exposes `GET /api/library/books/{book_id}/cover` for real EPUB cover extraction with generated fallback covers.
- Reader API exposes `POST /api/library/import` for EPUB import into the Mac-owned internal library.
- Reader API exposes `POST /api/library/books/{book_id}/hide` for non-destructive list removal.
- Reader API exposes `POST /api/library/books/{book_id}/reveal` for showing the EPUB copy in Finder on the Mac.
- PostgreSQL has `reader.library_state` for UI visibility state only.
- Swift opens the main window directly to `http://127.0.0.1:18180/library?surface=mac-app` when Reader API is available.
- Swift `书库` returns to the same main-window library surface instead of opening a separate window.
- Clicking a book cover/card in the Mac App opens the original native sentence reader through `sentence-reader://open-native?book_id=...`; it does not route the Mac user into `/lan/reader`.
- The home page has a large `继续阅读` area, recent-reading rail, recent notes/red highlights, a cover-wall library, dedicated notes center, dedicated red-highlight center, details drawer, and basic batch hide/export.
- The reader chrome uses larger grouped controls: `书库`, `目录`, `笔记`, `设置`, and `更多`.
- The old AppKit book table remains only as a fallback if Reader API cannot start.
- iPad entry now gives the `/library` URL first and preserves `/lan/reader` as a direct reading URL.
- `sentence_reader_library_ui_static_smoke.py` and `sentence_reader_library_v2_smoke.py` lock the page, API, cover endpoint, Mac native-open contract, notes/red centers, batch actions, Swift entry, migration, and non-destructive hide contract.

No external reader system is embedded. The App has one product direction: local durable reading data + sentence-level native reader + Library V2 product shell.

## Vocabulary Lookup Repair Status

English lookup is no longer only a UI gesture. The backend now has a real fallback path:

- single-click/single-tap English lookup first checks book-local vocabulary
- if the current book has no generated vocabulary rows, Reader API falls back to `reader.dictionary_entries`
- the fallback creates a dictionary-backed `reader.book_vocab_items` row so repeated lookup is stable
- simple morphology is covered for common forms like plural `strategies`
- a compact general/business seed dictionary is installed by `005_general_dictionary_seed.sql`
- the full ECDICT-compatible CSV has been downloaded to `data/external/ecdict/ecdict.csv`
- `sentence_reader_import_ecdict.py` imported 768,739 dictionary rows into PostgreSQL with source `ecdict`
- lookup quality is now backed by a real local dictionary, not just a tiny seed list

The Readium publication-open probe also no longer depends on a specific Desktop EPUB. The project now generates and packages `fixtures/sentence-reader-smoke.epub` as the stable test fixture, so the original source book can be moved or deleted without breaking validation.

## Life-study Context Vocabulary Pipeline Status

The Life-study bilingual vocabulary path has moved from probe to an accuracy-gated V1 pipeline.

Current implemented files:

- `scripts/lifestudy_context_vocab_pipeline.py`
- `scripts/lifestudy_context_vocab_pipeline_smoke.py`
- `scripts/lifestudy_context_vocab_import.py`
- `scripts/lifestudy_context_vocab_import_smoke.py`
- `scripts/lifestudy_context_vocab_domain_lookup_smoke.py`
- `scripts/lifestudy_context_vocab_book_lookup_smoke.py`
- `scripts/lifestudy_context_vocab_review_pack.py`
- `scripts/lifestudy_context_vocab_review_pack_smoke.py`
- `scripts/lifestudy_context_vocab_frontend_smoke.py`
- `docs/lifestudy_vocab_pipeline_plan.md`

Current source:

- The Life-study bilingual PDF corpus under the 中英对照 directory.
- Genesis is the only reviewed/applied production volume.
- Exodus and Leviticus are completed no-write candidate sources only.

Genesis first 50 pages passed the quality gate:

- line pairs: 2,264
- aligned text units: 788
- candidates: 8,153
- A/B importable candidates: 22
- A/B rule-gated precision estimate: 0.95
- top-100 noise/discard count: 0
- database writes: false

Genesis full run also completed:

- pages: 1,255
- line pairs: 57,002
- aligned text units: 20,481
- candidates: 112,642
- A/B importable candidates: 25
- quality grades: A=19, B=6, C=81,361, D=31,256
- A/B rule-gated precision estimate: 0.95
- top-100 noise/discard count: 0
- database writes: false

Controlled import after Genesis gate:

- domain staging: 25 active Genesis entries in `reader.domain_glossary_entries`, A=19, B=6
- imported target book: `book_e0679064039e4e298e9faf3127b65876`
- imported target title: `创世记生命读经 / Life-study of Genesis`
- imported source EPUB: `1-01创世记生命读经.epub`
- book-specific rows: 25 `reader.book_glossary`, 25 `reader.book_vocab_items`, 25 `reader.lexemes`
- C/D imported: 0
- general dictionary pollution: 0
- smoke lookup: `tree of life` returns `生命树` from `book_glossary`
- review pack outputs: `reports/lifestudy_vocab_review/Genesis-review-pack.json`, `.csv`, `.md`, and `Genesis-review-overrides.template.json`
- human review pending: 0 for Genesis A/B entries
- can expand next volume: true for controlled no-write probes

Generated reports:

- `reports/lifestudy_vocab_pipeline/01_Genesis-120-pages-1-50-pipeline.json`
- `reports/lifestudy_vocab_pipeline/01_Genesis-120-pages-1-50-importable.json`
- `reports/lifestudy_vocab_pipeline/01_Genesis-120-pages-1-1255-pipeline.json`
- `reports/lifestudy_vocab_pipeline/01_Genesis-120-pages-1-1255-importable.json`
- `reports/lifestudy_vocab_pipeline/02_Exodus-185-pages-1-1579-importable.json`
- `reports/lifestudy_vocab_pipeline/03_Leviticus-64-pages-1-462-importable.json`
- `reports/lifestudy_vocab_corpus/lifestudy_corpus_inventory.json`
- `reports/lifestudy_vocab_corpus/lifestudy_master_vocab.json`
- `reports/lifestudy_vocab_corpus/lifestudy_master_vocab.csv`
- `reports/lifestudy_vocab_corpus/lifestudy_all_words_master.json`
- `reports/lifestudy_vocab_corpus/lifestudy_all_words_master.csv`
- `reports/lifestudy_vocab_corpus/lifestudy_clean_all_words_master.json`
- `reports/lifestudy_vocab_corpus/lifestudy_vocab_top500_queue.json`
- `reports/lifestudy_vocab_corpus/lifestudy_vocab_top500_queue.csv`
- `reports/lifestudy_vocab_corpus/lifestudy_vocab_top2000_queue.json`
- `reports/lifestudy_vocab_corpus/lifestudy_vocab_v1_review_pack.json`
- `reports/lifestudy_vocab_corpus/lifestudy_vocab_v1_importable.json`
- `reports/lifestudy_vocab_corpus/lifestudy_vocab_v1_importable.csv`

The pipeline uses OpenCC `t2s`, filters obvious headers/noise, emits A/B/C/D grades, and keeps C/D out of importable reports. It also blocks OCR-damaged phrases such as `light darkness`.

Controlled import is now applied for Genesis only. The safe boundary remains: future runs must dry-run first, must not overwrite `source='user'` glossary rows, and must not import C/D candidates or write Life-study meanings into `reader.dictionary_entries`.

Frontend lookup is now phrase-safe: selected English phrases such as `tree of life` are preserved by the Mac reader and matched by Reader API without collapsing them into `treeoflife`. Single-word lookup and dictionary fallback remain in place.

## Next Required Step

After library UI productization, decide:

- manually test Library V2 with 10+ real EPUBs and unusual cover metadata
- whether to install/copy a stable app build into `/Applications` or keep using the project build path
- add a small release packaging note so future rebuilds do not confuse Dock aliases
- keep first-run repair explicit and avoid automatic destructive setup
- keep destructive restore and active-pack mutation behind explicit user approval
