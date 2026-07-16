# AutoJs6 Plugin Index

This repository stores the generated official AutoJs6 plugins index consumed by AutoJs6 Plugin Center.

Run the following command after publishing or updating official AutoJs6 plugin repositories:

```sh
python tools/generate_official_plugin_index.py
```

The command updates `plugins.official.generated.json`. Keeping this index in a standalone repository allows official plugin metadata updates without touching the AutoJs6 main repository.
