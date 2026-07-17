# AutoJs6 Plugin Index

This repository stores the generated official AutoJs6 plugins index consumed by AutoJs6 Plugin Center.

Run the following command after publishing or updating official AutoJs6 plugin repositories:

```sh
python tools/generate_official_plugin_index.py
```

The command updates `plugins.official.generated.json`. Keeping this index in a standalone repository allows official plugin metadata updates without touching the AutoJs6 main repository.

Schema version 2 expands APK-producing `productFlavors` into separate plugin entries. Each entry contains only the APK assets for its `distributionVariant`; `featured` controls whether Plugin Center should show that distribution by default. Metadata is read from the release tag so it stays aligned with the published APKs. An APK that cannot be assigned to exactly one declared flavor, or a configured featured distribution without an APK, makes generation fail.

Run the generator unit tests without network access:

```sh
python -m unittest discover -s tools -p 'test_*.py' -v
```
