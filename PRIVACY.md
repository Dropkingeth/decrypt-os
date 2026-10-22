# Privacy note: strategy access requests

This note covers one thing: what happens when you use the Decrypt OS app's
**Strategy access** page (or send a request by hand via
[decryptlabs.io](https://decryptlabs.io)) to ask for an invite-only
TradingView strategy. Nothing else in Decrypt OS sends data anywhere by
itself. See [RELEASE-NOTES.md](RELEASE-NOTES.md) and
[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) for what the app and the
bridge do and don't send.

## What's stored

When you submit a strategy access request, Decrypt Labs stores:

- **Your TradingView username**, exactly as typed.
- **An optional contact method**, only if you choose to type one. If you
  leave it blank, nothing is stored for it.
- **A salted hash of the IP address the request came from.** The raw IP
  address is not stored. The hash cannot be reversed back to the original
  IP, but it is the same length and shape for every request, so it can be
  compared to other requests from the same address.
- The strategy or strategies you picked, the time of the request, and
  whether it was sent successfully.

The request does not include your account numbers, broker credentials,
machine identifiers, or anything else from your Decrypt OS install. See
[`candidate/app-source/app/CONTRACT.md`](candidate/app-source/app/CONTRACT.md)
for the exact request shape the app sends.

## Why it's stored

- **Access management.** Decrypt Labs adds TradingView usernames to each
  invite-only script's access list by hand. The stored username and status
  are how that gets done and tracked.
- **Abuse prevention.** The salted IP hash is used only to apply rate
  limits (a small number of requests per address per hour/day) so the
  request form can't be used to spam the invite-only scripts or flood the
  queue. It is not used to identify or track you, and it is not shared.
- The optional contact method, if you provide one, is used only to follow
  up about that specific request (for example, if it fails to send).

## How to ask for removal

To ask Decrypt Labs to remove your stored request data, contact Decrypt
Labs:

- Open a GitHub issue using the **Strategy access problem** template at
  <https://github.com/Dropkingeth/decrypt-os/issues/new/choose>, or
- contact Decrypt Labs through <https://decryptlabs.io>.

Include the TradingView username your request used so it can be found.
Removing stored request data does not remove any access TradingView itself
has already granted to that username; that is a separate step in
TradingView under **Indicators -> Invite-only scripts**.
