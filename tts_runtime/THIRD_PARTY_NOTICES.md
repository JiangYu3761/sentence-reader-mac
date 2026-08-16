# Click TTS Runtime — Third-Party Notices

This file describes the software bundled in `ClickTTSRuntime`. It does not
grant rights to, promise pricing for, or state terms for the Microsoft Edge
online TTS service. Click does not bundle an Azure subscription or API key.

The build consumes only the exact, SHA-256-locked artifacts in
`python-runtime.lock.json` and
`requirements-edge-tts-macos-arm64-py311.lock`. License texts are copied from
those verified artifacts into the runtime's `licenses/` directory.

| Component | Version | License identifier | Source |
| --- | --- | --- | --- |
| CPython / python-build-standalone artifact | 3.11.15 / 20260602 | Python-2.0 and MPL-2.0; the complete upstream license set is pinned to source commit `9150a58ba8fbf6e9f0683ec3d66365e9b3d1d8e8` | https://github.com/astral-sh/python-build-standalone |
| edge-tts | 7.2.8 | LGPL-3.0-only AND MIT | https://github.com/rany2/edge-tts |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | https://github.com/aio-libs/aiohappyeyeballs |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| aiosignal | 1.4.0 | Apache-2.0 | https://github.com/aio-libs/aiosignal |
| attrs | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| idna | 3.18 | BSD-3-Clause | https://github.com/kjd/idna |
| multidict | 6.7.1 | Apache-2.0 | https://github.com/aio-libs/multidict |
| propcache | 0.5.2 | Apache-2.0 | https://github.com/aio-libs/propcache |
| tabulate | 0.10.0 | MIT | https://github.com/astanin/python-tabulate |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| yarl | 1.24.5 | Apache-2.0 | https://github.com/aio-libs/yarl |

`aiohttp`'s wheel also carries the license for its vendored `llhttp` source.
The CPython runtime archive contains its Python license at
`python/lib/python3.11/LICENSE.txt`; the builder also downloads and verifies
all 21 license and license-index files published by the exact
python-build-standalone source commit above because the stripped binary
archive does not carry that complete set.
The exact upstream license and notice files, rather than this summary, govern
each component.
