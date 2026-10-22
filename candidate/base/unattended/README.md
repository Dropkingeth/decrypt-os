# Desktop preparation helpers

`phase_b.py` installs the Omarchy desktop into a freshly built image: it unpacks the
pinned Omarchy archive (rejecting devices, hard links and escaping links), stages the
account-neutral skeleton in `/etc/skel`, the Decrypt theme and the SDDM greeter, and
enables `sddm`, NetworkManager and time sync. `common.py` holds its small file helpers.

They are called by `build/guest_build.py` (stage `desktop`). See `build/README.md`.
`test_omarchy_archive_paths.py` covers the archive path rules.
