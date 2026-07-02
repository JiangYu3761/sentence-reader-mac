# Life-study Vocabulary Final Review Plan

This document fixes the working method for finishing the Life-study vocabulary review without doing a rough 4,000-row bulk import and without spending excessive time to approve only a tiny handful of words.

## Current Scope

The current remaining review pool is:

- Source file: `reports/lifestudy_vocab_corpus/lifestudy_dictionary_guided_review_v2_possible_frontend_after_human_review.csv`
- Candidate count: 4,102 rows
- Final write target, after all review is complete: `reader.domain_glossary_entries`
- Current policy: the full review pass is complete, and the corrected import stance is that all 4,102 reviewed rows are vocabulary database assets for single-click word popup. High-confidence priority is a display/ranking flag, not the popup boundary.
- Safety boundary: no database import is allowed until the user explicitly confirms final import.

The previous 2,205-row needs-review pass is not being restarted. It has already served as the calibration set for the current rules: every usable row should support single-click lookup popup, high-confidence rows can be prioritized, and unsupported/noisy rows must be repaired before use.

## Full Review Completion Status

The 4,102-row final review has now been completed as a no-write review pass.

- Calibration 001: completed and reused as the 100-row decision sample.
- Production batch 001: completed and validated.
- Production batches 002 through 017: completed by the automatic batch loop.
- Production batch count: 17.
- Production batch size: 250 rows, except batch 017 with the final 2 rows.
- Reviewed source rows: 4,102.
- Missing source rows: 0.
- Duplicate source rows: 0.
- Duplicate reviewed words: 0.
- Database writes during review: 0.
- Import-ready rows during review: 0.

Final category counts:

| Category | Count |
| --- | ---: |
| `frontend_ready` | 26 |
| `learning_only` | 3,975 |
| `needs_more_evidence` | 96 |
| `reject` | 5 |

Final review outputs:

- all reviewed rows: `reports/lifestudy_vocab_final_review/final_review_all_reviewed.csv`
- front-end candidates: `reports/lifestudy_vocab_final_review/final_review_frontend_ready.csv`
- learning-only rows: `reports/lifestudy_vocab_final_review/final_review_learning_only.csv`
- needs-more-evidence rows: `reports/lifestudy_vocab_final_review/final_review_needs_more_evidence.csv`
- rejected rows: `reports/lifestudy_vocab_final_review/final_review_reject.csv`
- machine summary: `reports/lifestudy_vocab_final_review/final_review_summary.json`
- human summary: `reports/lifestudy_vocab_final_review/final_review_summary.md`

This is still not a database import package. The final import requires a separate user-confirmed final spot check and import step.

## Corrected Full Database Import Stance

The previous productized layer names were easy to misread. The correct product stance is:

- all 4,102 rows should be prepared for the Life-study vocabulary database and single-click reader popup after repair;
- `can_default_popup=true` means the user single-clicks an English word and sees the Life-study meaning;
- `high_confidence_popup=true` is only a priority/ranking flag for the strongest rows, not the popup boundary;
- `learning_vocab` means searchable/reviewable/wordbook vocabulary and still supports single-click popup;
- previous `evidence_queue` rows should not be thrown away. They enter the single-click popup/search layer with a follow-up-review flag;
- previous `reject` rows should be repaired when the aligned evidence supports a clear term meaning, instead of being discarded mechanically.

The current full import-preparation package is:

- importable JSON: `reports/lifestudy_vocab_final_review/final_database_import_prepare_all_4102.json`
- importable CSV: `reports/lifestudy_vocab_final_review/final_database_import_prepare_all_4102.csv`
- single-click popup subset: `reports/lifestudy_vocab_final_review/final_database_import_prepare_default_popup.csv`
- high-confidence popup subset: `reports/lifestudy_vocab_final_review/final_database_import_prepare_high_confidence_popup.csv`
- learning/search subset: `reports/lifestudy_vocab_final_review/final_database_import_prepare_learning_search.csv`
- repaired reject rows: `reports/lifestudy_vocab_final_review/final_database_import_prepare_repaired_from_reject.csv`
- summary: `reports/lifestudy_vocab_final_review/final_database_import_prepare_summary.md`

Current full import-preparation result:

| Import-preparation field | Count |
| --- | ---: |
| database import candidates | 4,102 |
| learning/search enabled | 4,102 |
| single-click popup enabled | 4,102 |
| high-confidence popup priority | 49 |
| needs follow-up review but still import candidate | 52 |
| repaired from previous reject | 5 |
| database writes performed | 0 |

The five repaired rows are:

| Word | Repaired meaning |
| --- | --- |
| `ephesians` | `以弗所书` |
| `philippians` | `腓立比书` |
| `divinity` | `神性` |
| `pharisees` | `法利赛人` |
| `anointed` | `膏` |

Validation:

- `scripts/lifestudy_final_database_import_prepare.py` generates the full package.
- `scripts/lifestudy_final_database_import_prepare_smoke.py` verifies 4,102 import candidates, no duplicate terms, no empty meanings, no database writes, no `reader.dictionary_entries` import candidates, and compatibility with the existing importer's `validate_items` function.
- A dry run through `scripts/lifestudy_context_vocab_import.py --domain-staging` accepted all 4,102 candidates and reported `database_write_performed=false`.

## Full Usability Audit

The practical usability question has a separate audit from the import package. It answers: for each of the 4,102 rows, what can the product safely do with it?

Current usability audit outputs:

- all rows: `reports/lifestudy_vocab_final_review/final_usability_audit_all_4102.csv`
- single-click popup subset: `reports/lifestudy_vocab_final_review/final_usability_audit_default_popup.csv`
- high-confidence popup subset: `reports/lifestudy_vocab_final_review/final_usability_audit_high_confidence_popup.csv`
- explicit click-lookup subset: `reports/lifestudy_vocab_final_review/final_usability_audit_click_lookup.csv`
- learning/search/wordbook subset: `reports/lifestudy_vocab_final_review/final_usability_audit_learning_search.csv`
- needs-fix subset: `reports/lifestudy_vocab_final_review/final_usability_audit_needs_fix.csv`
- summary: `reports/lifestudy_vocab_final_review/final_usability_audit_summary.md`

Current usability result:

| Usability surface | Count |
| --- | ---: |
| usable in database | 4,102 |
| usable for single-click popup | 4,102 |
| usable for explicit click lookup | 4,102 |
| usable for learning/search/wordbook | 4,102 |
| high-confidence popup priority | 49 |
| popup-enabled but needs follow-up sense review | 52 |
| needs fix before use | 0 |

This means the 4,102 rows are not being reduced to 49. All 4,102 rows support the reader's intended single-click English-word popup. The 49 count only marks the strongest high-confidence subset for future ranking/priority decisions.

Validation:

- `scripts/lifestudy_vocab_usability_audit_all.py` generates the per-word 4,102-row usability table.
- `scripts/lifestudy_vocab_usability_audit_all_smoke.py` verifies all 4,102 rows have a usable product surface, no missing meaning/evidence, no needs-fix rows, no database writes, and no import-ready flags.

## Meaning Source Rule

Chinese meanings must come from the aligned Chinese Life-study evidence, not from a general dictionary.

The dictionary-guided file may suggest candidates, but a term can only be approved when the same evidence record supports both sides:

- the English term appears in `evidence_en`
- the proposed Chinese meaning appears in `evidence_zh_simp`
- the pair is meaningful in the Life-study context

If the aligned Chinese sentence does not support the meaning, the row must be repaired before it enters the single-click popup import package.

## Review Categories

Every row must end in exactly one category:

| Category | Meaning |
| --- | --- |
| `frontend_ready` | Can enter the final Life-study front-end glossary package. |
| `learning_only` | Useful as a learning word, but should not pop up in the reader. |
| `needs_more_evidence` | Potentially useful, but the current evidence is not strong enough. |
| `reject` | Noise, wrong alignment, wrong meaning, OCR issue, or not useful. |

## Batch Size

The 4,102 rows should be reviewed in controlled batches of 250 rows.

This gives roughly 17 batches:

- large enough to make visible progress
- small enough to catch rule drift
- small enough to redo if a batch fails quality checks

Do not review all 4,102 in one silent pass. Do not review only 15 words per work session unless debugging a broken rule.

## Batch Workflow

Each 250-row batch follows the same process.

| Step | Action | Output |
| --- | --- | --- |
| 1. Build batch | Take the next 250 unreviewed rows from the 4,102 pool. | `batch_N_input.csv` |
| 2. Evidence pass | Check same-record English and Chinese evidence for every row. | provisional decision per row |
| 3. Front-end strict pass | Recheck every `frontend_ready` row one more time. | corrected front-end candidates |
| 4. Learning/reject sample | Spot-check at least 30 non-front-end rows, biased toward high-frequency and ambiguous terms. | sample result |
| 5. Batch report | Write counts, examples, and failed-rule notes. | `batch_N_review.md` |
| 6. Append reviewed rows | Add approved decisions to the cumulative final review file. | updated final review CSV |

## Calibration Gate

Before reviewing normal batches, run one calibration batch of 100 rows:

- include high-frequency terms, ambiguous terms, obvious learning-only terms, and likely front-end terms
- compare decisions against the 2,205-row needs-review pass and the existing 100 pending formal terms
- tune the prompt/rules until the output matches the established policy

Do not start the 250-row production batches until the calibration batch passes.

Current calibration tooling:

- generator: `scripts/lifestudy_final_review_calibration.py`
- smoke: `scripts/lifestudy_final_review_calibration_smoke.py`
- completed-review generator: `scripts/lifestudy_final_review_calibration_adjudicate.py`
- completed-review smoke: `scripts/lifestudy_final_review_calibration_reviewed_smoke.py`
- output directory: `reports/lifestudy_vocab_final_review/`
- calibration input: `reports/lifestudy_vocab_final_review/calibration_001_input.csv`
- review template: `reports/lifestudy_vocab_final_review/calibration_001_review_template.csv`
- summary: `reports/lifestudy_vocab_final_review/calibration_001_summary.json`
- reviewed output: `reports/lifestudy_vocab_final_review/calibration_001_reviewed.csv`
- reviewed front-end rows: `reports/lifestudy_vocab_final_review/calibration_001_frontend_ready.csv`
- reviewed learning-only rows: `reports/lifestudy_vocab_final_review/calibration_001_learning_only.csv`
- reviewed needs-more-evidence rows: `reports/lifestudy_vocab_final_review/calibration_001_needs_more_evidence.csv`

The calibration batch passes only when:

- every approved `frontend_ready` row is supported by same-record Chinese evidence
- no generic common word is promoted to `frontend_ready`
- at least 90% of sampled decisions match the established policy
- any disagreement is explained and either accepted as a rule improvement or fixed before production review
- historical corrections from the existing 100 pending formal terms are anchored, so rows such as `righteousness -> 公义`, `redemption -> 救赎`, `reality -> 实际`, and `priesthood -> 祭司职分` are not overwritten by older candidate meanings

Calibration 001 completion rule:

- all 100 rows must have a final category
- the front-end-ready rows must come from known pending formal anchors
- ambiguous but potentially useful terms such as `age`, `cross`, `flesh`, `nourishment`, and `power` stay in `needs_more_evidence`
- no calibration row is marked final import-ready
- no calibration row writes PostgreSQL

Only after the completed-review smoke passes can the workflow be copied into the 250-row production batch generator.

## Production Batch 001

Batch 001 copies the completed calibration workflow into the first 250-row production batch.

Current batch tooling:

- generator/reviewer: `scripts/lifestudy_final_review_batch.py`
- batch 001 smoke: `scripts/lifestudy_final_review_batch_001_smoke.py`
- generic batch smoke: `scripts/lifestudy_final_review_batch_smoke.py`
- batch input: `reports/lifestudy_vocab_final_review/batch_001_input.csv`
- batch review template: `reports/lifestudy_vocab_final_review/batch_001_review_template.csv`
- batch reviewed output: `reports/lifestudy_vocab_final_review/batch_001_reviewed.csv`
- batch front-end rows: `reports/lifestudy_vocab_final_review/batch_001_frontend_ready.csv`
- batch learning-only rows: `reports/lifestudy_vocab_final_review/batch_001_learning_only.csv`
- batch needs-more-evidence rows: `reports/lifestudy_vocab_final_review/batch_001_needs_more_evidence.csv`
- batch reject rows: `reports/lifestudy_vocab_final_review/batch_001_reject.csv`
- batch summary: `reports/lifestudy_vocab_final_review/batch_001_reviewed_summary.json`

Batch 001 rules:

- exclude all 100 calibration rows
- review exactly 250 rows
- keep every row no-write and not import-ready
- approve front-end rows only when an existing pending formal anchor is supported by the current aligned evidence
- keep biblical, typological, ministry, or domain-like terms in `needs_more_evidence` when one row is not enough
- reject obvious wrong or misaligned candidate meanings

Batch 001 current reviewed result:

- `frontend_ready`: 4
- `learning_only`: 157
- `needs_more_evidence`: 86
- `reject`: 3
- database writes: 0
- final import-ready rows: 0

Batch 001 is not an import package. It is one reviewed production batch and has passed validation.

## Automatic Production Batch Loop

After batch 001 passed, the production workflow was expanded into an automatic no-write loop for the remaining batches.

Loop tooling:

- per-batch generator/reviewer: `scripts/lifestudy_final_review_batch.py`
- generic per-batch smoke: `scripts/lifestudy_final_review_batch_smoke.py`
- full-run orchestrator: `scripts/lifestudy_final_review_run_all.py`
- full aggregation: `scripts/lifestudy_final_review_aggregate.py`
- full-review smoke: `scripts/lifestudy_final_review_final_smoke.py`

Loop behavior:

- completed batches are validated and skipped instead of regenerated
- the first unfinished batch is generated and reviewed
- each batch must pass the generic batch smoke before the next batch starts
- the final batch can contain fewer than 250 rows
- no batch marks rows as import-ready
- no batch writes PostgreSQL
- no batch imports to `reader.dictionary_entries`
- no batch imports to `reader.domain_glossary_entries`

Current loop result:

- skipped completed batches: batch 001
- generated batches: batch 002 through batch 017
- total production batches: 17
- batch 017 size: 2 rows
- final summary: `reports/lifestudy_vocab_final_review/final_review_run_all_summary.json`

## Quality Gate

A batch passes only when:

- every `frontend_ready` row has direct aligned Chinese evidence
- no `frontend_ready` row is a generic common word that would annoy the reader
- sampled `learning_only` and `reject` rows have no more than 2 obvious mistakes among 30 checks
- all approved `frontend_ready` rows are manually rechecked by a stricter second pass
- each `needs_more_evidence` row records exactly what evidence is missing
- batch approval rate is not treated as a target; a batch with few or many approvals is acceptable if the evidence supports it
- no row writes to `reader.dictionary_entries`
- no row writes to PostgreSQL during review

If a batch fails, do not continue to the next batch. Fix the rule and rerun that batch.

## Throughput Target

The original target pace was 2 to 4 production batches per focused work session after calibration.

That means roughly 500 to 1,000 reviewed rows per session, with a full 4,102-row pass expected to take about 5 to 9 focused sessions depending on correction rate.

If a session approves only a tiny number of terms, that is acceptable only when the report shows most rows were truly generic, unstable, or unsupported. If the low approval count comes from an overly strict rule, the rule must be corrected before continuing.

If a session approves too many terms, pause and audit the batch. A high approval rate is allowed only when direct evidence and reading usefulness are strong for each approved row.

The first full automated pass has now completed all batches. Future work should not restart this pass unless a rule bug is found; it should move to final human spot check and final import preparation.

## Front-End Approval Rule

A word should become `frontend_ready` only when it satisfies all of these:

- the Life-study Chinese evidence directly supports the meaning
- the word is useful while reading, not just useful in a vocabulary workbook
- the meaning is stable enough to show without confusing the reader
- it is not merely a name, place, generic verb, generic adjective, or ordinary function word
- the result belongs in the Life-study domain glossary, not the general dictionary

Examples of front-end-worthy terms include domain-specific or context-sensitive words such as `economy`, `dispensing`, `mingled`, `authority`, and `vision` when the aligned Chinese evidence supports their Life-study meaning.

## Decision Record

Every reviewed row must keep enough evidence to explain the decision later:

- English term
- final Chinese meaning, if any
- review category
- evidence English sentence
- evidence Chinese sentence
- reason for decision
- source volume/page
- batch id

This prevents a final package that cannot be audited.

## Final Package

After all 4,102 rows are reviewed:

1. Use `final_database_import_prepare_all_4102.json` as the full candidate import package.
2. Use `final_usability_audit_all_4102.csv` as the per-word product-surface audit.
3. Preserve the strongest evidence row for each term.
4. Run dry-run validation.
5. Only then apply to `reader.domain_glossary_entries` after explicit user confirmation.

The current full review has created the import package and dry-run-ready usability package. It has not applied anything to the database.

## Do Not Do

- Do not import before the full review is complete.
- Do not bulk approve without evidence. The current full package exists only because each row carries aligned evidence and empty/unsupported rows are blocked by smoke.
- Do not use general dictionary meanings as final Chinese meanings.
- Do not put Life-study meanings into `reader.dictionary_entries`.
- Do not re-review the old 2,205 rows from scratch.
- Do not treat the 33,724-row learning table as a front-end dictionary.

## Success Criteria

The review pass is complete when:

- all 4,102 rows have one final category
- all `frontend_ready` rows have aligned Chinese evidence
- no reviewed row is marked import-ready during the review phase
- no reviewed row writes to PostgreSQL during the review phase
- `reader.dictionary_entries` remains unchanged
- `reader.domain_glossary_entries` remains unchanged

The import phase is a separate future step and requires explicit user confirmation. It must:

- build a final import package from the reviewed front-end candidates
- pass final dry-run validation
- keep ordinary books from using Life-study meanings
- write only to `reader.domain_glossary_entries`
