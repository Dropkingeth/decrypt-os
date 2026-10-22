# Get started with Decrypt OS

Built on Omarchy. Assistant: Claude, running on OpenClaw.

This is the in-VM setup guide for the Decrypt OS beta: connecting your own Claude account and opening your workspace once the VM is running. If you still need to download and import the VM itself, start with the main [Getting Started guide](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/docs/GETTING-STARTED.md) instead -- this file picks up after that.

This is beta software. Trading engines stay held until you set them up and resume entries yourself; see [RELEASE-NOTES.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/RELEASE-NOTES.md) for current status and known issues.

## Open your workspace

Open Decrypt OS in Omarchy workspace 5. In **Setup**, select **Start setup**, choose an assistant name and save it. Then open **Connections**.

## Connect Claude

The assistant runs on Claude. Connect your own account in one of two ways:

- **Claude subscription (Pro or Max).** On any computer with Claude Code, run `claude setup-token` and copy the token it prints. Usage counts toward your plan.
- **Anthropic API key.** Create a key in the Anthropic Console (console.anthropic.com). Usage is billed to your API account.

1. Under **Claude account**, select **Start assistant**.
2. When **Sign-in needed** appears, select **Connect Claude**. A terminal opens on workspace 4. Choose 1 (setup-token) or 2 (API key) and paste it there. Never paste it into a chat.
3. When the status is **Signed in; response unverified**, select **Verify connection**. This sends one short test through your account.
4. When **Account connected** appears, select **Open assistant**. Ask a question and a follow-up.

Sign-in and a working response are separate checks. The assistant uses Claude Opus 5.5 and falls back to Claude Sonnet 5 if Opus is unavailable to your account.

## Your ICT assistant

The ICT assistant is a personal mentor for Inner Circle Trader concepts. It uses the same Claude sign-in. Select **Open ICT assistant** in Connections, or use its launcher on workspace 4.

Ask it to explain a concept, quiz you, or talk through a chart: give it the instrument, timeframe, time (New York) and the levels you see. It can check the current New York time and open public web pages. It cannot see your screen, place trades or read your accounts, and it does not give financial advice.

### Keep your sign-in

This step is optional. Without it, **Stop assistant** discards your Claude sign-in and conversation history.

1. Select **Keep sign-in after restart**.
2. In the encrypted-storage setup terminal, create a separate passphrase of at least 12 characters. Repeat it when prompted. Typed characters are hidden.
3. Wait for setup to finish. It saves the session, tests lock/unlock recovery and checks a model response.

Decrypt OS does not save or recover this passphrase. Keep it somewhere safe.

Use **Stop and lock** when you finish, then confirm **Saved sign-in is locked**. To return, select **Unlock saved sign-in** and enter the passphrase in its terminal. Unlock is also required after reboot. The restored account must pass a fresh response check.

The vault protects saved sign-in and history while locked. It does not encrypt the whole OS.

## If a step needs attention

| What you see | What to do |
| --- | --- |
| Signed in, but no reply | Select **Refresh status**, read the connection error and retry only after it clears. Do not stop a session-only runtime unless you are ready to sign in again. |
| Saved sign-in is locked | Select **Unlock saved sign-in**. Enter the storage passphrase in that terminal. |
| Storage setup or unlock failed | Read the error and refresh status. Do not delete or replace the vault. Record the error text for support without passwords or codes. |
| An unexpected password dialog | Do not enter your passphrase. Use only the named Decrypt OS storage setup or unlock terminal. |

Keep keys, passwords and sign-in codes out of conversations. Written help remains available without AI.

## Engines and trading modules

Engines run trading strategies. Modules add functions to an engine or the trading system. AI sign-in and credential storage remain in **Connections**.

The engine workspace saves your engine choices, names and module assignments on this device. Each supported source reference identifies an exact historical engine and strategy pair. It does not establish compatibility with a current TradingView publication.

Add an engine setup, then review the modules listed for that setup. Only explicitly compatible source references can be assigned. Save configuration changes before leaving the editor. Remove its module assignments before removing an engine setup. Setup history is retained.

**Saved** means your choices are stored. It does not mean an execution package is installed or the engine is running. A module with source references only is not working account protection.

### Install an execution package

Use **Execution runtime** above the saved drafts. Each package is a fixed engine and strategy version. Soup sends its orders to the Tradovate bridge on this machine; no third-party relay is used. Soup is the only engine you install here. The other strategies' bridge engines read the decrypt-bridge-v2 alert and ship off; they are set up with the bridge's own setup and are not offered here yet.

1. Set up the Tradovate bridge in a terminal (once):

   ```sh
   M=/usr/local/lib/decrypt-os/modules/tradovate-bridge
   python3 $M/setup.py init --account-name '<your Tradovate account name>' --soup-account-id '<your Tradovate account number>'
   python3 $M/setup.py enable
   ```

   A Tradovate window opens on workspace 3. Sign in there, choose Simulation and let the browser save the login. Your desktop keyring encrypts it. The bridge starts with new entries paused.
2. Install the engine. In **Execution runtime**, choose **Add a held native account and engine**, select Soup, and enter the strategy stream name, a feed name and the TP1 offset.
3. Enter the same Tradovate account number and account name you gave the bridge setup, the environment (demo) and the dated MNQ contract. Installing saves a held engine. It does not place an order.
4. Connect your own saved TradingView chart through **Market feed**. In its separate browser, sign in and use the exact dated MNQ contract, standard candles, a 1-second timeframe and real-time CME data. Your TradingView plan and data subscription must support this. A loaded chart is not a fresh feed.
5. Configure your own HTTPS alert origin and reverse proxy. The local receiver listens only on loopback port 8812. `installer/INGRESS.md` describes the optional receiver installer. `app/trading-runtime/Caddyfile.example` is an example, not an installed service. Never expose the local app/API or Chromium debugging ports.
6. Create the engine's private alert address, copy it into the matching TradingView alert, and keep the strategy's alert body unchanged. Keep the address out of messages, logs and screenshots.
7. Check the blockers. The engine connects to the bridge by itself once the bridge is set up for the same account. **Start with entries held** does not allow entries. **Enable demo entries** needs a fresh account reading from the bridge, an active registration and an open market.
8. In Bridge Advanced (workspace 5), press **Resume** when you want the bridge to accept new entries. Both switches must be on for a new trade.

Before every order the bridge must report the same account and contract. A new entry also needs a fresh reading that shows the account flat with nothing working. The bridge refuses an entry that would open the opposite side to a position or working order in this or another account (correlated index futures count as one instrument). Do not use a funded or live account with this candidate.

### Replace an exposed alert key

Hold new entries and wait for the engine to settle before replacing its private alert key. Key replacement invalidates the old address immediately. It preserves the ledger and previously received signal identities, so changing the address cannot make a duplicate signal new.

Copy the new address into the matching TradingView alert without changing the strategy's alert body. If the response is interrupted, refresh status and use **Show saved private address** to retrieve the current address. Do not replace the key again just to recover the missing response. Keep both addresses out of logs, support messages and screenshots.

### Restarts, entry holds and changing contracts

Every reopened engine starts with entries held. Fresh post-restart data and a fresh bridge reading are required; old signals are not replayed. An uncertain send is retained for reconciliation and is not retried as a new order. **Hold new entries** does not stop management of an existing trade.

Entry restrictions include the reviewed dated-contract calendar, daily maintenance, weekends and conservative holiday holds. Holiday holds are an operator policy, not a claim that CME is closed all day. The current calendar ends December 31, 2026; a reviewed signed release must refresh it before later dates. Strategies keep their own additional sessions and risk policy. The app does not automatically switch contracts or rebind an installed engine.

Dated-contract rollover for an installed native engine is not available in this candidate. When a contract nears its roll date, retire the engine for that account (below) and install a new one for the next dated contract.

For retirement, hold entries, finish the trade and
use **Retire this engine**. A used engine requires a fresh flat reading from the
bridge with no unresolved orders. An unused idle setup can retire without
claiming broker flatness. Retirement retains its exact ledger, sends no close or
cancel order and prevents its old alert address from reopening it.

**Retired engines** lets you review reactivation. Reactivation requires unchanged retained files, a supported exact engine/strategy pair and no active account or route conflict. It does not start a worker or enable entries. Backups retain retired status. At most 32 active and retired instances are retained; the app never deletes an old ledger to make room. Deleting a draft is unrelated to retiring an execution instance.

A module is not a running add-on just because a draft accepts its assignment. The Size Lock module for the Soup engine is the optional module that ships today. Strategy-access requests are separate from engine installation and entry authorization.


## Request strategy access

Decrypt OS needs no registration. You can install and run the Soup engine on your own; it gets no signals until you add a strategy alert. The strategies are invite-only TradingView scripts. Decrypt Labs grants them to your TradingView username.

1. Open **Strategy access**. Enter your TradingView username exactly as it appears on your TradingView profile. Add a contact if you want Decrypt Labs to be able to reach you.
2. Select **Send to Decrypt Labs**. Only the username, and the contact if you added one, are sent.
3. If the app says the request was not sent, copy the username it shows and send it to Decrypt Labs through https://decryptlabs.io.
4. When access is granted, sign in to TradingView with that username. Open **Indicators → Invite-only scripts** and add the strategy to your chart.
5. Create the alert as shown in **Trading → your engine → Private alert delivery**.

Sending your username does not install an engine or enable trading.

## Guardian saved logins

Guardian saves your TradingView sign-in and recovers an expired TradingView session with your permission. It does not handle Tradovate: the bridge window keeps that login, saved by the browser and encrypted by your desktop keyring.

1. Open **Connections** and find **Guardian**. Create a Guardian vault. Keep a passphrase safe; the app cannot recover it.
2. Save your TradingView username and password in the masked form. Turn on automatic sign-in only if you want Guardian to recover the session. Saved credentials are encrypted on this device and never shown again.
3. Finish any two-factor authentication or CAPTCHA yourself in the TradingView window. Guardian does not store one-time codes or bypass these challenges.

Guardian retries an ordinary expired login only while its vault is unlocked and automatic sign-in is on. Attempts are limited, and cooldowns survive restarts. If TradingView rejects the password or asks for human verification, finish sign-in in its window, then use the explicit resume action. Guardian never places or replays an order.

After a restart, unlock the vault again before automatic recovery can use the saved password. Locking the vault does not sign out an open browser session.


## Health notifications and recovery

In **System care**, desktop health notifications are optional and off by default. They cover local runtime, Guardian, market-feed and signal-receiver transitions. Delivery to the desktop notification service is not proof that you read an alert. Unknown delivery is not retried blindly. These are local notifications, not email, phone or an independent monitoring service; a powered-off or disconnected OS cannot notify you through its own desktop.

Encrypted backups exclude provider credentials by default. Include saved credentials only when you intend to protect the resulting archive and its separate passphrase. Browser cookies and enrollment are never included. Store exported encrypted copies on separate storage if you need an independent backup; another file or VM on the same disk is not independent.

Maintenance requires held entries, a fresh flat reading from the bridge with no working or unresolved orders, and stopped writers. The bridge keeps running during maintenance and is not part of the backup; Bridge Advanced exports its settings. If an active trade needs supervision, maintenance refuses without stopping its worker. Restoring records creates a persistent entry hold. Restart, then use **Reconcile restored records** in System care. An idle or closed ledger needs two distinct fresh flat readings from the bridge for its exact account. Unknown commands or active trades remain blocked. An installation with no account or engines can complete an explicit empty-scope check without claiming a broker reading. Successful reconciliation requires another restart, and engines still reopen held. Code rollback preserves newer ledgers and does not rewind trades.

Use the **System care** controls and the installed `installer/MAINTENANCE.md` for local encrypted export/import and signed release staging. Large backup or release files use the local command-line tool, not a pasted browser path. Signed updates still require an explicit activation and restart; unverified or incomplete code is refused.

### Lost passphrases and removal

Passphrases cannot be recovered. If a vault passphrase is lost, preserve its encrypted files and contact support before replacing anything. A known encrypted backup still needs its own passphrase. No automatic reset decrypts existing records.

Removing saved sign-ins locks local in-memory vaults and removes the selected local credential stores while retaining trading ledgers and backups. It does not sign out browser cookies, revoke provider tokens, cancel TradingView alerts, remove provider access or revoke enrollment. Complete those steps in the provider browser/account and with the owner. Never remove supervision during an active trade. Do not run the blank-disk installer as an update or uninstall.
