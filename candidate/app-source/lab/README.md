# Decrypt OS disposable lab

This directory owns only isolated fixture runs. No production browser, broker, AI key, order route, service, or developer account is attached.

`python3 lab/run.py --check` starts a disposable backend on an ephemeral loopback port, exercises the API contract, and removes its temporary state after stopping only the child process it created. Logs remain inside that temporary directory unless a failure summary is saved. `python3 lab/run.py --serve` opens an interactive fixture backend on port 8810 (or `--port N`); Ctrl-C stops that process and deletes fixture state. Start the UI separately using its documented development command. An occupied port is an error, never a reason to kill another process.

A fixture test is not a clean-VM installation test or a broker commissioning result. Operator-granted access is a lab record only. The real TradingView grant and customer verification remain separate gates.

## Clean guest qualification

Test on a fresh, disposable Linux VM installed from a clean base, without copying another machine's accounts or history. Local fixture checks do not replace a full installation test.

Before connecting a real provider account, save and verify a cold baseline of the guest and test its restore. Keep provider accounts disconnected until that baseline exists. A baseline that contains a local login password hash is private and must not be shared as a distribution image.

Use the [clean-VM checklist](../customer-docs/TEST-CHECKLIST.md) for the next installation, account, vault and reboot checks. Record each manual workaround as a remaining setup issue. Repeated cold boots, restore cycles and a long-running session are still required before reliability is qualified. No cycle count or soak result is claimed here.

Do not restart, reconfigure or copy any existing VM you use for other purposes. Keep guest networking separate, with no shared host folders, inherited SSH keys, production profiles or broker access. Review host capacity before creating another guest. Spare RAM alone is not storage or recovery capacity.

Apple Silicon virtualization qualifies this ARM64 compatibility target only. Standard x86-64 PCs need separate installation, driver and performance tests. Heavy application requirements must be checked against the target's architecture and graphics capabilities before integration.

## Reproducible source handoff

After the integration owner marks the source ready:

```sh
python3 lab/build_source_bundle.py --out-dir lab/artifacts/source-v0.1.0
```

The output is `decrypt-os-source.tar.gz`, `SOURCE-MANIFEST.json` and `BUNDLE-RECEIPT.json`. It contains only explicitly allowed app source/config and the disposable launcher documentation. Repeated builds of identical inputs produce identical archive bytes. Archive ownership, permissions and timestamps are normalized. Symlinks and unsafe archive paths are rejected; developer state, `.env`, secret-named files, dependencies, evidence and private plans are excluded. A narrow private-key/token marker scan also blocks known markers, but is not a comprehensive secret audit.

The manifest lists declared npm dependencies and lockfile version/integrity/license metadata when present, with review pointers. It is **not** a complete SBOM, license audit, vulnerability audit, signature, installer or bootable OS. Downloaded dependencies and real account setup are not bundled. The packaging tests exercise deterministic output, source hash correspondence, unsafe paths, symlinks and excluded private files. Record test counts with each build receipt. Clean guest installation remains a separate gate.
