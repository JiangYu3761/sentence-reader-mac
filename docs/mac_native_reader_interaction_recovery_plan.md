# Click Mac Native Reader Interaction Recovery Plan

Updated: 2026-07-05

Status: Superseded. Keep only as a warning record.

## Decision

Do not use the old recovery direction.

The old direction tried to make active正文 selection + two-finger tap perform selected-text red highlight, while also preserving copy behavior. In real use this destabilized the Mac native reader's core interactions, including double-click note.

The project has rolled back to the stable baseline:

- single-click English word -> lookup;
- double-click sentence -> existing sentence note panel;
- two-finger tap / secondary click sentence -> whole-sentence red highlight.

The only acceptable future selected-text design is documented in:

```text
docs/mac_native_reader_selection_action_bar_plan.md
```

That plan adds one feature only:

```text
After selecting正文 text, show a compact action bar:

复制 | 标红 | 备注
```

## Forbidden From This Superseded Plan

- Do not implement selected-text red through active selection + two-finger tap.
- Do not make `Command+C` a Click-owned reading-surface command.
- Do not rewrite the no-selection interaction router.
- Do not change double-click sentence note.
- Do not change two-finger whole-sentence red.
- Do not change single-click English lookup.
- Do not change Reader API schema.
- Do not change PostgreSQL schema.

## Why This File Remains

This file remains only to prevent the old failed plan from being revived. The active plan is the additive selection action bar plan, not this recovery plan.
