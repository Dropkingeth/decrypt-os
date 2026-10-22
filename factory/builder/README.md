# Builder inputs

Helpers that fill the builder cache used by `build/build.sh` (see `build/README.md`):

| File | Use |
| --- | --- |
| `lima-builder.yaml` | Lima definition of the Ubuntu 24.04 ARM64 builder VM |
| `bootstrap_alarm_root.sh` / `.py` | verify the Arch Linux ARM tarball (`--section alarm`) or the Arch Linux x86_64 bootstrap tarball (`--section archlinux`) signature and extract it (tool root) |
| `prepare_x86_64_cache.sh` | fill the x86_64 builder cache: tool root, Omarchy, runtime inputs |
| `seed_runtime_inputs.py` | create `runtime-inputs/` for the builder's architecture (Node, OpenClaw, wheels; hash-checked) |
| `fetch_omarchy.py` | download Omarchy at a pinned commit and check every Git blob hash |
| `fetch_runtime_inputs.py` | download the pinned Node, OpenClaw npm and Python wheel inputs |
| `provision_runtime.py` | runtime pins and helpers used by `build/target_app.py` |

`cryptography.json`, `cffi.json`, `pycparser.json`, `SecretStorage.json` and
`jeepney.json` are the pinned wheel metadata for the Python runtime (one file
per `provision_runtime.py`'s `VERSIONS`). `provision_runtime.verify_inputs`
checks a constant aggregate hash (`PYTHON_METADATA_SHA256`) over all five, the
same way `NPM_LOCK_HASH` pins the npm lockfile -- so a replaced metadata file
fails the build even if it still looks superficially valid (right name,
version, host). To intentionally bump a pinned Python package version,
update the file(s) from PyPI's JSON API (`https://pypi.org/pypi/<name>/<version>/json`,
trimmed to the `files` list fields this repo's schema uses), recompute the
aggregate the same way `python_metadata_aggregate()` does, and update both
`VERSIONS` and `PYTHON_METADATA_SHA256`.
