"""
Talks to Kaggle by running the official Kaggle CLI (bundled as wheels inside this extension) in a
SUBPROCESS, one process per call, with that account's credentials in the process environment only.
Nothing is written to ~/.kaggle and Blender's own environment is never changed, so several accounts
can be used at the same time without mixing tokens.

No `bpy` import here: this module is plain Python and is tested outside Blender.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CLI_SNIPPET = ("import sys, warnings; warnings.simplefilter('ignore'); sys.argv[0] = 'kaggle'; "
               "from kaggle.cli import main; main()")
# argv prefix used to start the CLI. Tests replace it with a fake CLI.
CLI_ARGV: list[str] = [sys.executable, "-c", CLI_SNIPPET]

_ENV_KEEP = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
             "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
             "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"}

_work_dir: Path = Path(os.environ.get("TEMP") or "/tmp") / "kaggle_render"


def configure(work_dir: Path) -> None:
    global _work_dir
    _work_dir = Path(work_dir)


class KaggleError(Exception):
    pass


class AuthError(KaggleError):
    pass


@dataclass(frozen=True)
class Creds:
    username: str   # may be "" for a KGAT_ token until verify() discovers it
    secret: str
    kind: str       # 'token' -> KGAT_... access token | 'legacy' -> username + old API key


def _packages_dir() -> str | None:
    """Folder that holds the bundled `kaggle` package (Blender installs the wheels there)."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("kaggle")
    except Exception:  # noqa: BLE001
        return None
    if spec and spec.submodule_search_locations:
        return str(Path(list(spec.submodule_search_locations)[0]).parent)
    return None


def build_env(creds: Creds) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    if creds.kind == "token":
        env["KAGGLE_API_TOKEN"] = creds.secret
    else:
        env["KAGGLE_USERNAME"] = creds.username
        env["KAGGLE_KEY"] = creds.secret
    # The CLI also reads ~/.kaggle and cached logins from the real home. An invalid token must NOT silently
    # fall back to another account of this PC, so the subprocess gets an empty fake HOME.
    home = _work_dir / "cli_home"
    cfg = _work_dir / "cli_config"
    home.mkdir(parents=True, exist_ok=True)
    cfg.mkdir(parents=True, exist_ok=True)
    env["HOME"] = env["USERPROFILE"] = env["XDG_CONFIG_HOME"] = str(home)
    env["KAGGLE_CONFIG_DIR"] = str(cfg)
    env["PYTHONIOENCODING"] = "utf-8"
    pkgs = _packages_dir()
    if pkgs:
        env["PYTHONPATH"] = pkgs + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    return env


def run(creds: Creds, args: list[str], cwd: Path | None = None, timeout: float | None = None) -> tuple[int, str]:
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flashing on Windows
    try:
        p = subprocess.run([*CLI_ARGV, *args], env=build_env(creds), cwd=str(cwd) if cwd else None,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        raise KaggleError(f"Kaggle did not answer within {int(timeout or 0)} s.")
    except OSError as e:
        raise KaggleError(f"Could not start the Kaggle client: {e}")
    text = p.stdout.decode("utf-8", errors="replace")
    if len(creds.secret) >= 8:  # never echo a token back (real ones are long; tiny test values would mangle text)
        text = text.replace(creds.secret, "***")
    return p.returncode or 0, text.strip()


# The CLI echoes uploaded file names, so a file called "error.blend" must not look like a failure.
_PROGRESS_LINE = re.compile(r"^\s*(starting upload|uploading|upload successful|skipping)|%\|", re.I)
_URL = re.compile(r"https?://\S+")


def _output_has_error(out: str) -> bool:
    lines = (_URL.sub("", l) for l in out.splitlines() if not _PROGRESS_LINE.search(l))
    return "error" in "\n".join(lines).lower()


def _auth_words(low: str) -> bool:
    return any(w in low for w in ("401", "403", "unauthorized", "forbidden", "invalid", "authentication required"))


def verify(creds: Creds) -> str:
    """Authenticated call. Returns the REAL Kaggle username that owns these credentials."""
    rc, out = run(creds, ["datasets", "list", "--mine"], timeout=90)
    if rc != 0:
        if _auth_words(out.lower()):
            raise AuthError(out[-300:])
        raise KaggleError(out[-300:])
    rc, out = run(creds, ["config", "view"], timeout=60)
    m = re.search(r"^- username:\s*(\S+)", out, re.MULTILINE)
    if rc != 0 or not m or m.group(1) == "None":
        raise AuthError("Could not find out the user name that belongs to this token.")
    return m.group(1)


def create_dataset(creds: Creds, folder: Path, slug: str, title: str) -> None:
    meta = {"title": title, "id": f"{creds.username}/{slug}", "licenses": [{"name": "CC0-1.0"}]}
    (folder / "dataset-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    # very long timeout instead of none: a stalled connection must not hang the upload forever (it is retried)
    rc, out = run(creds, ["datasets", "create", "-p", str(folder)], timeout=6 * 3600)
    if rc != 0 or _output_has_error(out):
        raise KaggleError(out[-400:] or "Dataset upload failed.")


def wait_dataset_ready(creds: Creds, slug: str, timeout: int = 120, sleep=time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            rc, out = run(creds, ["datasets", "status", f"{creds.username}/{slug}"], timeout=90)
        except KaggleError:
            rc, out = 1, ""
        low = out.lower()
        if rc == 0 and re.search(r"\bready\b", low) and not re.search(r"\bnot\s+(yet\s+)?ready\b", low):
            return True
        if rc == 0 and ("error" in low or "fail" in low):
            raise KaggleError("Kaggle could not process the file: " + out[-200:])
        if time.monotonic() >= deadline:
            return False
        sleep(10)


def dataset_exists(creds: Creds, slug: str) -> bool:
    """True when the dataset is already on Kaggle (e.g. an upload that finished right before a crash)."""
    try:
        rc, out = run(creds, ["datasets", "status", f"{creds.username}/{slug}"], timeout=90)
    except KaggleError:
        return False
    low = out.lower()
    return rc == 0 and not any(w in low for w in ("not found", "404", "does not exist", "forbidden"))


def delete_dataset(creds: Creds, slug: str) -> None:
    rc, out = run(creds, ["datasets", "delete", "-y", f"{creds.username}/{slug}"], timeout=120)
    low = out.lower()
    if rc != 0 and not any(w in low for w in ("not found", "404", "does not exist")):
        raise KaggleError(out[-300:] or "Could not delete the dataset.")


_STATUS_RE = re.compile(r'has status "(?:[A-Za-z]+\.)?([A-Za-z_]+)"')
_FAIL_RE = re.compile(r'Failure message: "(.*)"', re.DOTALL)
_STATUS_MAP = {"queued": "queued", "newscript": "queued", "running": "running", "complete": "complete",
               "error": "error", "cancelrequested": "cancelled", "cancelacknowledged": "cancelled",
               "cancelled": "cancelled", "canceled": "cancelled"}


def kernel_url(username: str, slug: str) -> str:
    return f"https://www.kaggle.com/code/{username}/{slug}"


def push_kernel(creds: Creds, folder: Path, slug: str, dataset_slugs: list[str], accelerator: str = "",
                gpu: bool = True) -> str:
    meta = {
        "id": f"{creds.username}/{slug}",
        "title": slug.replace("-", " "),  # slugifies back to exactly `slug`
        "code_file": "render.py", "language": "python", "kernel_type": "script",
        "is_private": True, "enable_gpu": bool(gpu), "enable_tpu": False,
        "enable_internet": True,  # needed to download Blender
        "dataset_sources": [f"{creds.username}/{d}" for d in dataset_slugs],
        "competition_sources": [], "kernel_sources": [],
    }
    (folder / "kernel-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    args = ["kernels", "push", "-p", str(folder)]
    if accelerator and gpu:
        args += ["--accelerator", accelerator]
    rc, out = run(creds, args, timeout=900)
    low = out.lower()
    if rc != 0 or _output_has_error(out) or "does not resolve" in low or "successfully pushed" not in low:
        raise KaggleError(out[-500:] or "Could not start the Kaggle kernel.")
    return kernel_url(creds.username, slug)


def kernel_status(creds: Creds, slug: str) -> tuple[str, str]:
    """(status, failure_message); status in queued/running/complete/error/cancelled/unknown."""
    rc, out = run(creds, ["kernels", "status", f"{creds.username}/{slug}"], timeout=120)
    if rc != 0:
        raise KaggleError(out[-300:])
    m = _STATUS_RE.search(out)
    if not m:
        raise KaggleError("Unexpected answer from Kaggle: " + out[-200:])
    fail = _FAIL_RE.search(out)
    # Kaggle's enum names have underscores (CANCEL_ACKNOWLEDGED, NEW_SCRIPT); older clients print camelCase
    key = m.group(1).lower().replace("_", "")
    return _STATUS_MAP.get(key, "unknown"), (fail.group(1).strip() if fail else "")


def download_output(creds: Creds, slug: str, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    rc, out = run(creds, ["kernels", "output", f"{creds.username}/{slug}", "-p", str(dest), "-o", "-q"],
                  timeout=3 * 3600)
    if rc != 0:
        raise KaggleError(out[-300:] or "Could not download the result.")
    return [p for p in dest.rglob("*") if p.is_file()]
