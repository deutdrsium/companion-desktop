"""Server-side settings reached through locally configured commands.

The public client knows only the contract (argv, stdin, last stdout line is JSON); the
actual commands, hosts and scripts live in the git-ignored local config.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _last_json(stdout):
    lines = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("no output")
    return json.loads(lines[-1])


class RhythmService:
    """Desktop-only reply timing (debounce). Command contract:

    ``argv`` elements may contain ``{args}``, replaced by ``--apply --min-ms A --max-ms B --total-ms C``,
    ``--reset`` or nothing (read). ``stdin_file`` (optional) is piped to the command. The last
    stdout line is JSON: ``{"scope": "desktop-window", "applied": bool, "before": {...}, "after": {...}}``
    with ``private_debounce_{min,max,total}_ms`` keys.
    """

    def __init__(self, spec, runner=subprocess.run):
        self.argv = list(spec["argv"])
        self.stdin_file = spec.get("stdin_file") or None
        self.runner = runner

    def _call(self, args, change):
        argv = [part.replace("{args}", args).strip() if "{args}" in part else part for part in self.argv]
        stdin = Path(self.stdin_file).read_bytes() if self.stdin_file else None
        result = self.runner(argv, input=stdin, capture_output=True, timeout=90, creationflags=CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("rhythm command failed")
        payload = _last_json(result.stdout)
        if payload.get("scope") != "desktop-window" or (change and not payload.get("applied")):
            raise RuntimeError("rhythm command refused")
        return payload.get("after" if change else "before", {})

    def load(self):
        return self._call("", False)

    def save(self, values):
        a, b, c = values
        if any(type(v) is not int for v in values) or not 10 <= a <= b <= c <= 1_200_000:
            raise ValueError("invalid timing")
        return self._call(f"--apply --min-ms {a} --max-ms {b} --total-ms {c}", True)

    def reset(self):
        return self._call("--reset", True)


class VoiceKeyService:
    """Hands a voice-provider key to the configured setup command over stdin; keeps nothing.

    The last stdout line must be JSON ``{"ok": true}``.
    """

    def __init__(self, spec, runner=subprocess.run):
        self.argv = list(spec["argv"])
        self.runner = runner

    def submit(self, key):
        key = key.strip()
        if not key or len(key) > 4096 or "\n" in key or "\r" in key:
            return False
        try:
            result = self.runner(self.argv, input=key.encode("utf-8"), capture_output=True, timeout=60,
                                 creationflags=CREATE_NO_WINDOW)
            return result.returncode == 0 and _last_json(result.stdout).get("ok") is True
        except Exception:
            return False


def start_helpers(helpers, popen=subprocess.Popen, process_iter=None):
    """Start optional background helpers from local config, once each.

    Each helper: ``{"argv": [...], "cwd": "", "unique": true}``. With ``unique`` the helper is
    skipped when a process already runs with the same script argument.
    """
    started = []
    for spec in helpers or []:
        argv = [os.path.expandvars(a) for a in spec.get("argv", [])]
        if not argv or not Path(argv[-1]).exists() and spec.get("require_exists", True):
            continue
        if spec.get("unique") and _running(argv[-1], process_iter):
            continue
        if argv[0] == "{python}":
            argv[0] = sys.executable.replace("python.exe", "pythonw.exe") if sys.platform == "win32" else sys.executable
        popen(argv, cwd=spec.get("cwd") or None, creationflags=CREATE_NO_WINDOW,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started.append(argv)
    return started


def _running(script, process_iter=None):
    try:
        import psutil
    except ImportError:
        return False
    wanted = os.path.normcase(os.path.normpath(script))
    for process in (process_iter or psutil.process_iter)(["name"]):
        try:
            if any(os.path.normcase(os.path.normpath(arg)) == wanted for arg in process.cmdline()):
                return True
        except Exception:
            continue
    return False
