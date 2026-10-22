# Retired recovery paths

The old ISO bootstrap, live firewall-module restoration, pacstrap recovery and
ABI13 package injection are not part of the offline image factory. Their entry
files remain present but exit before any operation:

- `bootstrap_iso9660.sh`
- `restore_live_firewall_modules.py`
- `resume_pacstrap.py`
- `install-abi13-compat.sh`

Their original source predates this release and is not included here, along
with its old native instructions. Do not reconstruct or revive the old path.

A failed factory run keeps its logs and partial image. Start a new, separately
bound image only after the cause is understood; do not use recovery scripts to
bypass package locks, zero-byte checks, media signatures or target identity.
