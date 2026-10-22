#!/usr/bin/env python3
"""Root-only fixed namespace entry; no arbitrary executable or service target."""
import importlib.util, os, pathlib, sys
sys.dont_write_bytecode = True
if os.getuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in ("status", "login", "console", "ict", "verify"):
    raise SystemExit(2)
here = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("customer_runtime", here / "runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
try:
    argv = runtime.verified_namespace_command(sys.argv[1])
    # Joining the exact service cgroup ensures stop/MemoryMax also covers attached TTYs.
    # Do this before entering its read-only cgroup mount namespace and dropping uid.
    cgroup = pathlib.Path("/sys/fs/cgroup/system.slice") / runtime.UNIT / "cgroup.procs"
    with cgroup.open("w") as handle:
        handle.write(str(os.getpid()))
    os.execve(argv[2], argv[2:], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
except Exception:
    print("Customer namespace entry failed.", file=sys.stderr)
    raise SystemExit(1)
