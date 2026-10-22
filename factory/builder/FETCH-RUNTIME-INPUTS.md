# Offline runtime input preparation

`fetch_runtime_inputs.py` prepares bytes for the reviewed `provision_runtime.py` and app/UI packager. It does not install dependencies, extract archives, run lifecycle scripts, build the app, sign a release or prove native runtime success.

The helper pins the reviewed provisioner source and executes only those verified source bytes. It reuses the provisioner's exact metadata inventory, immutable OpenClaw npm lock, runtime versions, archive pins and wheel selector. The fetch plan also binds this helper's source hash.

## Required inputs

- Retained dependency directory with the seven reviewed metadata/package files and retained `archives/` Node/OpenClaw files.
- Explicit `--inputs-sha256`: the provisioner's canonical seven-file metadata aggregate. This is not a recursive directory hash.
- Exact pinned retained `PUBLIC-INPUTS.json`, supplying original Node/OpenClaw SHA256 and byte counts.
- Exact pinned native target-Python report, described below. Do not substitute the bootstrap root or the host Python.
- Optional repeatable `--additional-npm-lock PATH SHA256` for the UI or other explicitly reviewed lock v3. This cannot replace the immutable OpenClaw lock. URL duplicates are deduplicated only when integrity pins agree; conflicting pins refuse.
- A separately pinned curl executable for the fetch step.

Private paths are CLI inputs only. The public plan/receipt contain source hashes, relative output paths, pinned public URLs, Python ABI facts and measured results, not host/guest absolute paths, machine IDs or credentials.

### Native Python report

The pinned report must include:

```json
{
  "schema": "decrypt.runtime-target-python.v1",
  "execution": "native-target-observation",
  "platform": "linux-aarch64",
  "targetPythonIdentity": {
    "version": [3, 14, 0],
    "cacheTag": "cpython-314",
    "soabi": "cpython-314-aarch64-linux-gnu"
  },
  "packagingAvailable": true,
  "supportedTags": ["cp314-cp314-manylinux_2_17_aarch64", "cp311-abi3-manylinux_2_17_aarch64", "py3-none-any"]
}
```

These values are **illustrative, not accepted native observations**. Use the exact version/ABI and complete ordered tags obtained from the actual target image's Python, using `packaging.tags.sys_tags` or an explicitly recorded `pip._vendor.packaging.tags` provider. Additional native evidence fields are retained in the independently pinned report but need not be redistributed. Python identity uses exactly the provisioner's `version,cacheTag,soabi` fields; do not add fields inside that object.

The helper refuses missing/unavailable tags or a different interpreter/platform. It does not infer Python3.14 compatibility, select an sdist or enable a source-build fallback. Later `provision_runtime.py` independently probes the real build Python and repeats wheel selection; an incompatible target still refuses.

## Two stages

Run the helper on a dedicated Linux build host with enough free space; it does not manage host resources. Real downloads require Linux. The fixed curl child sets `PR_SET_PDEATHSIG=SIGKILL` before exec and immediately rechecks its parent PID; controller loss therefore stops that child even on SIGKILL. Missing prctl, executable scripts, set-ID binaries or file capabilities refuse. Curl still has its own group for ordinary exact-child cancellation; no generic process signaling is exposed.

First create a **new** plan file:

```text
python3 -I -B fetch_runtime_inputs.py plan \
  --provisioner REVIEWED_PROVISIONER --inputs RETAINED_INPUTS \
  --inputs-sha256 METADATA_AGGREGATE \
  --public-inputs PUBLIC_INPUTS --public-inputs-sha256 PUBLIC_SHA \
  --target-python NATIVE_ABI_REPORT --target-python-sha256 ABI_REPORT_SHA \
  --additional-npm-lock UI_LOCK UI_LOCK_SHA \
  --maximum-total-bytes 8589934592 --plan-output NEW_PLAN_FILE
```

Review/pin that exact plan. Then use the same inputs/options with action `fetch`, replacing `--plan-output` with:

```text
--plan ACCEPTED_PLAN --plan-sha256 PLAN_SHA \
--output NEW_INPUT_DIRECTORY --curl PINNED_CURL --curl-sha256 CURL_SHA
```

The plan is reconstructed from the pinned sources before fetch and must match exactly. No existing output is reused, resumed or removed. A failed directory is retained with `FETCH-FAILURE.json`; a fresh reviewed attempt needs a new directory. No automatic retry exists.

The output contains:

- Unchanged seven metadata/package files at the provisioner's original relative paths.
- `archives/` retained Node/OpenClaw files, copied at20MiB/s and verified against original size/SHA256 plus OpenClaw's pinned SHA512.
- `npm-archives/<sha256(resolvedURL)>.tgz`, each checked against the exact locked SRI. The retained OpenClaw archive is reused for its matching npm row.
- `wheels/` with one explicitly selected, metadata-SHA256-verified binary wheel per required Python package.
- `FETCH-PLAN.json` and `FETCH-RECEIPT.json` (or failure receipt).

Only HTTPS registry.npmjs.org, files.pythonhosted.org and the exact pinned nodejs.org Node archive path are allowed. The Node URL is usable only when explicitly selected into the refetch plan. Curl's first option is `--disable`; clean environment, no redirects, no retries, no resume, no proxy, max-time and max-size limits, one worker, and20MiB/s rate limit are fixed. Curl streams into a fresh bounded file; no downloaded package or script is executed. The default is retained-copy. An explicit `--refetch-public-runtime` flag, present at both plan and fetch, instead allows only the exact reviewed Node URL and OpenClaw metadata URL with all original hashes and byte counts unchanged. The downloaded OpenClaw archive is locally copied for its matching npm row; it is not fetched twice. This is a separately reviewed plan choice, never an automatic fallback.

The whole-fetch budget defaults to8GiB, explicitly configurable from1–32GiB and bound in the plan. Preflight reserves that budget plus1GiB, each file is bounded, and cumulative bytes may not exceed the plan. Unpinned upstream sizes are not invented: npm/PyPI rows are marked `measured-bounded-hash-verified`; retained archive sizes are `origin-pinned`. All accepted files have actual positive byte counts and SHA256, with their locked digest also verified. Hashing and retained copies are capped at20MiB/s.

`dependencyBytesVerified:true` is only byte acceptance. `nativeRuntimeBuildVerified:false`, `lifecycleScriptsExecuted:false` and `signatureVerification:false` remain explicit. Registry metadata may contain signatures; this helper does not claim to verify them. Native build/app/runtime and image acceptance remain separate steps.

## Local qualification

`python3 -B test_fetch_runtime_inputs.py` uses small local fixtures and a fake download callback. It verifies exact source and metadata pins, native-tag requirements, additional-lock conflicts, retained archive reuse, curl argument restrictions, hash/size/space failures, fresh-output refusal and no private paths/native success in receipts. It performs no network or VM action. The actual bounded child-parent-death fixture runs only on Linux; it is explicitly skipped on a non-Linux maintainer machine. Mocked tests cover the prctl setup, parent-loss race refusal, unavailable/privileged-exec rejection and opt-in public-refetch/local npm reuse.
