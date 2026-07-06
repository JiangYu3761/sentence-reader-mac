# Click Mac Native Reader Selection Action Bar Plan

Updated: 2026-07-05

Status: Implemented as an additive Mac native reader feature on top of the rollback baseline.

## Current Decision

The Mac native reader keeps the stable no-selection reading baseline and adds one selected-text action bar.

Current baseline to preserve:

- Single-click English word -> lookup.
- Double-click sentence -> existing sentence note panel.
- Two-finger tap / secondary click sentence -> whole-sentence red highlight toggle.
- Existing note UI, red-highlight save path, Reader API schema, and PostgreSQL schema remain unchanged.

The failed direction is not allowed back in:

- Do not use active text selection + two-finger tap as the selected-text red path.
- Do not add `Command+C` as a Click-owned reading-surface command.
- Do not let copy support alter double-click note, two-finger whole-sentence red, or lookup.
- Do not add a second note editor UI.

## Why The Previous Attempt Failed

The previous repair mixed too many responsibilities:

- native secondary-click ownership;
- WebKit text selection;
- selected-fragment red highlight;
- copy behavior;
- double-click note timing.

Once native or JS tried to own both selection and secondary-click, normal reading gestures began to collapse. The visible failure was severe: double-click note stopped being reliable. Therefore the next implementation must not modify the stable no-selection gesture pipeline.

## New Scope

The implemented feature is:

```text
After the user deliberately creates a正文 selection by dragging, show a compact floating action bar:

复制 | 标红 | 备注
```

This must be additive. It must not rewrite the existing no-selection interaction router.

## Interaction Contract

### No Active 正文 Selection

This path is the stable baseline and must not be changed.

| Action | Result |
| --- | --- |
| Single-click English word | Lookup word |
| Double-click sentence | Open the existing sentence note panel for the whole sentence |
| Two-finger tap sentence | Toggle whole-sentence red highlight |

### Active 正文 Selection

This is the only new feature to add.

| Action | Result |
| --- | --- |
| Drag-select word / phrase / half sentence / multi-line text | Show selection action bar |
| Click `复制` | Copy exactly selected text |
| Click `标红` | Red-highlight exactly selected text fragments |
| Drag-select exact existing selected-text red fragments | Red button changes to `取消标红` |
| Click `取消标红` | Remove exactly matched selected-text red fragments |
| Click `备注` | Open the existing note panel for exactly selected text |
| `Command+Z` after `标红` | Undo the last selected-fragment red highlight and remove its persisted annotation |
| `Command+Z` after `取消标红` | Restore the removed selected-fragment red highlight |

### Forbidden

- Active selection + two-finger tap -> selected-fragment red.
- Reading-surface `Command+C` -> Click-owned copy command.
- Any native event interception that can block double-click note.
- Any change that routes no-selection double-click through the new selection layer.

## Implementation Boundaries

### JS Added

- Drag-gated正文 selection detection. `selectionchange` may hide the bar when the selection collapses, but it must not open the bar by itself.
- Mapping selected ranges to `.sr-sentence` plain-text offsets.
- A small non-layout floating action bar.
- Three action handlers: copy, red, note.

### JS Must Not Touch

- Existing `document.addEventListener('dblclick', ...)` whole-sentence note path except to explicitly ignore action-bar buttons.
- Existing no-selection secondary-click whole-sentence red path.
- Existing single-click lookup delay/cancel path.

### Native Added

- A native pasteboard route for the action-bar `复制` button if needed.
- A selection-note save wrapper that reuses the existing note panel UI.

### Native Must Not Touch

- No-selection double-click sentence note behavior.
- No-selection two-finger whole-sentence red behavior.
- Reader API schema.
- PostgreSQL schema.

## Selection Action Bar Requirements

- One row only: `复制`, `标红`, `备注`.
- Appears only after a deliberate正文 drag selection. Double-click selection, secondary-click/right-click selection, lookup selection, keyboard selection, or generic `selectionchange` must not open it.
- Does not appear in note editor, lookup card, correction editor, settings, buttons, inputs, or toolbars.
- Does not reserve layout space.
- Hides after action completion, page turn, chapter change, reader reload, Esc, or collapsed selection.
- Button `mousedown` must prevent selection from collapsing before the button handler reads fragments.

## Selection Fragment Requirements

Each selected fragment must include:

- `sentenceIndex`
- `startOffset`
- `endOffset`
- selected text snapshot

Selections may be:

- one word;
- a phrase;
- part of one sentence;
- several lines;
- several sentences.

Selections must not be expanded to whole sentence unless the user actually selected the whole sentence.

## Red Highlight Storage

Use existing `reader.annotations`.

Use existing `red_highlight` kind.

Use `range_locator.mode = text_selection`.

No Reader API schema change.

No PostgreSQL schema change.

Whole-sentence red and fragment red must not overwrite each other.

`Command+Z` must undo both whole-sentence red and selected-fragment red. For selected-fragment red, the WebView restores the previous fragment paint set and posts a `selectionRedUndo` message so the native layer deletes the matching `range_locator.mode = text_selection` `red_highlight` annotation from the existing Reader API path.

## Note Storage

Use existing `reader.annotations`.

Use existing `note` kind.

Reuse the existing note panel UI.

Use `range_locator.mode = text_selection_note`.

Sentence note and selection note must not overwrite each other.

## Smoke Tests

Tests must prove:

- Baseline double-click sentence note still exists.
- Baseline two-finger whole-sentence red still exists.
- Baseline single-click English lookup still exists.
- Selection action bar exists.
- Action bar has exactly `复制`, `标红`, `备注`.
- Action bar changes `标红` to `取消标红` only when the drag selection exactly matches existing selected-text red fragments.
- Action bar copy does not depend on `Command+C`.
- Action bar red uses `range_locator.mode = text_selection`.
- Action bar red participates in `Command+Z` undo and deletes the matching persisted `text_selection` red annotation.
- Action bar note uses `range_locator.mode = text_selection_note`.
- No selected-text secondary-click red route exists.
- Reader API schema and PostgreSQL schema are unchanged.

## Manual Acceptance

1. No selection, double-click a sentence: original note panel opens.
2. No selection, two-finger tap a sentence: whole sentence red toggles.
3. No selection, single-click an English word: lookup opens.
4. Drag-select one word: action bar appears.
5. Drag-select half a sentence: action bar appears.
6. Drag-select multi-line text: action bar appears.
7. Double-click a word/sentence: action bar does not appear.
8. Two-finger tap or right-click a sentence: action bar does not appear.
9. `复制` copies only the selected text.
10. `标红` marks only the selected text.
11. `备注` opens the existing note panel for selected text.
12. Sentence note and selection note remain independent.

## Non-Goals

- Do not implement this by fighting macOS/WebKit secondary-click selection behavior.
- Do not add `Command+C` as a Click reading command.
- Do not replace the reader engine.
- Do not route Mac native reader through LAN reader.
- Do not add a new annotation table.
- Do not change Reader API schema.
- Do not change PostgreSQL schema.
