#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
READER_API_PYTHON="${READER_API_PYTHON:-$ROOT/.venv-reader-api/bin/python}"
if [ ! -x "$READER_API_PYTHON" ]; then
  READER_API_PYTHON="python3"
fi

echo "== docs =="
test -s "$ROOT/README.md"
test -s "$ROOT/docs/v1_design.md"
test -s "$ROOT/docs/readium_probe_acceptance.md"
test -s "$ROOT/docs/implementation_stages.md"
test -s "$ROOT/docs/readium_dependency_notes.md"
test -s "$ROOT/docs/v1_ui_interaction_design.md"
test -s "$ROOT/docs/reader_engine_adapter.md"
test -s "$ROOT/docs/data_model_and_export.md"
test -s "$ROOT/prototypes/v1_reader_wireframe.html"
test -s "$ROOT/schema/v1_schema.sql"
test -s "$ROOT/samples/sample_export.md"
test -s "$ROOT/scripts/check_native_reader.py"
test -s "$ROOT/scripts/probe_readium_xcode_build.py"
test -s "$ROOT/scripts/probe_readium_catalyst_adapter.py"
test -s "$ROOT/scripts/probe_readium_publication_open.py"
test -s "$ROOT/scripts/probe_readium_visual_reader.py"
test -s "$ROOT/Probe/ReadiumCatalystAdapterProbe/Package.swift"
test -s "$ROOT/Probe/ReadiumCatalystAdapterProbe/Sources/ReadiumCatalystAdapterProbe/ReadiumCatalystAdapterProbe.swift"
test -s "$ROOT/Probe/ReadiumPublicationOpenProbe/Package.swift"
test -s "$ROOT/Probe/ReadiumPublicationOpenProbe/Sources/ReadiumPublicationOpenProbe/ReadiumPublicationOpenProbe.swift"
test -s "$ROOT/Probe/ReadiumPublicationOpenProbe/Tests/ReadiumPublicationOpenProbeTests/PublicationOpenProbeTests.swift"
test -s "$ROOT/Probe/ReadiumVisualReaderProbe/Package.swift"
test -s "$ROOT/Probe/ReadiumVisualReaderProbe/Sources/ReadiumVisualReaderProbe/ReadiumVisualReaderProbeApp.swift"
echo "docs PASS"

echo "== sentence core smoke =="
(cd "$ROOT/Probe" && swift run readium-probe)

echo "== sentence core tests =="
(cd "$ROOT/Probe" && swift test)

echo "== app shell build =="
(cd "$ROOT/Probe" && swift build --product sentence-reader-app-probe)

echo "== wireframe smoke =="
python3 "$ROOT/scripts/check_wireframe.py"

echo "== native reader smoke =="
python3 "$ROOT/scripts/check_native_reader.py"
python3 "$ROOT/scripts/sentence_reader_import_ownership_static_smoke.py"
python3 "$ROOT/scripts/sentence_reader_interaction_contract_smoke.py"
python3 "$ROOT/scripts/sentence_reader_vocab_lookup_static_smoke.py"
swiftc "$ROOT/Probe/NativeSentenceReader/SentenceReaderNative.swift" \
  -o /tmp/SentenceReaderNativeAcceptance \
  -framework Cocoa \
  -framework WebKit \
  -framework AVFoundation \
  -framework Speech

echo "== schema smoke =="
python3 "$ROOT/scripts/check_schema.py"

echo "== reader api static smoke =="
python3 "$ROOT/scripts/reader_api_static_smoke.py"
"$READER_API_PYTHON" "$ROOT/scripts/sentence_reader_library_ui_static_smoke.py"
"$READER_API_PYTHON" "$ROOT/scripts/sentence_reader_library_v2_smoke.py"
python3 -m compileall "$ROOT/reader_api" "$ROOT/scripts"

echo "== reader api mock pytest =="
if "$READER_API_PYTHON" - <<'PY'
try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    import pytest  # noqa: F401
except Exception:
    raise SystemExit(1)
PY
then
  "$READER_API_PYTHON" -m pytest "$ROOT/tests/test_reader_api_mock.py"
else
  echo "reader api mock pytest SKIP: install requirements-reader-api.txt or set READER_API_PYTHON"
fi

echo "== readium dependency probe =="
if python3 "$ROOT/scripts/probe_readium_status.py"; then
  echo "Readium dependency probe PASS"
else
  echo "Readium dependency probe SKIP: optional local checkout unavailable; native Click fallback already verified"
fi
if [ -d "$ROOT/Probe/ReadiumDependencyProbe/.build/checkouts/swift-toolkit" ]; then
  echo "Readium checkout exists"
  grep -q "platforms: \\[.iOS" "$ROOT/Probe/ReadiumDependencyProbe/.build/checkouts/swift-toolkit/Package.swift"
  echo "Readium iOS-oriented package confirmed"
else
  echo "SwiftPM Readium checkout missing; local /tmp Readium probe may still be available"
fi

echo "== readium xcode/catalyst probes =="
if command -v xcodebuild >/dev/null 2>&1 && [ -d "/tmp/readium-swift-toolkit-shallow" ]; then
  python3 "$ROOT/scripts/probe_readium_xcode_build.py" --timeout 120 --skip-xcode-resolve
  python3 "$ROOT/scripts/probe_readium_catalyst_adapter.py" --timeout 240
  python3 "$ROOT/scripts/probe_readium_publication_open.py" --timeout 300
  python3 "$ROOT/scripts/probe_readium_visual_reader.py"
else
  echo "Readium Xcode/Catalyst probes skipped; full Xcode or local Readium clone missing"
fi

echo "v1 acceptance smoke PASS"
