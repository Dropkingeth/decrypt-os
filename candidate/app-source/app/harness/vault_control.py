#!/usr/bin/env python3
"""Fixed root operations for the customer's encrypted local vault.
Secrets enter only through a local masked terminal; never HTTP or argv.
"""
from __future__ import annotations
import contextlib, fcntl, getpass, hashlib, importlib.util, json, os, pathlib
import pwd, signal, sqlite3, stat, subprocess, sys, time
sys.dont_write_bytecode = True
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_storage as storage

USER = "decrypt-os-customer"
UNIT = "decrypt-os-customer-pilot.service"
ROOT = pathlib.Path("/run/decrypt-os-customer-pilot")
DURABLE = ("home", "state", "config", "data", "claude", "workspace", "secrets", "logs")
READY = storage.MOUNT / "customer-ready.json"

def progress(stage, details=None):
    record = {"schema":"decrypt.customer-storage-operation.v1", "stage":stage,
              "at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    if details: record.update(details)
    path = pathlib.Path("/run/decrypt-os-storage-progress.json")
    tmp = path.with_suffix(".new")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,"w") as f:
        json.dump(record,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def command(argv, timeout=40):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout, env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"})
    if result.returncode:
        raise RuntimeError("A fixed customer storage operation failed.")
    return result.stdout

def runtime():
    spec = importlib.util.spec_from_file_location("customer_runtime", HERE / "runtime.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def identity(create=False):
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        if not create:
            raise RuntimeError("Customer storage identity is not installed.")
        command(["/usr/bin/useradd", "--system", "--user-group", "--no-create-home",
                 "--home-dir", "/nonexistent", "--shell", "/usr/bin/nologin", USER])
        account = pwd.getpwnam(USER)
    if account.pw_uid in (0, 1000) or account.pw_gid == 0 or account.pw_dir != "/nonexistent" or account.pw_shell != "/usr/bin/nologin":
        raise RuntimeError("Customer storage identity does not match its installed boundary.")
    import grp
    if any(g.gr_gid != account.pw_gid and USER in g.gr_mem for g in grp.getgrall()):
        raise RuntimeError("Customer storage identity has unexpected groups.")
    return account

# Root never follows a link or writes through a path the sandbox user controls.
# Every walk below holds directory file descriptors, opens each component with
# O_NOFOLLOW, checks fstat() on the opened object and changes ownership with
# fchown() on that descriptor, never on a path.
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
DYNAMIC_UIDS = range(61184, 65520)
MIGRATION_LIMIT = 650 * 1024 * 1024
TRUSTED_UID = 0  # owner of root-controlled parents (tests substitute their own uid)

def open_dir(name, dir_fd=None, uid=None):
    """Open one directory component without following a link. With uid, the
    directory must belong to that uid and must not be group/other writable."""
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise RuntimeError("Linked or missing customer directory refused.") from None
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or (uid is not None and (info.st_uid != uid or info.st_mode & 0o022)):
        os.close(fd)
        raise RuntimeError("Customer directory ownership could not be verified.")
    return fd

def make_dir(parent_fd, name, uid, gid):
    """Create (or reuse) one private directory under a root-held parent fd."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = open_dir(name, parent_fd)
    os.fchown(fd, uid, gid); os.fchmod(fd, 0o700)
    return fd

def trusted_parent(path):
    """The parent of a root-written directory must be a real root-owned directory
    that nobody else can write."""
    info = os.lstat(path.parent)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != TRUSTED_UID or info.st_mode & 0o022:
        raise RuntimeError("Customer storage parent is not root-controlled.")
    return open_dir(str(path.parent))

def safe_directory(path, uid, gid):
    parent = trusted_parent(path)
    try:
        os.close(make_dir(parent, path.name, uid, gid))
    finally:
        os.close(parent)

def process_uid(pid):
    """Real UID of the paused customer supervisor (the temporary DynamicUser)."""
    status = (pathlib.Path("/proc") / str(pid) / "status").read_text()
    for line in status.splitlines():
        if line.startswith("Uid:"):
            uid = int(line.split()[1])
            if uid in DYNAMIC_UIDS:
                return uid
    raise RuntimeError("Customer runtime identity could not be verified.")

def open_runtime_dirs(owner, root=ROOT):
    """Directory fds for ROOT and ROOT/runtime. Both belong to the sandbox UID,
    so they are opened once, without following links, and used only via the fds."""
    parent = trusted_parent(root)
    try:
        root_fd = open_dir(root.name, parent, owner)
    finally:
        os.close(parent)
    try:
        return root_fd, open_dir("runtime", root_fd, owner)
    except BaseException:
        os.close(root_fd)
        raise

def open_runtime_file(runtime_fd, name, owner, gid):
    """Open (or create) a sandbox-owned lock file for flock() only. Refuses links,
    FIFOs, devices and hard links; a file root just created is handed to owner."""
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | FILE_FLAGS, 0o600, dir_fd=runtime_fd)
    except OSError:
        raise RuntimeError("Customer runtime lock refused.") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (owner, 0):
        os.close(fd)
        raise RuntimeError("Customer runtime lock refused.")
    if info.st_uid == 0:
        os.fchown(fd, owner, gid); os.fchmod(fd, 0o600)
    return fd

def request_gateway_refresh(runtime_fd, owner, gid):
    """Create the empty refresh marker. O_EXCL: an existing entry (file or planted
    link) means nothing is written; the marker is never opened through a link."""
    try:
        fd = os.open("gateway-refresh-requested", os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                     0o600, dir_fd=runtime_fd)
    except FileExistsError:
        return False
    try:
        os.fchown(fd, owner, gid); os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return True

def regular_file(dir_fd, name):
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode)

def write_marker(dir_fd, name, uid, gid):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | FILE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Unexpected customer marker.")
        os.fchown(fd, uid, gid); os.fchmod(fd, 0o600)
    finally:
        os.close(fd)

def open_relative(base_fd, parts, uid):
    """Walk directory components from base_fd, each opened with O_NOFOLLOW."""
    fd = os.dup(base_fd)
    try:
        for part in parts:
            child = open_dir(part, fd, uid)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def desktop_account():
    """Desktop user owning the assistant terminals: the sudo caller, else the
    installed instance policy's desktop account. Never a hardcoded name."""
    candidates = [os.environ.get("SUDO_UID", "")]
    try:
        policy = json.loads(pathlib.Path("/etc/decrypt-os/instance-policy.json").read_text())
        candidates.append(str(policy.get("desktopUid", "")))
    except (OSError, ValueError, AttributeError):
        pass
    for value in candidates:
        try:
            uid = int(value)
            if 1000 <= uid < 65534:
                return pwd.getpwuid(uid)
        except (ValueError, KeyError):
            continue
    return None

def prepare():
    state = storage.status()
    if state.get("error") or not state.get("unlocked"):
        raise RuntimeError("Unlock customer storage first.")
    if not READY.is_file() or READY.is_symlink() or READY.stat().st_uid != 0:
        raise RuntimeError("Customer storage migration has not completed.")
    account = identity()
    if storage.DATA.is_symlink() or not storage.DATA.is_dir():
        raise RuntimeError("Customer data directory is unavailable.")
    for name in DURABLE:
        p = storage.DATA / name
        if p.is_symlink() or not p.is_dir() or p.stat().st_uid != account.pw_uid:
            raise RuntimeError("Customer storage ownership could not be verified.")
    return {"uid":account.pw_uid, "gid":account.pw_gid}

def process_state(pid):
    try:
        text = (pathlib.Path("/proc") / str(pid) / "stat").read_text()
        return text[text.rfind(")") + 2:].split()[0]
    except FileNotFoundError:
        return None

def group_pids():
    p = pathlib.Path("/sys/fs/cgroup/system.slice") / UNIT / "cgroup.procs"
    return [int(v) for v in p.read_text().split()] if p.exists() else []

def durable_inventory(source_fd, owner):
    """Directories and files under each DURABLE name, read only through directory
    fds. Every entry must belong to owner; links, special files and hard links are
    refused. Returns (dirs, files) as tuples of path components."""
    dirs, files, total = [], [], 0
    def visit(fd, rel):
        nonlocal total
        for name in sorted(os.listdir(fd)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != owner:
                raise RuntimeError("Customer migration refuses foreign files.")
            if stat.S_ISDIR(info.st_mode):
                child = open_dir(name, fd, owner)
                try:
                    dirs.append(rel + (name,)); visit(child, rel + (name,))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > MIGRATION_LIMIT:
                    raise RuntimeError("Customer data exceeds this vault's migration allowance.")
                files.append((rel + (name,), info.st_size))
            else:
                raise RuntimeError("Customer migration refuses linked or special files.")
    for name in DURABLE:
        try:
            info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Linked customer source refused.")
        fd = open_dir(name, source_fd, owner)
        try:
            dirs.append((name,)); visit(fd, (name,))
        finally:
            os.close(fd)
    return dirs, files

def _hash_fd(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.digest()

def copy_file(source_fd, target_fd, rel, expected_size, owner, account):
    src_dir = open_relative(source_fd, rel[:-1], owner)
    dst_dir = open_relative(target_fd, rel[:-1], account.pw_uid)
    try:
        try:
            src = os.open(rel[-1], os.O_RDONLY | FILE_FLAGS, dir_fd=src_dir)
        except OSError:
            raise RuntimeError("Linked source refused.") from None
        try:
            info = os.fstat(src)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner
                    or info.st_size != expected_size):
                raise RuntimeError("Customer source changed during migration.")
            dst = os.open(rel[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | FILE_FLAGS, 0o600, dir_fd=dst_dir)
            try:
                first = hashlib.sha256()
                while chunk := os.read(src, 1024 * 1024):
                    first.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(dst, view):]
                os.fsync(dst)
                os.fchown(dst, account.pw_uid, account.pw_gid); os.fchmod(dst, 0o600)
                if os.fstat(dst).st_size != expected_size or _hash_fd(dst) != first.digest():
                    raise RuntimeError("Encrypted customer copy verification failed.")
            finally:
                os.close(dst)
        finally:
            os.close(src)
    finally:
        os.close(src_dir); os.close(dst_dir)

def seal_target(fd, uid, gid):
    """Own every copied entry by the storage account, via fds (SQLite may have
    added sidecars). Root-only area; links and special files are still refused."""
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = open_dir(name, fd)
            try:
                os.fchown(child, uid, gid); os.fchmod(child, 0o700)
                seal_target(child, uid, gid)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            child = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=fd)
            try:
                os.fchown(child, uid, gid); os.fchmod(child, 0o600)
            finally:
                os.close(child)
        else:
            raise RuntimeError("Unexpected file after encrypted database verification.")

def copy_durable(source_fd, target, account, owner):
    """Copy the paused temporary runtime (source_fd = ROOT, owned by owner) into
    the root-only vault data directory."""
    dirs, files = durable_inventory(source_fd, owner)
    parent = trusted_parent(target)
    try:
        target_fd = open_dir(target.name, parent, TRUSTED_UID)
    finally:
        os.close(parent)
    try:
        for name in DURABLE:
            os.close(make_dir(target_fd, name, account.pw_uid, account.pw_gid))
        for rel in dirs:
            if len(rel) > 1:
                parent_fd = open_relative(target_fd, rel[:-1], account.pw_uid)
                try:
                    os.close(make_dir(parent_fd, rel[-1], account.pw_uid, account.pw_gid))
                finally:
                    os.close(parent_fd)
        for rel, expected_size in files:
            copy_file(source_fd, target_fd, rel, expected_size, owner, account)
        checked = 0
        for rel, _ in files:
            if rel[-1].endswith((".sqlite", ".db")):
                path = target.joinpath(*rel)
                with path.open("rb") as f: header = f.read(16)
                if header == b"SQLite format 3\x00":
                    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
                    try:
                        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise RuntimeError("Encrypted database recovery check failed.")
                        checked += 1
                    finally: conn.close()
        seal_target(target_fd, account.pw_uid, account.pw_gid)
        return {"filesVerified":len(files), "databasesChecked":checked}
    finally:
        os.close(target_fd)

def mark_ready(account):
    record = {"schema":"decrypt.customer-vault.v1", "user":USER, "uid":account.pw_uid}
    fd = os.open(READY, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f); f.flush(); os.fsync(f.fileno())
    command(["/usr/bin/sync", "-f", str(storage.MOUNT)])

def migrate():
    rt = runtime()
    info = rt.service_info()
    account = identity(create=True)
    safe_directory(storage.DATA, 0, 0)
    if any(storage.DATA.iterdir()) or READY.exists():
        raise RuntimeError("Customer storage is not empty; refusing replacement.")
    if info.get("ActiveState") != "active":
        for name in DURABLE: safe_directory(storage.DATA / name, account.pw_uid, account.pw_gid)
        mark_ready(account)
        return {"filesVerified":0, "databasesChecked":0}
    if info.get("User") != "decrypt-os-customer-pilot":
        raise RuntimeError("Only the temporary customer runtime can be migrated.")
    # Verify cgroup and isolated uid using the same boundary as native actions.
    rt.verified_namespace_command("status")
    supervisor = int(info["MainPID"])
    owner = process_uid(supervisor)
    root_fd, runtime_fd = open_runtime_dirs(owner)
    try:
        return _migrate_locked(rt, account, supervisor, owner, root_fd, runtime_fd)
    finally:
        os.close(runtime_fd); os.close(root_fd)

def _migrate_locked(rt, account, supervisor, owner, root_fd, runtime_fd):
    paused = False
    copied = False
    auth_lock = open_runtime_file(runtime_fd, "auth-operation.lock", owner, os.fstat(runtime_fd).st_gid)
    with contextlib.closing(os.fdopen(auth_lock, "rb", buffering=0)):
        fcntl.flock(auth_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Closing the assistant TTYs prevents concurrent console requests during migration.
        desktop = desktop_account()
        if desktop is not None:
            subprocess.run(["/usr/bin/runuser", "-u", desktop.pw_name, "--", "/usr/bin/env",
                            f"XDG_RUNTIME_DIR=/run/user/{desktop.pw_uid}",
                            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{desktop.pw_uid}/bus",
                            "/usr/bin/systemctl", "--user", "stop",
                            "decrypt-os-assistant.service", "decrypt-os-ict-assistant.service"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        try:
            os.kill(supervisor, signal.SIGSTOP); paused = True
            deadline = time.monotonic()+5
            while process_state(supervisor) != "T":
                if time.monotonic()>deadline: raise RuntimeError("Customer supervisor did not pause.")
                time.sleep(.1)
            # Only verified members of this customer cgroup; no other service PID targets.
            for pid in group_pids():
                if pid != supervisor and process_state(pid) not in (None, "Z"):
                    os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic()+35
            while any(p != supervisor and process_state(p) not in (None,"Z") for p in group_pids()):
                if time.monotonic()>deadline:
                    raise RuntimeError("Customer writers did not stop; original sign-in retained.")
                time.sleep(.2)
            copied = copy_durable(root_fd, storage.DATA, account, owner)
            if regular_file(runtime_fd, "login-completed"):
                parent = trusted_parent(storage.DATA)
                try:
                    data_fd = open_relative(parent, (storage.DATA.name, "data"), None)
                finally:
                    os.close(parent)
                try:
                    write_marker(data_fd, "customer-login-completed", account.pw_uid, account.pw_gid)
                finally:
                    os.close(data_fd)
            mark_ready(account)
            # Ciphertext copy and database checks passed BEFORE temporary state destruction.
            # Resume only the paused supervisor for systemd's normal stop; writers are gone.
            os.kill(supervisor, signal.SIGCONT); paused = False
            rt.stop(lock_storage=False)
            return copied
        except Exception:
            if paused:
                os.kill(supervisor, signal.SIGCONT)
            # Preserve tmpfs source and request replacement of the terminated Gateway.
            # Only through the verified runtime directory fd; never a path lookup.
            if rt.service_info().get("ActiveState") == "active":
                request_gateway_refresh(runtime_fd, owner, os.fstat(runtime_fd).st_gid)
            raise

def wait_ready(rt):
    deadline = time.monotonic() + 110
    while time.monotonic() < deadline:
        observed = rt.safe_status()
        if observed.get("protocolReady") and observed.get("status") in ("ready", "needs_login", "connected"):
            return observed
        if observed.get("status") in ("error", "not_started"):
            raise RuntimeError("Customer runtime needs attention after storage unlock.")
        time.sleep(2)
    raise RuntimeError("Customer readiness did not complete after storage unlock.")

def password(create=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Use the owner's local masked storage terminal.")
    value = getpass.getpass("Storage passphrase: ")
    if len(value) < 12:
        raise RuntimeError("Use a storage passphrase with at least 12 characters.")
    if create and value != getpass.getpass("Repeat storage passphrase: "):
        raise RuntimeError("Passphrases did not match. Nothing was migrated.")
    return value.encode("utf-8")

def main():
    if os.getuid()!=0 or len(sys.argv)!=2 or sys.argv[1] not in ("status","setup","unlock","lock","prepare"):
        return 2
    try:
        op=sys.argv[1]
        if op=="status":
            print(json.dumps(storage.status()));return 0
        if op=="prepare":
            print(json.dumps(prepare()));return 0
        operation_lock = pathlib.Path("/run/decrypt-os-storage-operation.lock")
        fd = os.open(operation_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if op=="lock":
            if runtime().service_info().get("ActiveState") in ("active","activating","deactivating"):
                raise RuntimeError("Stop the customer runtime before locking storage.")
            storage.lock();print(json.dumps(storage.status()));return 0
        print("Decrypt OS encrypted sign-in storage")
        print("Choose a separate passphrase. It is not saved. Unlock once after reboot.")
        print("Losing this passphrase means the saved sign-in and history cannot be recovered.")
        progress("awaiting_passphrase", {"operation":op})
        secret=password(create=op=="setup")
        try:
            if op=="setup":
                volume = storage.create(secret)
                if not volume.get("unlocked") or volume.get("error"):
                    raise RuntimeError("New encrypted volume did not pass its readiness check.")
                progress("volume_created")
                summary=migrate()
                progress("copy_verified",summary)
                print("Encrypted customer copy verified.")
            else:
                volume = storage.unlock(secret)
                if not volume.get("unlocked") or volume.get("error"):
                    raise RuntimeError("Encrypted volume did not unlock.")
            prepare()
            rt = runtime()
            rt.start()
            observed = wait_ready(rt)
            progress("runtime_ready",{"responseVerified":False})
            if op == "setup":
                print("Checking saved data across a complete stop, lock and unlock.")
                rt.stop(lock_storage=False)
                storage.lock()
                locked = storage.status()
                progress("locked_for_restore_test")
                if locked.get("unlocked") or locked.get("error"):
                    raise RuntimeError("Storage locking was not confirmed.")
                volume = storage.unlock(secret)
                if not volume.get("unlocked") or volume.get("error"):
                    raise RuntimeError("Storage recovery was not confirmed.")
                prepare()
                rt.start()
                observed = wait_ready(rt)
                progress("restored",{"lockUnlockPassed":True})
            if observed.get("status") in ("ready", "connected"):
                print("Checking one small model response through your saved account.")
                result = rt.require_success(rt.run(rt.namespace_command("verify"), timeout=120),
                                             "Saved-account response check did not complete.")
                proof = json.loads(result.stdout)
                if proof.get("verified") is not True:
                    raise RuntimeError("Saved-account response was not verified.")
                progress("model_verified", {"responseVerified":True, "verifiedAt":proof.get("verifiedAt")})
                print("Saved sign-in restored and a real response passed.")
            else:
                print("Storage is ready. Complete Claude sign-in in Connections.")
            print("Storage remains unlocked while the assistant runs. Use Stop and lock when done.")
            progress("done",{"responseVerified":observed.get("status") in ("ready","connected"),
                             "lockUnlockPassed":op=="setup"})
            return 0
        finally:
            del secret
    except Exception as exc:
        if len(sys.argv)==2 and sys.argv[1] in ("setup","unlock"):
            try: progress("failed",{"operation":sys.argv[1]})
            except Exception: pass
        # Deliberately no raw exceptions, subprocess output, paths, or credential values.
        print("Customer storage action did not complete. Existing data was not overwritten.",file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())

