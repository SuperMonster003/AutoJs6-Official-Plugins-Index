# Native page alignment metadata

Schema 2 includes optional numeric `nativePageAlignment` and string
`nativePageAlignmentSource` fields on each release and on the plugin entry.
The entry copies the newest release by version code; it never borrows a value
from a different release when the newest version is unknown.

- `0`: the measured artifact contains no native ELF payload, or its manifest explicitly declares none.
- `4096`, `16384`, etc.: minimum ELF PT_LOAD alignment in bytes for 64-bit code.
- Missing: no measurement or valid declaration is available. Missing never means zero.
- Source `measured`: the APK was downloaded and inspected, including nested ZIP/IMY assets and extensionless ELF files.
- Source `declared`: measurement was unavailable, so the release-tag manifest declaration was used.

The manifest key is `org.autojs.plugin.contract.NATIVE_PAGE_ALIGNMENT`; the
equivalent string resValue is `plugin_native_page_alignment`. Both accept zero
or a positive power of two and must agree when both are present.

Generation prefers a universal APK, otherwise it checks all named 64-bit splits.
32-bit alignment is advisory. A successful scan is cached by the actual APK
SHA-256 under `.cache/native-alignment/`; GitHub/admission digests are verified
before a receipt is accepted. A corrupt APK, malformed ELF or digest mismatch
fails generation. An unavailable download stays unknown or falls back to a
declaration from the release source. Local unreleased APKs never stand in for
published release assets.

Run the normal generator after publication, or update only the native fields of
an existing index with:

```sh
python tools/generate_official_plugin_index.py --augment-native-alignment
python -m unittest discover -s tools -p 'test_*.py'
```

This field describes ELF alignment. APK ZIP alignment is enforced by the shared
Gradle verifier in each producing repository. Runtime page-size assumptions
(for example the separately tracked Bun/JSC x86_64 limitation) still require
device testing; the field is not a blanket runtime compatibility certificate.
