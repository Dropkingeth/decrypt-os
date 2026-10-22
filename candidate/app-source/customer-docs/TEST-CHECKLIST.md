# Check your Decrypt OS install

Use this after you import and start your VM, to confirm the install is working. It is not a requirement -- just a quick way to catch a problem early, before you connect real accounts.

Keep screenshots and notes free of passwords, setup tokens, account numbers, your private alert address and any other codes. Use harmless test messages only.

## First boot

- [ ] The VM starts, you set a user name, password and time zone, and you reach the Decrypt OS sign-in screen without errors.
- [ ] After signing in, Decrypt OS opens in workspace 5 as a single window (no duplicate windows or extra login prompts).
- [ ] The workspace bar shows workspaces 1-5 and Super+1 through Super+5 (or clicking the numbers) switches between them.

## Claude connection

- [ ] In **Setup**, you can name your assistant and save it.
- [ ] In **Connections → Claude**, **Start assistant** opens a sign-in terminal on workspace 4. Pasting a Claude setup-token or an Anthropic API key there is accepted.
- [ ] After **Verify connection**, the status reaches **Account connected**.
- [ ] Opening the assistant and sending a question gets a real reply, and a follow-up question works too.

## ICT assistant

- [ ] **Open ICT assistant** opens a terminal on workspace 4 using the same Claude sign-in.
- [ ] Asking it a question (for example, the current killzone, or to explain a concept) gets a real, conditional answer -- not a trade signal or financial advice.

## Optional: keep your sign-in

Skip this section if you chose session-only sign-in.

- [ ] **Keep sign-in after restart** asks for a separate storage passphrase and finishes without errors.
- [ ] **Stop and lock**, then **Unlock saved sign-in** with that passphrase, restores your account and conversation history.
- [ ] After a VM restart, the saved sign-in stays locked until you unlock it again.

## Tradovate bridge and an engine

Use a Tradovate **Simulation** or demo account for this -- never a funded or live account with this beta.

- [ ] The bridge setup command (or the form in **Connections → Tradovate bridge**) completes, and a Tradovate sign-in window opens on workspace 3.
- [ ] After you sign in once, the bridge status on workspace 3 shows your account connected, and the browser keeps the login for next time.
- [ ] Installing an engine in **Execution runtime** succeeds and the engine shows as **held** -- no order is placed just by installing it.
- [ ] **Bridge Advanced** on workspace 5 shows the bridge and engine status, and entries stay paused until you choose **Resume**.

## Strategy access (optional)

- [ ] **Strategy access** accepts your TradingView username and shows that it sent (or shows the username to send yourself to Decrypt Labs).

## If something didn't work

Note which step failed and what you saw (without any passwords, tokens, account numbers or your private alert address). If written help in [GETTING-STARTED.md](GETTING-STARTED.md) doesn't resolve it, report it through [GitHub Issues](https://github.com/Dropkingeth/decrypt-os/issues/new/choose) -- there are forms for bug reports and feedback.
