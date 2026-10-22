# Local customer API

The API binds only to127.0.0.1. Default local app/API ports are8811/8810. It saves optional setup preferences and sends a strategy access request when the user asks. Personal AI remains an explicit opt-in; no broker, engine or trading activation is added.

## Strategy access

`POST /api/strategy-access/request` takes `{tradingViewUsername, contact?}` and
sends only those values to the `requestUrl` in `app/strategy-access.json`
(override: `~/.config/decrypt-os/strategy-access.json` or
`--strategy-access-config`). An empty `requestUrl` turns sending off. If a send
fails, the app shows the username and the public contact route so the user can
send it by hand. No registration, installation ID or signed receipt is involved.
See `strategy_access.py` and `../CONTRACT.md`.

## Browser boundary

`GET /api/session` creates an HttpOnly SameSite=Strict session cookie and returns a CSRF token, but only to a same-origin `fetch()` from the app page (`Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors`, `Sec-Fetch-Dest: empty`, and an `Origin`, if present, equal to the host). Other local clients get 403. Mutations require that session, JSON and `X-CSRF-Token`. Host/Origin are limited to configured loopback ports; cross-site requests are refused. Unknown fields and duplicate JSON keys are rejected. Request bodies are bounded. This is a local browser boundary, not isolation from a compromised same-user/root process.

Read `app/CONTRACT.md` for the full state and personal-AI contracts. Source and fixture tests do not establish actual TradingView access or qualified customer execution.

## Claude assistant

The assistant runs Claude on the isolated customer OpenClaw runtime (`app/harness`). Enable its fixed routes with `--enable-harness`. The user connects their own Claude subscription (setup-token) or Anthropic API key in a native terminal on workspace 4; the API never receives a token or key. See `../CONTRACT.md` and `../harness/README.md`.

## Guardian recovery capability

The installed API enables the optional Guardian capability with `--enable-guardian`; fixture servers leave it disabled unless explicitly selected. No browser or recovery starts merely because source is installed. The local Connections form configures an encrypted vault and separate Tradovate and TradingView logins with opt-in automatic sign-in recovery. TradingView sign-in recovery reuses the customer's own dedicated market browser (loopback port 9567, private profile); it is launched only by the user's open-browser action, never a production/developer browser.

This is sign-in recovery, not a trading transport or engine confirmation publisher. All mutations retain the existing Host/Origin/session/CSRF boundary. Raw credentials, cookies, native HTML and exception text are excluded from status and logs. See the Guardian tests and current release checklist for verified layers.
