#!/usr/bin/env python3
"""Runs only inside the customer service mount namespace and DynamicUser."""
from __future__ import annotations
import fcntl, json, os, pathlib, secrets, shutil, stat, subprocess, sys, time, signal, pwd
from contextlib import ExitStack

ROOT = pathlib.Path("/run/decrypt-os-customer-pilot")
NODE = "/opt/decrypt-node/bin/node"
CLI = "/opt/decrypt-openclaw/openclaw.mjs"
PROFILE = "decrypt-customer"
PORT = 19789
# Claude only. The customer signs in with their own Anthropic account.
PROVIDER = "anthropic"
AUTH_PROFILE = "anthropic:customer"
MODEL = "anthropic/claude-opus-5-5"
FALLBACK_MODELS = ("anthropic/claude-sonnet-5",)
LIGHT_MODEL = "anthropic/claude-haiku-4-5"  # heartbeat, compaction and titles only
AUTH_METHODS = {"1": "setup-token", "2": "api-key"}
# Claude Code is proprietary; the OS never bundles it. This is only an optional,
# user-initiated convenience for making a setup-token when the user has no other
# computer with Claude Code at hand. It installs one exact, hash-pinned release
# of Claude Code's native Linux ARM64 package (the only file the wrapper package
# would copy in its install script) with npm ci from a lockfile, install scripts
# off, no proxy, into this account's own runtime root. Never into the bundled,
# read-only OpenClaw/Node install. A pin bump means: new version, tarball URL
# and the registry's "integrity" value for that exact tarball.
CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code-linux-arm64"
CLAUDE_CODE_VERSION = "2.1.287"
CLAUDE_CODE_TARBALL = ("https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/"
                       "claude-code-linux-arm64-2.1.287.tgz")
CLAUDE_CODE_INTEGRITY = ("sha512-MU3ReK1kjstDmjEOzhFZaphh2AVUKnPaik6lOV0WLs3/"
                         "mOM7mE/mqkdcQ/Sv0KwHH5MnOhSkIjCEl0/8tr+vRg==")
CLAUDE_CODE_DIR_NAME = "claude-code-helper"
NPM_REGISTRY = "https://registry.npmjs.org/"
NPM_CLI = "/opt/decrypt-node/lib/node_modules/npm/bin/npm-cli.js"
ICT_AGENT = "ict"
APPROVED_SKILLS = ("decrypt-unslop", "decrypt-frontend-design", "review-skill-imports")
ICT_SOURCE = pathlib.Path("/opt/decrypt-ict-assistant")
# OpenClaw's own web_fetch SSRF policy can only add private-network exceptions
# or deny specific patterns; it has no "allow only these public hosts" mode.
# web_reader_proxy.py (packaged with the ICT pack) supplies that: a loopback
# proxy that refuses every host except the ICT assistant's own sources. Only
# the ict agent ever has web_fetch (main's tools are entirely denied), so
# routing it through tools.web.fetch.useTrustedEnvProxy below is scoped in
# practice to the ICT assistant's own reading, not model/API traffic.
WEB_READER_PROXY_PORT = 8783

def persistent_mode():
    return os.environ.get("DECRYPT_CUSTOMER_STORAGE") == "encrypted"

def valid_customer_uid(uid):
    if persistent_mode():
        try:
            return uid not in (0, 1000) and uid == pwd.getpwnam("decrypt-os-customer").pw_uid
        except KeyError:
            return False
    return 61184 <= uid <= 65519

def environment():
    return {
        "PATH": "/opt/decrypt-node/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm-256color",
        "HOME": str(ROOT / "home"), "OPENCLAW_HOME": str(ROOT / "home"),
        "OPENCLAW_STATE_DIR": str(ROOT / "state"),
        "OPENCLAW_CONFIG_PATH": str(ROOT / "config/openclaw.json"),
        "OPENCLAW_PROFILE": PROFILE, "OPENCLAW_GATEWAY_PORT": str(PORT),
        "CLAUDE_CONFIG_DIR": str(ROOT / "claude"),
        "XDG_CONFIG_HOME": str(ROOT / "config"), "XDG_CACHE_HOME": str(ROOT / "cache"),
        "XDG_DATA_HOME": str(ROOT / "data"), "XDG_RUNTIME_DIR": str(ROOT / "runtime"),
        "TMPDIR": str(ROOT / "tmp"),
        "OPENCLAW_DISABLE_BONJOUR": "1", "OPENCLAW_SKIP_CHANNELS": "1",
        "OPENCLAW_EXEC_SHELL_SNAPSHOT": "0", "OPENCLAW_NO_RESPAWN": "1",
        "OPENCLAW_NO_AUTO_UPDATE": "1", "DO_NOT_TRACK": "1", "NO_COLOR": "1",
        "DECRYPT_CUSTOMER_STORAGE": "encrypted" if persistent_mode() else "session_only",
        # Scoped to tools.web.fetch.useTrustedEnvProxy in config() below; the
        # ict agent is the only one with web_fetch at all.
        "HTTP_PROXY": "http://127.0.0.1:" + str(WEB_READER_PROXY_PORT),
        "HTTPS_PROXY": "http://127.0.0.1:" + str(WEB_READER_PROXY_PORT),
    }

def boundary():
    st = ROOT.stat()
    if not valid_customer_uid(os.getuid()) or st.st_uid != os.getuid():
        raise RuntimeError("Unexpected customer runtime identity.")
    # Base-OS default-user placeholder path; installer/clean_base.py::adapt_bootstrap
    # rewrites this check to a general home/root-visibility guard at install time.
    if pathlib.Path("/home/customer/.openclaw").exists():
        raise RuntimeError("Owner home is visible; customer runtime refused.")
    if not pathlib.Path(CLI).is_file() or not pathlib.Path(NODE).is_file():
        raise RuntimeError("Public runtime mounts are unavailable.")
    for forbidden in ("/run/dbus/system_bus_socket", "/run/docker.sock", "/var/run/docker.sock"):
        if pathlib.Path(forbidden).exists():
            raise RuntimeError("A host control socket is visible.")

def cli(args, timeout=60):
    result = subprocess.run([NODE, CLI, "--profile", PROFILE, *args],
                            env=environment(), cwd=ROOT / "workspace",
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("An isolated OpenClaw operation failed.")
    return result.stdout

def start_web_reader_proxy():
    """Loopback allow-list proxy for the ict agent's web_fetch (see
    web_reader_proxy.py and WEB_READER_PROXY_PORT above). Loaded from the
    packaged ICT pack mount. Without it serve() stops: web_fetch is pointed at
    this port, so any other local process that took it would become the proxy."""
    module_path = ICT_SOURCE / "web_reader_proxy.py"
    if not module_path.is_file():
        raise RuntimeError("The ICT web reader proxy is missing.")
    import importlib.util
    spec = importlib.util.spec_from_file_location("decrypt_web_reader_proxy", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.start(WEB_READER_PROXY_PORT, "127.0.0.1")

def ict_agent():
    """Packaged ICT mentor agent. Reads its reviewed OpenClaw fragment."""
    fragment = json.loads((ICT_SOURCE / "openclaw-agent.json").read_text())
    entry = fragment["agents"]["entries"][ICT_AGENT]
    if fragment.get("plugins") or set(fragment) != {"agents"}:
        raise RuntimeError("ICT agent fragment may only declare its agent entry.")
    return {**entry, "workspace": str(ROOT / "workspace-ict")}

def config(ict=None):
    models = {name: {"agentRuntime": {"id": "openclaw"}}
              for name in (MODEL, *FALLBACK_MODELS, LIGHT_MODEL)}
    entries = {"main": {"workspace": str(ROOT / "workspace"),
                        "skills": list(APPROVED_SKILLS),
                        # The product helper keeps the pilot's no-tools boundary.
                        "tools": {"deny": ["*"]}}}
    if ict is not None:
        entries[ICT_AGENT] = ict
    return {
        "gateway": {"mode": "local", "port": PORT, "bind": "loopback",
                    "auth": {"mode": "token",
                             "token": {"source": "file", "provider": "customer", "id": "/gatewayToken"},
                             "allowTailscale": False},
                    "tailscale": {"mode": "off"}, "terminal": {"enabled": False},
                    "cliAgents": {"enabled": False}, "controlUi": {"enabled": False},
                    "nodes": {"pairing": {"autoApproveLocal": False, "sshVerify": False}},
                    "http": {"endpoints": {"responses": {"enabled": False},
                                           "chatCompletions": {"enabled": False}}}},
        "secrets": {"providers": {"customer": {"source": "file",
                     "path": str(ROOT / "secrets/gateway.json"), "mode": "json"}}},
        # Explicit ownership: no retired "default" marker. CLI calls name --agent.
        "agents": {"ownership": "explicit",
                   "defaults": {"workspace": str(ROOT / "workspace"),
                    "model": {"primary": MODEL, "fallbacks": list(FALLBACK_MODELS)},
                    "models": models,
                    "userTimezone": "America/New_York",
                    "utilityModel": LIGHT_MODEL,
                    "systemAgent": {"agentId": "main"},
                    "heartbeat": {"every": "0m", "isolatedSession": True, "agentId": "main",
                                  "lightContext": True, "model": LIGHT_MODEL},
                    "compaction": {"model": LIGHT_MODEL}},
                   "entries": entries},
        # Profile "minimal" allows only session_status. Agents can narrow it
        # further (main denies everything); nothing here grants shell, files,
        # browser, messaging, automation or device control.
        "tools": {"profile": "minimal",
                  "deny": ["group:runtime", "group:fs", "group:ui", "group:automation",
                           "group:nodes", "group:messaging", "group:media", "group:memory"],
                  "elevated": {"enabled": False}, "agentToAgent": {"enabled": False},
                  # Only the ict agent ever gets web_fetch; routed through the
                  # allow-listed loopback proxy web_reader_proxy.py starts in
                  # serve(). HTTP_PROXY/HTTPS_PROXY come from environment().
                  "web": {"fetch": {"useTrustedEnvProxy": True}}},
        "browser": {"enabled": False}, "channels": {},
        "cron": {"enabled": False, "triggers": {"enabled": False}},
        "hooks": {"enabled": False},
        "discovery": {"mdns": {"mode": "off"}},
        "update": {"checkOnStart": False, "auto": {"enabled": False}},
        "telemetry": {"enabled": False}, "env": {"shellEnv": {"enabled": False}},
        "plugins": {"allow": [PROVIDER]},
        "logging": {"file": str(ROOT / "logs/gateway.log")},
    }

def atomic_json(path, value):
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)
    os.replace(tmp, path)

def start_gateway():
    return subprocess.Popen([NODE, CLI, "--profile", PROFILE, "gateway", "run",
                              "--port", str(PORT), "--bind", "loopback"],
                             env=environment(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=ROOT / "workspace")

def restart_gateway(child):
    """Replace only the child, retaining the service namespace and tmpfs state."""
    invalidate_proof()
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    return start_gateway()

def gateway_refresh_state():
    if (ROOT / "runtime/gateway-refresh-failed").exists():
        return "error"
    if any((ROOT / ("runtime/" + name)).exists() for name in
           ("gateway-refresh-requested", "gateway-refresh-in-progress")):
        return "starting"
    return None

def replacement_status(state):
    return {"status": state, "model": MODEL, "protocolReady": False,
            "loginRunning": lock_active("login"), "consoleRunning": lock_active("console"),
            "scopeUpdatedAt": None, "checkedAtMonotonic": time.monotonic(),
            "authExpiresAtMs": None, "authUsable": False,
            "error": ("Customer Gateway replacement failed; sign-in state is retained."
                      if state == "error" else "Refreshing the customer Gateway after sign-in.")}

def request_gateway_refresh():
    # Native sign-in can change shared-store ownership after Gateway startup.
    # A process-local reload retains that cached ownership in this pinned build.
    # No tokens, paths or user input enter this fixed app-owned signal.
    invalidate_proof()
    (ROOT / "runtime/gateway-refresh-requested").touch(mode=0o600)

def serve():
    boundary()
    os.umask(0o077)
    for name in ("home", "state", "config", "cache", "data", "runtime",
                 "claude", "tmp", "workspace", "workspace-ict", "secrets", "logs",
                 CLAUDE_CODE_DIR_NAME):
        (ROOT / name).mkdir(mode=0o700, exist_ok=True)
    source = pathlib.Path("/opt/decrypt-customer-workspace")
    if not (source / "AGENTS.md").is_file():
        raise RuntimeError("Customer scope is missing.")
    for item in source.iterdir():
        target = ROOT / "workspace" / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(item, target)
    # No model tools: make reviewed public system scope part of automatic bootstrap,
    # not merely a linked file that the model cannot read.
    agent_file = ROOT / "workspace/AGENTS.md"
    system_file = ROOT / "workspace/SYSTEM.md"
    if system_file.is_file():
        with agent_file.open("a") as output:
            output.write("\n\n## Supplied customer system context (snapshot, not live health)\n")
            output.write(system_file.read_text())
            output.write("\nNo model tools are enabled. Do not claim to inspect or repair live state.\n")
    ict = install_ict_workspace(source)
    if not (ROOT / "secrets/gateway.json").exists():
        atomic_json(ROOT / "secrets/gateway.json", {"gatewayToken": secrets.token_urlsafe(48)})
    patch = ROOT / "config/customer-patch.json"
    atomic_json(patch, config(ict))
    cli(["config", "patch", "--file", str(patch)], timeout=120)
    cli(["config", "validate", "--json"], timeout=60)
    if persistent_mode() and (ROOT / "data/customer-login-completed").is_file():
        (ROOT / "runtime/login-completed").touch(mode=0o600)
    invalidate_proof()
    # Marker is app lifecycle metadata only, not a credential/profile database.
    atomic_json(ROOT / "runtime/lifecycle.json",
                {"scopeUpdatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    start_web_reader_proxy()
    # Keep an app-owned supervisor so status can return quickly without initiating models.
    os.chdir(ROOT / "workspace")
    child = start_gateway()
    def terminate(signum, frame):
        if child.poll() is None:
            child.terminate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    while True:
        refresh_request = ROOT / "runtime/gateway-refresh-requested"
        if refresh_request.exists():
            # Serialize with login and verification; never replace mid-authentication.
            with (ROOT / "runtime/auth-operation.lock").open("a") as auth_lock:
                try:
                    fcntl.flock(auth_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(1)
                    continue
                progress = ROOT / "runtime/gateway-refresh-in-progress"
                failed = ROOT / "runtime/gateway-refresh-failed"
                progress.touch(mode=0o600)
                failed.unlink(missing_ok=True)
                invalidate_proof()
                atomic_json(ROOT / "runtime/status.json", replacement_status("starting"))
                refresh_request.unlink()
                try:
                    child = restart_gateway(child)
                    atomic_json(ROOT / "runtime/gateway-refreshed.json",
                                {"refreshedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "pid": child.pid})
                except Exception:
                    # Keep the outer service alive: exiting it would destroy tmpfs auth.
                    failed.touch(mode=0o600)
                finally:
                    progress.unlink(missing_ok=True)
        if child.poll() is not None:
            invalidate_proof()
            (ROOT / "runtime/gateway-refresh-failed").touch(mode=0o600)
        snapshot = refresh_status()
        atomic_json(ROOT / "runtime/status.json", snapshot)
        for _ in range(10):
            if (ROOT / "runtime/gateway-refresh-requested").exists():
                break
            time.sleep(1)

def compose_ict_agents(pack):
    """Workspace AGENTS.md = operating rules + the reviewed knowledge pack.

    The ICT agent has no file tools, so its knowledge must be bootstrap context.
    """
    parts = [(pack / "workspace/AGENTS.md").read_text().rstrip()]
    parts.append("\n\n# ICT knowledge pack\n\nReviewed, own-words summaries. Link to the sources in"
                 " sources.md instead of quoting transcripts.")
    for item in sorted((pack / "knowledge").glob("*.md")):
        parts.append("\n\n" + item.read_text().strip())
    return "\n".join(parts) + "\n"

def install_ict_workspace(customer_source):
    """Copy the packaged ICT agent workspace; return its config entry."""
    if not (ICT_SOURCE / "workspace/AGENTS.md").is_file():
        return None
    target = ROOT / "workspace-ict"
    for item in (ICT_SOURCE / "workspace").iterdir():
        if item.is_file():
            shutil.copyfile(item, target / item.name)
    # Same reviewed skills as the product helper (approved skill set).
    if (customer_source / "skills").is_dir():
        shutil.copytree(customer_source / "skills", target / "skills", dirs_exist_ok=True)
    (target / "AGENTS.md").write_text(compose_ict_agents(ICT_SOURCE))
    return ict_agent()

def lock_active(name):
    path = ROOT / "runtime" / (name + ".lock")
    if not path.exists():
        return False
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True

def usable_auth(auth, now_ms=None):
    """Public models.authStatus metadata only; never credential rows.

    Returns (usable, expiry_ms). Setup-tokens without expiry and API keys report
    status "static" and no expiry; OAuth/expiring tokens must not be expired.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not isinstance(auth, dict) or auth.get("unavailable"):
        return False, None
    providers = auth.get("providers")
    if not isinstance(providers, list):
        return False, None
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("provider") != PROVIDER:
            continue
        if provider.get("status") not in ("ok", "expiring", "static"):
            continue
        order = provider.get("profileOrder")
        if order is not None and (not isinstance(order, list) or AUTH_PROFILE not in order):
            continue
        profiles = provider.get("profiles")
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict) or profile.get("profileId") != AUTH_PROFILE:
                continue
            if profile.get("type") not in ("token", "api_key", "oauth"):
                continue
            if profile.get("status") not in ("ok", "expiring", "static"):
                continue
            expiry_info = profile.get("expiry")
            expiry = expiry_info.get("at") if isinstance(expiry_info, dict) else None
            if expiry is None:
                if profile.get("type") == "oauth":
                    continue  # OAuth always carries an expiry
                return True, None
            if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry > now_ms:
                return True, int(expiry)
    return False, None

def invalidate_proof():
    # Remove only our own safe assertion, not provider auth or conversation data.
    (ROOT / "runtime/inference-proof.json").unlink(missing_ok=True)

def refresh_status():
    boundary()
    replacement = gateway_refresh_state()
    if replacement:
        invalidate_proof()
        return replacement_status(replacement)
    base = {"status": "starting", "model": MODEL, "protocolReady": False,
            "loginRunning": lock_active("login"), "consoleRunning": lock_active("console"),
            "scopeUpdatedAt": None, "error": None, "checkedAtMonotonic": time.monotonic(),
            "authExpiresAtMs": None, "authUsable": False}
    if not (ROOT / "runtime/lifecycle.json").exists():
        return base
    base["scopeUpdatedAt"] = json.loads((ROOT / "runtime/lifecycle.json").read_text()).get("scopeUpdatedAt")
    try:
        # Supported CLI waits for protocol hello-ok before dispatching health RPC.
        # Pinned source: dist/call-V6wqNFR2.mjs onHelloOk callback (line466).
        health = json.loads(cli(["gateway", "call", "health", "--json", "--timeout", "10000"], timeout=20))
        if not isinstance(health, dict):
            raise RuntimeError("Invalid health response.")
        base["protocolReady"] = True
        base["status"] = "needs_login"
        # Query supported metadata only. No runtime database/file credentials reads.
        auth = json.loads(cli(["gateway", "call", "models.authStatus", "--json",
                               "--params", '{"agentId":"main"}', "--timeout", "10000"], timeout=20))
        # Accept only the dedicated customer Claude profile's public health/expiry.
        usable, expiry = usable_auth(auth)
        base["authStatusAvailable"] = isinstance(auth, dict)
        base["authUsable"] = usable
        base["authExpiresAtMs"] = expiry
        if usable and (ROOT / "runtime/login-completed").exists():
            base["status"] = "ready"
            if (ROOT / "runtime/inference-proof.json").exists():
                proof = json.loads((ROOT / "runtime/inference-proof.json").read_text())
                base["status"] = "connected"
                base["verifiedAt"] = proof["verifiedAt"]
        else:
            invalidate_proof()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        base["error"] = "Authenticated Gateway readiness is not yet verified."
    return base

def status():
    boundary()
    replacement = gateway_refresh_state()
    if replacement:
        value = replacement_status(replacement)
        return {key: value[key] for key in ("status", "model", "protocolReady",
                "loginRunning", "consoleRunning", "scopeUpdatedAt", "error")}
    path = ROOT / "runtime/status.json"
    if not path.exists():
        return {"status": "starting", "model": MODEL, "protocolReady": False}
    value = json.loads(path.read_text())
    checked = value.get("checkedAtMonotonic")
    stale = not isinstance(checked, (int, float)) or not 0 <= time.monotonic() - checked <= 60
    expiry = value.get("authExpiresAtMs")
    if stale:
        value.update(status="error", protocolReady=False,
                     error="Customer readiness snapshot expired; waiting for a fresh protocol check.")
    elif value.get("status") in ("ready", "connected"):
        expired = expiry is not None and (not isinstance(expiry, (int, float)) or expiry <= time.time() * 1000)
        if value.get("authUsable") is not True or expired:
            value.update(status="needs_login", error="Claude sign-in needs to be checked again.")
        elif not (ROOT / "runtime/login-completed").exists():
            value["status"] = "needs_login"
        elif value["status"] == "connected" and not (ROOT / "runtime/inference-proof.json").exists():
            value["status"] = "ready"
    value["loginRunning"] = lock_active("login")
    value["consoleRunning"] = lock_active("console")
    if value.get("status") != "connected":
        value.pop("verifiedAt", None)
    # Do not return internal monotonic values or profile expiry beyond this helper.
    return {key: value[key] for key in ("status", "model", "protocolReady", "loginRunning",
             "consoleRunning", "scopeUpdatedAt", "error", "verifiedAt") if key in value}

def choose_auth_method(read=input):
    print("Connect your own Claude account.")
    print("  1) Claude subscription (Pro or Max): paste a setup-token.")
    print("     Make one with 'claude setup-token' on any computer that has Claude Code,")
    print("     or install a sign-in helper right here when asked next.")
    print("  2) Anthropic API key (pay as you go, from console.anthropic.com).")
    for _ in range(3):
        choice = read("Choose 1 or 2: ").strip()
        if choice in AUTH_METHODS:
            return AUTH_METHODS[choice]
    raise RuntimeError("No sign-in method was chosen.")


def claude_code_prefix():
    return ROOT / CLAUDE_CODE_DIR_NAME


def claude_code_package_dir():
    return claude_code_prefix() / "node_modules" / CLAUDE_CODE_PACKAGE


def claude_code_binary():
    return claude_code_package_dir() / "claude"


def helper_environment():
    """The runtime environment without any proxy: the ICT web reader proxy only
    allows the ICT sources and must not apply to npm or to Claude Code."""
    env = {key: value for key, value in environment().items() if "proxy" not in key.lower()}
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def claude_code_manifest():
    """package.json and package-lock.json that pin the one helper package by
    exact version, registry tarball and sha512 integrity."""
    name, version = "decrypt-claude-signin-helper", "1.0.0"
    package = {"name": name, "version": version, "private": True,
               "dependencies": {CLAUDE_CODE_PACKAGE: CLAUDE_CODE_VERSION}}
    lock = {"name": name, "version": version, "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": name, "version": version,
                              "dependencies": {CLAUDE_CODE_PACKAGE: CLAUDE_CODE_VERSION}},
                         "node_modules/" + CLAUDE_CODE_PACKAGE: {
                             "version": CLAUDE_CODE_VERSION, "resolved": CLAUDE_CODE_TARBALL,
                             "integrity": CLAUDE_CODE_INTEGRITY, "cpu": ["arm64"], "os": ["linux"],
                             "libc": ["glibc"], "license": "SEE LICENSE IN LICENSE.md"}}}
    return package, lock


def claude_code_install_command(prefix):
    # Empty, separate user and global configs: no npmrc can add a proxy, registry or script.
    # Runs with cwd=prefix, whose package.json makes it the project root. (npm ci
    # with --prefix refuses an in-sync lockfile in npm 11.)
    return [NODE, NPM_CLI, "ci",
            "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund", "--no-progress",
            "--no-update-notifier", "--strict-ssl=true", "--registry=" + NPM_REGISTRY,
            "--userconfig=" + str(prefix / "empty-user.npmrc"),
            "--globalconfig=" + str(prefix / "empty-global.npmrc"),
            "--cache=" + str(ROOT / "tmp/claude-helper-npm-cache")]


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("The Claude sign-in helper folder is not private to this account.")


def install_claude_code_helper():
    """User-initiated only (see choose_claude_code_helper). npm ci verifies the
    pinned sha512 before unpacking; install scripts never run. Used only to run
    'claude setup-token'; the API key sign-in path never touches this."""
    prefix = claude_code_prefix()
    private_directory(prefix)
    package, lock = claude_code_manifest()
    atomic_json(prefix / "package.json", package)
    atomic_json(prefix / "package-lock.json", lock)
    for name in ("empty-user.npmrc", "empty-global.npmrc"):
        (prefix / name).write_text("")
    try:
        result = subprocess.run(claude_code_install_command(prefix),
                                cwd=prefix, env=helper_environment(),
                                stdin=subprocess.DEVNULL, check=False)
    finally:
        shutil.rmtree(ROOT / "tmp/claude-helper-npm-cache", ignore_errors=True)
    if result.returncode or not installed_claude_code():
        raise RuntimeError("Installing the Claude sign-in helper failed. Check your network "
                            "connection and try again, paste a token made elsewhere, or use "
                            "an Anthropic API key instead.")


def installed_claude_code():
    """The pinned package, unpacked in place: a real executable, not a link."""
    try:
        meta = json.loads((claude_code_package_dir() / "package.json").read_text())
        info = claude_code_binary().lstat()
    except (OSError, ValueError):
        return False
    return (isinstance(meta, dict) and meta.get("name") == CLAUDE_CODE_PACKAGE
            and meta.get("version") == CLAUDE_CODE_VERSION and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid() and bool(info.st_mode & 0o100))


def claude_setup_token_command():
    return [str(claude_code_binary()), "setup-token"]


def run_claude_setup_token():
    if not installed_claude_code():
        raise RuntimeError("The Claude sign-in helper is not installed.")
    result = subprocess.run(claude_setup_token_command(), cwd=ROOT / "workspace",
                            env=helper_environment(), check=False)
    if result.returncode:
        raise RuntimeError("'claude setup-token' did not finish. You can still paste a token "
                            "made elsewhere, or use an Anthropic API key instead.")


def choose_claude_code_helper(read=input):
    """Entirely optional; only runs the npm install and setup-token below when
    the user types yes. Skipping this leaves the setup-token paste prompt
    exactly as before, so a user who already has a token is unaffected."""
    print()
    print("Decrypt OS does not include Claude Code (its license is proprietary).")
    print("It is only needed here to make a setup-token for Claude subscription sign-in.")
    print("If you already have a setup-token, or another computer with Claude Code,")
    print("you do not need this.")
    choice = read("Install the Claude sign-in helper now and run its setup-token? [y/N]: ").strip().lower()
    if choice not in ("y", "yes"):
        return
    print("Installing Claude Code " + CLAUDE_CODE_VERSION + " from the public npm registry")
    print("(about 110 MB; checked against its published sha512, install scripts off)...")
    install_claude_code_helper()
    print("Installed. Running 'claude setup-token' -- follow its own sign-in steps below,")
    print("then paste the token it prints when asked.")
    run_claude_setup_token()

def interactive_args(operation, method=None):
    if operation == "login":
        if method not in AUTH_METHODS.values():
            raise RuntimeError("Unsupported sign-in method.")
        # --agent main: the ICT agent reads through to this shared profile.
        return ["models", "auth", "login", "--agent", "main", "--provider", PROVIDER,
                "--method", method, "--profile-id", AUTH_PROFILE, "--force"]
    if operation == "ict":
        return ["tui", "--session", "agent:" + ICT_AGENT + ":main"]
    return ["tui", "--session", "agent:main:main"]

def interactive(operation):
    with ExitStack() as locks:
        boundary()
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("A real owner terminal is required.")
        lock = locks.enter_context((ROOT / "runtime" / (operation + ".lock")).open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if operation == "login":
            auth_lock = locks.enter_context((ROOT / "runtime/auth-operation.lock").open("a"))
            fcntl.flock(auth_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            invalidate_proof()
            (ROOT / "runtime/login-completed").unlink(missing_ok=True)
            if persistent_mode():
                (ROOT / "data/customer-login-completed").unlink(missing_ok=True)
        if operation == "ict" and not (ROOT / "workspace-ict/AGENTS.md").is_file():
            raise RuntimeError("The ICT assistant is not installed.")
        print("Decrypt OS assistant; no shell or trading tools.")
        if persistent_mode():
            print("Sign-in and history use encrypted storage. Stop and lock retains them.")
        else:
            print("Temporary sign-in: stopping removes this sign-in and conversation history.")
        method = choose_auth_method() if operation == "login" else None
        if method == "setup-token":
            try:
                choose_claude_code_helper()
            except RuntimeError as error:
                # Optional convenience only; the setup-token paste prompt below
                # still works with a token made elsewhere even if this failed.
                print(str(error))
        args = interactive_args(operation, method)
        result = subprocess.run([NODE, CLI, "--profile", PROFILE, *args],
                                cwd=ROOT / "workspace", env=environment(), check=False)
        if result.returncode:
            raise RuntimeError("Customer terminal action failed or was cancelled.")
        if operation == "login":
            (ROOT / "runtime/login-completed").touch(mode=0o600)
            if persistent_mode():
                (ROOT / "data/customer-login-completed").touch(mode=0o600)
            request_gateway_refresh()
        return 0

def extract_verified_text(value):
    # Accept only public agent response payload text, never search echoed request/metadata.
    if not isinstance(value, dict):
        return None
    result = value.get("result", value)
    if not isinstance(result, dict):
        return None
    payloads = result.get("payloads")
    if not isinstance(payloads, list) or len(payloads) != 1:
        return None
    item = payloads[0]
    return item.get("text") if isinstance(item, dict) else None

def verify():
    with ExitStack() as locks:
        boundary()
        auth_lock = locks.enter_context((ROOT / "runtime/auth-operation.lock").open("a"))
        fcntl.flock(auth_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if gateway_refresh_state():
            raise RuntimeError("Customer Gateway replacement must complete before verification.")
        if not (ROOT / "runtime/login-completed").exists():
            raise RuntimeError("Customer sign-in is required before verification.")
        invalidate_proof()
        auth = json.loads(cli(["gateway", "call", "models.authStatus", "--json",
                               "--params", '{"agentId":"main"}'], timeout=20))
        if not usable_auth(auth)[0]:
            raise RuntimeError("Customer Claude sign-in is not currently usable.")
        result = json.loads(cli(["agent", "--agent", "main", "--session-id",
                                 "decrypt-os-claude-verification",
                                 "--message", "Reply exactly DECRYPT-OS-CLAUDE-OK",
                                 "--json"], timeout=100))
        if extract_verified_text(result) != "DECRYPT-OS-CLAUDE-OK":
            raise RuntimeError("The fixed model response was not verified.")
        receipt = {"verified": True, "model": MODEL, "runtime": "OpenClaw embedded",
                   "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        atomic_json(ROOT / "runtime/inference-proof.json", receipt)
        return receipt

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("serve", "status", "login", "console", "ict", "verify"):
        return 2
    try:
        op = sys.argv[1]
        if op == "serve":
            serve()
        elif op == "status":
            print(json.dumps(status()))
        elif op == "verify":
            print(json.dumps(verify()))
        else:
            return interactive(op)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was saved.", file=sys.stderr)
        return 1
    except Exception:
        # Never log provider data, raw CLI stdout/stderr, or exception values.
        print("Customer runtime operation failed.", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
