# Life-study Full Database Import Preparation

This package corrects the product stance: all 4,102 reviewed Life-study vocabulary rows are treated as database vocabulary assets and single-click popup terms.

No PostgreSQL write is performed here. This package is ready for dry-run import and still requires explicit user confirmation before apply.

## Counts

- Total import candidates: `4102`
- Single-click popup enabled: `4102`
- High-confidence popup priority: `49`
- Learning/search enabled: `4102`
- Needs follow-up but still import candidate: `52`
- Repaired from previous reject: `5`
- Database writes performed: `0`

## Corrected Interpretation

- `can_default_popup=true` means single-click word popup is enabled in the reader.
- `high_confidence_popup=true` means the popup is high-priority/high-confidence.
- `learning_vocab` means the word still belongs in the Life-study popup/search/review/wordbook surfaces.
- Previous `evidence_queue` rows are imported as single-click popup/search assets with a follow-up-review flag.
- Previous `reject` rows were repaired when the aligned evidence supported a clear term meaning.

## Repaired Rows

- `ephesians` -> `以弗所书`
- `philippians` -> `腓立比书`
- `divinity` -> `神性`
- `pharisees` -> `法利赛人`
- `anointed` -> `膏`

## Outputs

- `importable_json`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_all_4102.json`
- `importable_csv`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_all_4102.csv`
- `default_popup_csv`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_default_popup.csv`
- `high_confidence_popup_csv`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_high_confidence_popup.csv`
- `learning_search_csv`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_learning_search.csv`
- `repaired_from_reject_csv`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_repaired_from_reject.csv`
- `summary_json`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_summary.json`
- `summary_md`: `reports/lifestudy_vocab_final_review/final_database_import_prepare_summary.md`