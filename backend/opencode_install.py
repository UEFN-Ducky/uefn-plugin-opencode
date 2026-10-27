"""Ducky's own pinned OpenCode, so the agent works out of the box.

The binary comes from npm's registry (the same package `npm i @opencode/cli` installs),
is checked against the SHA-512 pinned here, and lives in Ducky's app data, never on
PATH: no Node, no npm, no installer, and the person's own OpenCode stays untouched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

OPENCODE_VERSION = "2.2.0"
# npm registry dist.integrity per platform package (registry.npmjs.org/@opencode/<pkg>/2.2.0).
_INTEGRITY = {
    "cli-windows-x64": "sha512-S/eVkzMaUqoiBmeLHgEdr4b3yIQjpfDzLo4CFSSB3skB0UH+lduqqjTnmvAvDeli4fEjcNbZmd/8YwkoKnFkBA==",
    "cli-windows-x64-baseline": "sha512-pHacmWjCbm0BrCjVBJhPiPot579oCntWCsQcIIFUKBRJNZYiabxMxYRV6M9rXdq87tSdKOt5MQVg56cnajvBBw==",
    "cli-windows-arm64": "sha512-IK9FGVGMC0akI2/NHWuwiUvMmaSDTTjjl0S5TUdgxBFQEfCvg/wL1S23BKPq4KDZuI4rfk7w36ajcp2CCZ9IGw==",
    "cli-darwin-arm64": "sha512-y3q/sghk/xCengTlv1pH2+5t9j0cuxXcEAY9yvyM7loKLQIYE1eSLEkDacCAVNyfnATd3D2JpueFSEB/i6A0Qg==",
    "cli-darwin-x64": "sha512-oK46aktdkYIAlkFs72I4JNgFS+cEgrks/HAykoKXwcZTW3XlGY1GlhTfdp55XRw9XTfTCqHvtCtvD9IgCnGhQA==",
    "cli-darwin-x64-baseline": "sha512-91JnM4NW91kufSeYbdScwsrWQIX1w87WRG2Gy9aG27/6DhmPwCqJZlOf1yZeER557QUs0xrtaSpNXa4T7hczmA==",
    "cli-linux-x64": "sha512-XTGFTiZTgugps6rgTXp4mPjOM8klsa5uHn/Shcms2o3k1bjSA3Sgl6O4yJ48y18WWxn9My5Pxt7JKcLID7LOHg==",
    "cli-linux-x64-baseline": "sha512-ComUyKWDToJMBEI1IdxiCGSSdJ0BhXYPoOkb+o2BozDERtTg51UWiomfkiwMIJ3qgR1p/9Itdt/c8oVINUKNmg==",
    "cli-linux-arm64": "sha512-NCCltb8h/8WL30X9o2KTm3UFX0lmfk48lG8tglXJN9moscxeB7w2qOmfHVa32iFngk0iVAKH4xRPlBc88Y0qlw==",
}
_REGISTRY = "https://registry.npmjs.org/@opencode"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_lock = threading.Lock()
_state: dict[str, Any] = {"busy": False, "message": "", "error": ""}


def exe_name() -> str:
    return "opencode.exe" if os.name == "nt" else "opencode"


def tools_root() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "opencode"


def owned_bin() -> str:
    path = tools_root() / OPENCODE_VERSION / exe_name()
    return str(path) if path.is_file() else ""


def hidden_run_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "startupinfo": info}


def read_version(binary: str, timeout_s: float = 20.0) -> str:
    if not binary:
        return ""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout_s,
                              encoding="utf-8", errors="replace", **hidden_run_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = _VERSION_RE.search((proc.stdout or "") + " " + (proc.stderr or ""))
    return ".".join(m.groups()) if m else ""


def is_v2(version: str) -> bool:
    m = _VERSION_RE.search(version or "")
    return bool(m) and int(m.group(1)) >= 2


def resolve_bin(override: str = "") -> str:
    """The person's override, else Ducky's own copy, else an OpenCode 2 on PATH."""
    from backend.agent.coding_agents.base import which_cli

    if (override or "").strip():
        return which_cli("opencode", override)
    owned = owned_bin()
    if owned:
        return owned
    found = shutil.which("opencode")
    return found if found and is_v2(read_version(found)) else ""


def candidates(system: str = "", machine: str = "") -> list[str]:
    """Platform packages to try in order (x64 falls back to the no-AVX2 baseline build)."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    osname = {"windows": "windows", "darwin": "darwin", "linux": "linux"}.get(system, system)
    base = f"cli-{osname}-{arch}"
    names = [base, f"{base}-baseline"] if arch == "x64" else [base]
    if osname == "windows" and arch == "arm64":
        names += ["cli-windows-x64-baseline", "cli-windows-x64"]  # emulated x64 when native fails
    return [n for n in names if n in _INTEGRITY]


def _verify(path: Path, integrity: str) -> bool:
    algo, _, expected = integrity.partition("-")
    digest = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii") == expected


def _download(url: str, dest: Path, on_progress: Callable[[str], None] | None) -> None:
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            out.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else -1
            if on_progress and pct >= 0 and pct // 10 != shown:
                shown = pct // 10
                on_progress(f"Downloading OpenCode {OPENCODE_VERSION}… {pct}%")


def _extract_bin(tgz: Path, target: Path) -> None:
    want = f"package/bin/{exe_name()}"
    with tarfile.open(tgz, "r:gz") as tar:
        member = tar.getmember(want)
        src = tar.extractfile(member)
        if src is None:
            raise OSError("binary missing from package")
        tmp = target.with_suffix(".part")
        with src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)


def install(on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Download, verify and unpack the pinned OpenCode into Ducky's app data."""
    with _lock:
        if owned_bin():
            return {"ok": True, "cli_path": owned_bin(), "version": OPENCODE_VERSION}
        _state.update(busy=True, error="", message=f"Installing OpenCode {OPENCODE_VERSION}…")
        dest_dir = tools_root() / OPENCODE_VERSION
        errors: list[str] = []
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for name in candidates():
                url = f"{_REGISTRY}/{name}/-/{name}-{OPENCODE_VERSION}.tgz"
                with tempfile.TemporaryDirectory(dir=tools_root()) as tmp:
                    tgz = Path(tmp) / f"{name}.tgz"
                    try:
                        _download(url, tgz, on_progress)
                    except OSError as exc:
                        errors.append(f"{name}: download failed ({exc})")
                        continue
                    if not _verify(tgz, _INTEGRITY[name]):
                        errors.append(f"{name}: checksum mismatch, not installed")
                        continue
                    target = dest_dir / exe_name()
                    try:
                        _extract_bin(tgz, target)
                    except (OSError, KeyError, tarfile.TarError) as exc:
                        errors.append(f"{name}: unpack failed ({exc})")
                        continue
                    if read_version(str(target)) == OPENCODE_VERSION:
                        _state.update(message=f"OpenCode {OPENCODE_VERSION}")
                        (tools_root() / "installed.json").write_text(
                            json.dumps({"version": OPENCODE_VERSION, "package": name, "at": time.time()}),
                            encoding="utf-8")
                        return {"ok": True, "cli_path": str(target), "version": OPENCODE_VERSION}
                    target.unlink(missing_ok=True)  # this CPU cannot run that build; try the next
                    errors.append(f"{name}: does not run on this PC")
            error = "Could not install OpenCode: " + "; ".join(errors or ["no build for this PC"])
            _state.update(error=error, message="")
            return {"ok": False, "error": error}
        finally:
            _state["busy"] = False


def status_text() -> str:
    if _state.get("busy"):
        return str(_state.get("message") or "Installing OpenCode…")
    return str(_state.get("error") or "")


def schedule_install_on_load() -> None:
    """First enable installs in the background, so the first message does not wait."""
    if os.environ.get("PYTEST_CURRENT_TEST") or owned_bin():
        return
    try:
        if resolve_bin():
            return
    except Exception:
        return
    threading.Thread(target=install, name="opencode-install", daemon=True).start()
