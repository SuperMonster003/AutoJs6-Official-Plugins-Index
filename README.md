# AutoJs6 Plugin Index

This repository stores the generated official AutoJs6 plugins index consumed by AutoJs6 Plugin Center.

Run the following command after publishing or updating official AutoJs6 plugin repositories:

```sh
python tools/generate_official_plugin_index.py
```

The command updates `plugins.official.generated.json`. Keeping this index in a standalone repository allows official plugin metadata updates without touching the AutoJs6 main repository.

Schema version 2 expands APK-producing `productFlavors` into separate plugin entries. Each entry contains only the APK assets for its `distributionVariant`; `featured` controls whether Plugin Center should show that distribution by default. Metadata is read from the release tag so it stays aligned with the published APKs. An APK that cannot be assigned to exactly one declared flavor, or a configured featured distribution without an APK, makes generation fail.

Routing metadata is read from Android string `resValue` declarations named
`plugin_engine`, `plugin_variant`, and `plugin_id`. A plugin may also declare the
optional positive host version code `plugin_requires_host_version` and reference
it from manifest `meta-data` named `requiresHostVersion`. Plugins predating these
fields remain valid when the fields are absent. A field that is present but
malformed, unresolved, or inconsistent with another declared source stops index
generation instead of silently publishing weakened routing or compatibility
metadata. For example, the YOLO NCNN `0.1.0` release declares `5275`, the
minimum compatible AutoJs6 host version code.

Schema 2 also permits additive, optional runtime-contract fields. A plugin may
declare the following Android string `resValue` keys and repeat them as manifest
`meta-data` using the names in parentheses:

- `plugin_runtime_component` (`org.autojs.plugin.contract.RUNTIME_COMPONENT`): exact
  `package/fully.qualified.Service` component, which must name a declared
  service in the same application ID.
- `plugin_protocol_api_min` / `plugin_protocol_api_max`
  (`org.autojs.plugin.contract.PROTOCOL_API_MIN` /
  `org.autojs.plugin.contract.PROTOCOL_API_MAX`): an inclusive `major.minor` protocol range; both ends are
  required when either is present.
- `plugin_max_host_version` (`org.autojs.plugin.contract.MAX_HOST_VERSION`): optional inclusive Host version
  ceiling; it requires and cannot be lower than `requiresHostVersion`.
- `plugin_backend`, `plugin_task`, and `plugin_decoder`
  (`org.autojs.plugin.contract.BACKEND`, `org.autojs.plugin.contract.TASK`, and
  `org.autojs.plugin.contract.DECODER`): stable contract identifiers.
- `plugin_supported_abis` (`org.autojs.plugin.contract.SUPPORTED_ABIS`): a comma-separated ABI list. When
  APK names also encode specific ABIs, both declarations must agree.

These fields remain absent for legacy plugins. An explicitly empty, malformed,
unresolved, incomplete, or conflicting declaration stops generation. The
current AutoJs6 Plugin Center consumes the established schema-2 display,
routing, compatibility, ABI, and release fields; these additional fields are a
release-admission contract and are not yet evidence of Host-side runtime
enforcement.

## Final APK admission

After final APK production, this index repository may add an admission manifest
at `release-manifests/<packageName>/<versionCode>.json`. Keeping it in the index
repository avoids a source-artifact hash cycle in the plugin tag. Its schema is:

```json
{
  "schemaVersion": 1,
  "owner": "SuperMonster003",
  "repository": "AutoJs6-Plugin-Example",
  "releaseTag": "v1.0.0",
  "sourceCommit": "40 hexadecimal characters",
  "packageName": "io.github.example.plugin",
  "versionName": "1.0.0",
  "versionCode": 1,
  "signerSha256": [
    "64 hexadecimal characters"
  ],
  "runtimeComponent": "io.github.example.plugin/io.github.example.plugin.ProviderService",
  "supportedAbis": [
    "arm64-v8a"
  ],
  "artifacts": [
    {
      "name": "plugin-v1.0.0-arm64-v8a.apk",
      "sha256": "64 lowercase or uppercase hexadecimal characters",
      "sizeBytes": 123456
    }
  ]
}
```

When this index-owned file exists, it must bind the exact owner, repository,
release tag, resolved 40-character plugin source commit, source version,
package, component, ABI list, signer set, and every selected release APK asset.
The generator validates asset size and digest against GitHub release facts and
the remaining identity against metadata read from the plugin tag. It then emits
normalized admission evidence on each asset. Missing or drifting evidence fails
closed; the manifest cannot override GitHub release facts. Without the file, a
legacy plugin retains its previous shape and does not acquire signer or final
artifact-admission claims. The generator does not inspect or sign APKs itself;
the release producer must write the manifest from its verified final APK
receipt. No YOLO manifest or placeholder hash is committed before those final
facts exist.

`--admission-root <directory>` may be used by an isolated local test; normal
generation reads the index checkout's own `release-manifests` directory.

Run the generator unit tests without network access:

```sh
python -m unittest discover -s tools -p 'test_*.py' -v
```
