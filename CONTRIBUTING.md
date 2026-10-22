# Contributing

Decrypt OS is licensed under the GNU Affero General Public License v3.0
(AGPL-3.0; see [LICENSE](LICENSE)). By contributing, you agree your changes
are offered under the same license. The AGPL means that if you distribute a
modified version, or run one as a service other people use over a network,
you must also offer them the complete corresponding source for your
version -- keep that in mind for any change you propose.

Keep changes scoped. Include the problem, affected components,
verification and known limitations. Use generic configuration and
synthetic fixtures; never contribute credentials, customer information,
personalized images or internal logs. Preserve third-party notices (see
[NOTICES.md](NOTICES.md)). Do not submit proprietary strategy logic --
trading strategies are invite-only TradingView scripts and are not part of
this repository (see [README.md](README.md)).

## Building

One command builds a release image from a repo checkout, inside a Linux
ARM64 builder VM:

```sh
build/build.sh --version dev --unsigned-dev
```

See [build/README.md](build/README.md) for the full builder setup (the Lima
VM, its package/runtime cache, and `--online` to refresh it) and
[build/IMPORT.md](build/IMPORT.md) for importing the result into a VM.
`--unsigned-dev` is for local test builds only -- a real release needs
`--signing-key`, and `build.sh` refuses to export without one or the other.

## Testing

Run the tests for whatever you changed:

```sh
python3 -m pytest build factory/builder candidate/base tools
python3 -m pytest candidate/app-source   # if you touched app source
cd candidate/app-source/app/ui && npm test   # if you touched the UI
```

Separate source-fixture/unit results from any native-guest (Linux ARM64)
observations in your PR, and say plainly which platforms you did not test
on. Bridge browser tests never run on a Mac (see
`modules/tradovate-bridge/`'s own docs).

## Privacy check

Before you open a PR, run the privacy gate the release build also runs:

```sh
python3 tools/privacy_check.py --root .
```

It fails on owner-identifying data, host paths, key material and other
secret shapes in the tracked tree (see the module docstring in
`tools/privacy_check.py` for exactly what it looks for, and how to add a
reviewed exemption if you have a real false positive). `--history` also
scans full git history and commit/tag identities, but that mode is for the
public release repo only, not your working branch.

The privacy scan treats emails in license/notice files separately: required
upstream copyright email addresses in `NOTICES.md` and similar attribution
files are preserved verbatim rather than flagged. This exception does not
permit secrets or personal host paths.

## Independent review

Changes to image identity, enrollment, privilege boundaries, signatures or
release packaging need an independent review before merging -- say so
explicitly in your PR.
