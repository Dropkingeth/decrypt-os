#!/usr/bin/env python3
"""Fixed-operation customer OpenClaw pilot. No live start during candidate tests."""
from __future__ import annotations
import argparse, json, os, pathlib, socket, stat, subprocess, sys, time, pwd

UNIT = "decrypt-os-customer-pilot.service"
PROFILE = "decrypt-customer"
PORT = 19789
ROOT = "/run/decrypt-os-customer-pilot"
# Base-OS default-user placeholder paths; installer/clean_base.py::adapt_runtime
# rewrites both to the target's actual runtime-install locations before the
# service ever ships. Not a hardcoded requirement for external contributors.
PACKAGE = pathlib.Path("/home/customer/.local/lib/node_modules/openclaw")
NODE_ROOT = pathlib.Path("/home/customer/.local/opt/node-v24.19.0-linux-arm64")
HERE = pathlib.Path(__file__).resolve().parent
CUSTOMER = HERE.parent / "customer-workspace"
ICT_PACK = HERE.parent / "ict-assistant"
STATIC_USER = "decrypt-os-customer"
PERSISTENT_DATA = pathlib.Path("/run/decrypt-os-customer-vault/data")
DURABLE_DIRS = ("home", "state", "config", "data", "claude", "workspace", "secrets", "logs")
# After every privileged action the helper leaves a sanitized copy of the status here, world-readable,
# so the Decrypt app can show it without sudo (its sudoers entry needs a password). No paths, tokens,
# account identity or raw error text; the app treats it as stale once the unit restarts.
STATUS_FILE = pathlib.Path("/run/decrypt-os/harness-status.json")
STATUS_SCHEMA = "decrypt.harness-status.v1"
# Per-UID nftables rules (installer/loopback_guard.py) that keep this sandbox off
# trading, bridge, app and browser-debugging ports on loopback.
LOOPBACK_GUARD_UNIT = "decrypt-os-loopback-guard.service"
SAFE_PATH = "/usr/bin:/bin"
DEPENDENCIES = ("/usr/bin/sudo", "/usr/bin/systemd-run", "/usr/bin/systemctl",
                "/usr/bin/nsenter", "/usr/bin/setpriv", "/usr/bin/python3", "/usr/bin/findmnt")

class HarnessError(RuntimeError):
    pass

def run(argv, *, timeout=30, capture=True):
    try:
        return subprocess.run(argv, env={"PATH": SAFE_PATH, "LANG": "C.UTF-8"},
                              stdin=None if not capture else subprocess.DEVNULL,
                              capture_output=capture, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError("Required local operation could not complete.") from exc

def require_success(result, message):
    if result.returncode:
        # Never surface raw subprocess output; provider output can contain secrets.
        raise HarnessError(message)
    return result

def check_tree(root):
    if not root.is_dir() or root.is_symlink():
        raise HarnessError("Required public installation directory is unavailable.")
    resolved = root.resolve()
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = pathlib.Path(base) / name
            if item.is_symlink():
                target = item.resolve()
                if os.path.isabs(os.readlink(item)) or not target.is_relative_to(resolved):
                    raise HarnessError("Public package has an external symlink; refusing the mount.")

def check_public_context(root):
    # DynamicUser cannot use the owner's0600/0700 files even through a read-only bind.
    # Check, never chmod: only reviewed public content should be made readable.
    for item in [root, *root.rglob("*")]:
        try:
            info = item.stat()
        except OSError as exc:
            raise HarnessError("Customer context could not be inspected.") from exc
        required = 0o005 if stat.S_ISDIR(info.st_mode) else 0o004
        if stat.S_IMODE(info.st_mode) & required != required:
            raise HarnessError("Public customer context must be readable and its directories traversable by the isolated service.")

def preflight():
    if sys.platform != "linux":
        raise HarnessError("The customer runtime requires the Omarchy Linux VM.")
    for item in DEPENDENCIES:
        if not pathlib.Path(item).is_file() or not os.access(item, os.X_OK):
            raise HarnessError("A required system isolation dependency is missing.")
    try:
        package = json.loads((PACKAGE / "package.json").read_text())
    except (OSError, ValueError) as exc:
        raise HarnessError("Pinned OpenClaw package is unavailable.") from exc
    if package.get("name") != "openclaw" or package.get("version") != "2026.9.9":
        raise HarnessError("OpenClaw version differs from reviewed 2026.9.9.")
    check_tree(PACKAGE)
    check_tree(NODE_ROOT)
    version = run([str(NODE_ROOT / "bin/node"), "--version"]).stdout.strip()
    if version != "v24.19.0":
        raise HarnessError("Node version differs from reviewed 24.19.0.")
    if not (CUSTOMER / "AGENTS.md").is_file():
        raise HarnessError("Reviewed customer workspace context is missing.")
    check_tree(CUSTOMER)
    check_public_context(CUSTOMER)
    for name in ("decrypt-unslop", "decrypt-frontend-design", "review-skill-imports"):
        if not (CUSTOMER / "skills" / name / "SKILL.md").is_file():
            raise HarnessError("An approved customer skill is missing.")
    if not (ICT_PACK / "workspace/AGENTS.md").is_file() or not (ICT_PACK / "openclaw-agent.json").is_file():
        raise HarnessError("The packaged ICT assistant is missing.")
    check_tree(ICT_PACK)
    check_public_context(ICT_PACK)
    # Token state must remain on tmpfs; a private runtime mount is not optional.
    fs = run(["/usr/bin/findmnt", "--noheadings", "--output", "FSTYPE", "--target", "/run"])
    if fs.returncode or fs.stdout.strip() != "tmpfs":
        raise HarnessError("The runtime directory is not backed by tmpfs.")
    require_success(run(["/usr/bin/sudo", "-n", "true"]),
                    "Passwordless sudo is required for the isolated pilot; no weaker fallback.")
    return {"available": True, "openclawVersion": package["version"], "nodeVersion": version,
            "gatewayPort": PORT, "credentialPersistence": "session_only"}

def storage_operation(operation, capture=True):
    if operation not in ("status", "setup", "unlock", "lock", "prepare"):
        raise HarnessError("Unsupported customer storage action.")
    result = run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-B", "-I",
                  str(HERE / "vault_control.py"), operation],
                 timeout=5 if operation == "status" else (None if not capture else 45),
                 capture=capture)
    require_success(result, "Customer storage action did not complete.")
    return json.loads(result.stdout) if capture else None

def storage_status():
    try:
        value = storage_operation("status")
        return {"configured":value.get("configured") is True,
                "unlocked":value.get("unlocked") is True,
                "error":value.get("error") if isinstance(value.get("error"), str) else None}
    except (HarnessError, ValueError):
        return {"configured":False, "unlocked":False, "error":"Customer storage status is unavailable."}

def unit_properties(persistent=False):
    binds = [f"{PACKAGE}:/opt/decrypt-openclaw",
             f"{NODE_ROOT}:/opt/decrypt-node",
             f"{HERE}:/opt/decrypt-harness",
             f"{CUSTOMER}:/opt/decrypt-customer-workspace",
             f"{ICT_PACK}:/opt/decrypt-ict-assistant"]
    properties = [
        "DynamicUser=no" if persistent else "DynamicUser=yes",
        "User=" + (STATIC_USER if persistent else "decrypt-os-customer-pilot"),
        "RuntimeDirectory=decrypt-os-customer-pilot", "RuntimeDirectoryMode=0700",
        "RuntimeDirectoryPreserve=no", "UMask=0077", "NoNewPrivileges=yes",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
        "ProtectKernelLogs=yes", "ProtectClock=yes", "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes", "LockPersonality=yes", "ProtectProc=invisible",
        "ProcSubset=pid", "CapabilityBoundingSet=", "AmbientCapabilities=",
        "MemoryMax=2G", "CPUQuota=50%", "TasksMax=128", "TimeoutStopSec=20",
        "KillMode=control-group", "Restart=no", "StandardOutput=null",
        "StandardError=null", "TemporaryFileSystem=/run:ro",
        f"BindPaths={ROOT}:{ROOT}",
        "BindReadOnlyPaths=" + " ".join(binds),
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        # No new namespaces (root later enters this unit's namespaces) and only
        # ordinary service syscalls. Blocked calls fail with EPERM instead of
        # killing the process, so libuv can fall back from io_uring.
        "RestrictNamespaces=yes", "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM",
    ]
    if persistent:
        properties = [p for p in properties if not p.startswith("BindPaths=")]
        properties.append("BindPaths=" + " ".join([f"{ROOT}:{ROOT}"] +
            [f"{PERSISTENT_DATA / name}:{ROOT}/{name}" for name in DURABLE_DIRS]))
    return properties

def service_info():
    r = run(["/usr/bin/systemctl", "show", UNIT,
             "--property=LoadState,ActiveState,SubState,MainPID,User,ControlGroup,ActiveEnterTimestampMonotonic"])
    if r.returncode:
        return {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)

def same_namespace(a, b):
    first, second = os.stat(a), os.stat(b)
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)

def verified_namespace_command(operation):
    if operation not in ("status", "login", "console", "ict", "verify"):
        raise HarnessError("Unsupported isolated operation.")
    info = service_info()
    if info.get("ActiveState") != "active" or info.get("User") not in ("decrypt-os-customer-pilot", STATIC_USER):
        raise HarnessError("The isolated customer service is not active.")
    try:
        pid = int(info["MainPID"])
        if pid <= 1:
            raise ValueError
        proc = pathlib.Path("/proc") / str(pid)
        status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines()
                      if ":" in line)
        uid = int(status["Uid"].split()[0])
        gid = int(status["Gid"].split()[0])
        if uid == os.getuid() or gid == 0:
            raise ValueError
        if info.get("User") == STATIC_USER:
            account = pwd.getpwnam(STATIC_USER)
            if uid != account.pw_uid or gid != account.pw_gid or uid == 0:
                raise ValueError
        elif uid < 61184 or uid > 65519:
            raise ValueError
        cgroup = (proc / "cgroup").read_text()
        if info.get("ControlGroup") != "/system.slice/" + UNIT:
            raise ValueError
        if "0::/system.slice/" + UNIT not in cgroup.splitlines():
            raise ValueError
        # Root is about to enter this process's mount namespace and run binaries
        # from it. It must still be owned by the initial user namespace, not one
        # the sandbox created.
        if not same_namespace(proc / "ns/user", pathlib.Path("/proc/1/ns/user")):
            raise ValueError
    except (OSError, ValueError, KeyError) as exc:
        raise HarnessError("Customer process identity could not be verified.") from exc
    # PID reuse is guarded by service identity + cgroup/UID verification; nsenter can still
    # race process exit. Inner operation rechecks env/runtime ownership and fails closed.
    return ["/usr/bin/sudo", "-n", "/usr/bin/nsenter", "--target", str(pid),
            "--mount", "--net", "--ipc", "--uts", "--",
            "/usr/bin/setpriv", "--reuid", str(uid), "--regid", str(gid),
            "--clear-groups", "--no-new-privs", "/usr/bin/env", "-i",
            "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
            "DECRYPT_CUSTOMER_STORAGE=" + ("encrypted" if info.get("User") == STATIC_USER else "session_only"),
            "/usr/bin/python3", "/opt/decrypt-harness/bootstrap.py", operation]

def namespace_command(operation):
    if operation not in ("status", "login", "console", "ict", "verify"):
        raise HarnessError("Unsupported isolated operation.")
    return ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-B", "-I",
            str(HERE / "namespace_entry.py"), operation]

def safe_status():
    info = service_info()
    storage = storage_status()
    base = {"enabled": True, "status": "not_started", "runtime": "OpenClaw",
            "provider": "Claude", "credentialPersistence": "encrypted" if storage["configured"] else "session_only",
            "storage": storage, "gatewayPort": PORT, "model": None, "loginRunning": False,
            "consoleRunning": False, "scopeUpdatedAt": None, "error": None}
    if info.get("ActiveState") in ("activating",):
        base["status"] = "starting"
    elif info.get("ActiveState") == "failed":
        base.update(status="error", error="Customer runtime stopped before it became ready.")
    elif info.get("ActiveState") == "active":
        try:
            result = run(namespace_command("status"), timeout=2)
            require_success(result, "Customer runtime readiness is not verified.")
            child = json.loads(result.stdout)
            for key in ("status", "model", "loginRunning", "consoleRunning",
                        "scopeUpdatedAt", "error", "protocolReady"):
                if key in child:
                    base[key] = child[key]
        except (HarnessError, ValueError):
            base.update(status="starting", error="Waiting for authenticated Gateway readiness.")
    return base

def publish_status():
    """Write STATUS_FILE for the desktop user (see the constant). Never raises."""
    if os.geteuid() != 0:
        return
    try:
        value = safe_status()
    except (HarnessError, OSError, ValueError):
        value = {"status": "error"}
    storage = value.get("storage") if isinstance(value.get("storage"), dict) else {}
    info = service_info()
    out = {"schema": STATUS_SCHEMA, "writtenAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "unitStart": info.get("ActiveEnterTimestampMonotonic") if info.get("ActiveState") == "active" else None,
           "status": value.get("status"), "model": value.get("model"),
           "loginRunning": value.get("loginRunning") is True, "consoleRunning": value.get("consoleRunning") is True,
           "scopeUpdatedAt": value.get("scopeUpdatedAt"), "credentialPersistence": value.get("credentialPersistence"),
           "storage": {"configured": storage.get("configured") is True, "unlocked": storage.get("unlocked") is True,
                       "error": bool(storage.get("error"))}}
    try:
        STATUS_FILE.parent.mkdir(mode=0o755, exist_ok=True)
        os.chmod(STATUS_FILE.parent, 0o755)
        tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o644)
            json.dump(out, handle)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass

def start():
    preflight()
    info = service_info()
    if info.get("ActiveState") in ("active", "activating"):
        return safe_status()
    storage = storage_status()
    if storage.get("error"):
        raise HarnessError("Customer storage could not be verified; no temporary fallback was started.")
    persistent = storage["configured"]
    if persistent:
        if not storage["unlocked"]:
            raise HarnessError("Unlock the saved customer sign-in in the native storage window.")
        storage_operation("prepare")
    # Fail closed, and reload so the rules also cover the static account that
    # storage setup creates after boot.
    require_success(run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", LOOPBACK_GUARD_UNIT]),
                    "The loopback firewall for trading ports is not active; the assistant was not started.")
    if info.get("ActiveState") == "failed":
        require_success(run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reset-failed", UNIT]),
                        "The failed customer runtime could not be reset.")
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise HarnessError("The dedicated customer Gateway port is already in use.") from exc
    argv = ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run", "--unit", UNIT,
            "--service-type=exec"]
    argv += [f"--property={p}" for p in unit_properties(persistent)]
    argv += ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
             "DECRYPT_CUSTOMER_STORAGE=" + ("encrypted" if persistent else "session_only"),
             "/usr/bin/python3", "/opt/decrypt-harness/bootstrap.py", "serve"]
    require_success(run(argv), "The isolated service could not start; no settings were weakened.")
    observed = safe_status()
    if observed["status"] in ("not_started", "error"):
        raise HarnessError("Customer runtime exited during startup; isolation was not weakened.")
    return observed

def stop(lock_storage=True):
    info = service_info()
    if info.get("LoadState") != "not-found":
        require_success(run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", UNIT]),
                        "The customer runtime could not be stopped.")
    info = service_info()
    if info.get("ActiveState") not in ("inactive", "failed", None):
        raise HarnessError("Customer runtime shutdown is not confirmed.")
    # Do not read credentials to verify deletion; pathname existence is sufficient.
    if pathlib.Path(ROOT).exists():
        raise HarnessError("Customer runtime stopped but temporary state removal is not confirmed.")
    if lock_storage:
        state = storage_status()
        if state.get("configured"):
            storage_operation("lock")
    return safe_status()

def interactive(operation):
    if operation not in ("login", "console", "ict", "storage_setup", "storage_unlock") or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise HarnessError("Open this action in the owner's local terminal in workspace 4.")
    if operation in ("storage_setup", "storage_unlock"):
        storage_operation("setup" if operation == "storage_setup" else "unlock", capture=False)
        return
    argv = namespace_command(operation)
    require_success(run(argv, timeout=None, capture=False),
                    "The customer terminal action did not complete.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "start", "status", "login", "console", "ict", "stop", "verify", "storage_setup", "storage_unlock"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            value = preflight()
        elif args.operation == "status":
            value = safe_status()
        elif args.operation == "start":
            value = start()
        elif args.operation == "stop":
            value = stop()
        elif args.operation == "verify":
            r = require_success(run(namespace_command("verify"), timeout=120),
                                "The fixed live verification did not complete.")
            value = json.loads(r.stdout)
        else:
            interactive(args.operation)
            publish_status()
            return 0
        print(json.dumps(value))
        publish_status()
        return 0
    except HarnessError as exc:
        print(json.dumps({"status": "error", "error": str(exc), "gatewayPort": PORT}))
        publish_status()
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
