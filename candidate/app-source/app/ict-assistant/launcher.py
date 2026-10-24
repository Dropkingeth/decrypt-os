#!/usr/bin/env python3
"""Workspace 4 launcher for the ICT assistant and the Claude connection steps.

Default: shows the New York session clock, then walks the user to the ICT
assistant: start the runtime, unlock saved sign-in or connect Claude when needed.
--connect: the same walk without the clock, ending once Claude is connected (used
by the Decrypt app's "Connect Claude"). --verify / --stop: one privileged step.
Runs only the fixed customer runtime actions; on an installed system each one
goes through sudo, which asks for the password in this terminal. Nothing starts
until the user presses Enter.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ict_clock  # noqa: E402

INSTALLED_CONTROL = Path("/usr/local/lib/decrypt-os/installer/customer_control.py")
DEV_RUNTIME = HERE.parent / "harness/runtime.py"
ACTIONS = ("status", "start", "login", "ict", "storage_unlock", "verify", "stop")


def command(action):
    if action not in ACTIONS:
        raise ValueError("unsupported action")
    if INSTALLED_CONTROL.is_file():
        # Matches the installed sudoers rule exactly; sudo may ask for the password.
        return ["/usr/bin/sudo", "/usr/bin/python3", "-B", "-I", str(INSTALLED_CONTROL), action]
    return ["/usr/bin/python3", str(DEV_RUNTIME), action]


def status(run=subprocess.run):
    # Installed: sudo asks for the password on this terminal, and a person may take a while to
    # type it, so no timeout there (Ctrl+C still works). The dev path keeps a bound.
    try:
        reply = run(command("status"), stdin=None, stdout=subprocess.PIPE, text=True,
                    timeout=None if INSTALLED_CONTROL.is_file() else 60, check=False)
        value = json.loads(reply.stdout or "{}")
        return value if isinstance(value, dict) else {"status": "error"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"status": "error"}


def ask(question, read=input):
    try:
        return read(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def next_step(state):
    """Return the action the launcher should offer for a runtime status."""
    storage = state.get("storage") if isinstance(state.get("storage"), dict) else {}
    value = state.get("status")
    if value in ("ready", "connected"):
        return "ict"
    if storage.get("configured") and not storage.get("unlocked"):
        return "storage_unlock"
    if value == "not_started":
        return "start"
    if value == "needs_login":
        return "login"
    if value == "starting":
        return "wait"
    return None


PROMPTS = {
    "start": "Your assistant runtime is off. Start it?",
    "storage_unlock": "Your saved Claude sign-in is locked. Unlock it? (asks for your passphrase)",
    "login": "No Claude account yet. Connect one now? (subscription token or API key)",
}
MODES = {"--connect": "connect", "--verify": "verify", "--stop": "stop"}
WAIT_LIMIT_S = 240   # how long to wait for a starting runtime before giving up


def one_step(step, read=input, run=subprocess.run):
    """--verify / --stop: run one privileged step and say what happened in plain words."""
    if step == "verify":
        print("Check the Claude connection  |  sends one short test through your Claude account.")
        print("It counts toward your plan or API usage.\n")
        if not ask("Run the check now?", read):
            print("Nothing was sent.")
            return 0
        reply = run(command("verify"), stdin=None, stdout=subprocess.PIPE, text=True, check=False)
        ok = reply.returncode == 0
        print("Claude answered. The connection is verified." if ok else
              "No verified answer. Check the sign-in in Decrypt OS (Super+1) > Connections > Claude account.")
    else:
        print("Stop the assistant  |  stops the runtime; saved sign-in (if any) is locked again.\n")
        if not ask("Stop it now?", read):
            print("The assistant keeps running.")
            return 0
        reply = run(command("stop"), stdin=None, stdout=subprocess.PIPE, text=True, check=False)
        ok = reply.returncode == 0
        print("The assistant is stopped." if ok else "The stop did not finish. See Decrypt OS > Connections for details.")
    read("Press Enter to close. ")
    return 0 if ok else 1


def main(argv=None, read=input, run=subprocess.run, call=subprocess.call):
    argv = sys.argv[1:] if argv is None else argv
    mode = next((MODES[flag] for flag in argv if flag in MODES), "ict")
    if mode in ("verify", "stop"):
        return one_step(mode, read, run)
    if mode == "connect":
        print("Connect Claude  |  your assistants run on your own Claude account.")
        print("Each step asks for your password here. Nothing is sent anywhere else.\n")
        try:
            read("Press Enter to continue. ")
        except (EOFError, KeyboardInterrupt):
            return 0
    else:
        print("ICT assistant  |  your personal ICT mentor, on your own Claude account.")
        print("Education only: it cannot trade, place orders or see your accounts.\n")
        print(ict_clock.describe(ict_clock.snapshot()))
        if "--clock" in argv:
            return 0
        try:
            read("\nPress Enter to open the ICT assistant. (Ctrl+C leaves this window open. The times above don't update.) ")
        except (EOFError, KeyboardInterrupt):
            return 0
    waited = 0
    while waited <= WAIT_LIMIT_S:
        state = status(run)
        step = next_step(state)
        if step == "ict" and mode == "connect":
            if state.get("status") == "connected":
                print("Claude is connected and verified.")
            elif ask("Signed in. Check the connection now? (one short test; counts toward your usage)", read):
                ok = run(command("verify"), stdin=None, stdout=subprocess.PIPE, text=True, check=False).returncode == 0
                print("Claude answered. The connection is verified." if ok else
                      "No verified answer yet. You can retry from Decrypt OS > Connections > Claude account.")
            else:
                print("Signed in. You can check the connection later from Decrypt OS > Connections.")
            print("The ICT assistant is on workspace 4 (Super+4); the Decrypt app shows the new status within a few seconds.")
            read("Press Enter to close. ")
            return 0
        if step == "ict":
            return call(command("ict"))
        if step == "wait":
            if waited == 0:
                print("Starting the assistant runtime. The first start can take a minute or two.")
            elif waited % 30 == 0:
                print("Still starting (%d s)..." % waited)
            time.sleep(5)
            waited += 5
            continue
        if step is None:
            print("The assistant is not available right now.")
            print("Open Decrypt OS on workspace 1 (Super+1) > Connections > Claude account for details.")
            read("Press Enter to close. ")
            return 1
        if not ask(PROMPTS[step], read):
            print("No problem. Come back any time with Super+4.")
            return 0
        if step == "start":
            # Start prints a JSON status; keep the terminal readable.
            done = run(command(step), stdin=None, stdout=subprocess.PIPE, text=True, check=False).returncode == 0
        else:
            done = call(command(step)) == 0
        if not done:
            print("That step did not finish. Open Decrypt OS (Super+1) > Connections > Claude account to see why.")
            read("Press Enter to close. ")
            return 1
    print("The assistant runtime did not become ready in %d minutes." % (WAIT_LIMIT_S // 60))
    print("Open Decrypt OS (Super+1) > Connections > Claude account, stop the assistant there and try again.")
    read("Press Enter to close. ")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
