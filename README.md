# Decrypt OS

Decrypt OS is a public, open-source Linux operating system for running trading strategies and bots.

It is built on Arch Linux ARM with the Omarchy/Hyprland desktop, and it runs inside a virtual machine rather than on bare metal. It bundles the tools to run automated trading strategies and bots in one place, with a local assistant to help you manage and monitor them — a dedicated, repeatable environment instead of running bots on your everyday computer.

## How trading works

- Strategies run as engines inside the Decrypt app. In this beta only Soup can be installed from the app and trade; the other six engines are not tradeable yet.
- Soup sends its orders to the Tradovate bridge on the same machine. The bridge drives your own Tradovate web session in its own browser window. You sign in there once and the browser saves the login, encrypted by your desktop keyring.
- The bridge checks safety only: the right account and contract, fresh account state, a flat account before a new entry, a positive whole-number size, exits that never exceed the open position, and no opposite position in your other accounts. The strategy alert sets the size; the strategy decides everything else.
- Everything ships off. New entries stay held until you release them in the app and resume them in Bridge Advanced.
- Data lives under `~/.local/share/decrypt-os/`. See `candidate/app-source/app/trading-runtime/README.md`.

## Status

beta4 is the first public beta. See [RELEASE-NOTES.md](RELEASE-NOTES.md) for what's included and [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) to download it.

## License

Decrypt OS is free software, licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See [LICENSE](LICENSE). You may use, study, change and share it. If you distribute a modified version, or run one as a service for other people, you must publish your changes under the same license.

Copyright (C) 2026 Decrypt Labs. Third-party components keep their own licenses; see NOTICES.md.

Trading strategies are not part of this repository. They are invite-only TradingView scripts that Decrypt Labs grants to your TradingView username.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities privately through the Security tab ("Report a vulnerability"). Releases are signed with the Ed25519 key `SHA256:fTPlBq3LRYwNA8swEKYocZx/culH9OS2SV+MIWr2QHc` (OpenSSH fingerprint). See [SECURITY.md](SECURITY.md) to check a download.
