"""
Background engine of the add-on: uploads the scene, starts the Kaggle kernels, waits for them and
downloads the frames. Plain Python (no `bpy`), one worker thread per part, state saved in jobs.json so
closing Blender does NOT lose a render: on the next start the add-on resumes watching it.

A job = one render. It has 1..N parts (one per Kaggle account); each part renders a contiguous slice of
the frame range.
  * output "png"  : every part downloads its PNG frames into the SAME folder (nothing to merge).
  * output "video": every part encodes its slice to an mp4 segment ON KAGGLE and downloads only that small
                    segment into a temp folder. One account -> the segment is the final video. Several accounts
                    -> a small CPU "merge" kernel on Kaggle joins the segments, and only the final mp4 lands
                    in the chosen folder (the temp segments are deleted).
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import kaggle_cli
from .kaggle_cli import Creds, KaggleError

TERMINAL = ("done", "error")
ACTIVE = ("upload", "push", "queued", "running", "download")
NET_TRIES = 10                    # transient network errors are retried this many times (backoff up to 5 min)
OWNER_STALE_SEC = 30              # another Blender window counts as gone after this long without a heartbeat
_TRANSIENT = ("timed out", "timeout", "did not answer", "connection", "name resolution", "resolve", "temporar",
              "502", "503", "504", "429", "too many requests", "reset by peer", "network", "ssl", "eof",
              "broken pipe", "unreachable", "max retries", "lost contact", "unexpected answer")
MACTIVE = ACTIVE + ("finish",)   # merge stage: "finish" = moving the final video into place
HERE = Path(__file__).resolve().parent


def ascii_slug(text: str, default: str, maxlen: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:maxlen].strip("-")
    return s or default


def split_frames(start: int, end: int, step: int, n: int) -> list[tuple[int, int, int]]:
    """Cuts start..end (every `step`) into at most n contiguous, balanced slices."""
    step = max(1, int(step))
    frames = list(range(int(start), int(end) + 1, step))
    n = max(1, min(int(n), len(frames)))
    per, extra = divmod(len(frames), n)
    out, lo = [], 0
    for i in range(n):
        size = per + (1 if i < extra else 0)
        chunk = frames[lo:lo + size]
        lo += size
        out.append((chunk[0], chunk[-1], step))
    return out


def render_script(cfg: dict) -> str:
    text = (HERE / "kernel_job.py").read_text(encoding="utf-8")
    marker = "CONFIG = None  # @@CONFIG@@"
    if marker not in text:
        raise KaggleError("kernel_job.py is damaged (CONFIG marker missing).")
    return text.replace(marker, "CONFIG = json.loads(" + json.dumps(json.dumps(cfg)) + ")")


# The patterns are deliberately specific: a Blender message such as "Frame 401 failed" or "Object 'Phone' ..."
# must never be explained as an account problem.
_HINTS = (
    (re.compile(r"\b40[13]\s*(client error|server error|-|:|unauthori[sz]ed|forbidden)|\(40[13]\)|"
                r"http(?: status)?\D{0,3}40[13]\b|unauthori[sz]ed|forbidden|invalid (api )?(key|token|credentials)", re.I),
     "Kaggle rejected this account's token. Create a new token (kaggle.com > Settings > API) and add the account again."),
    (re.compile(r"weekly .{0,20}quota|quota (exceeded|reached|limit)|exceeded .{0,20}quota|gpu quota|"
                r"limit exceeded|not enough (gpu )?time", re.I),
     "This account may be out of its weekly GPU quota. Wait for it to reset, or use another account."),
    (re.compile(r"phone[ -]?(number|verif)|verify .{0,30}phone|phone[- ]verified", re.I),
     "Kaggle needs this account to be phone-verified (required for GPU and internet in notebooks)."),
)


def with_hint(msg: str) -> str:
    """Adds a plain-language explanation to the errors users meet most often."""
    msg = (msg or "").strip()
    for rx, text in _HINTS:
        if rx.search(msg):
            return f"{text} [{msg[-220:]}]"
    return msg[-500:]


def unique_path(path: Path) -> Path:
    """Never overwrite an existing file: shot.mp4 -> shot_2.mp4 -> shot_3.mp4 ..."""
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.stem}_{n}{path.suffix}").exists():
        n += 1
    return path.with_name(f"{path.stem}_{n}{path.suffix}")


class Runner:
    def __init__(self, work_dir: Path):
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "jobs.json"
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.creds: dict[str, Creds] = {}
        self.threads: dict[tuple[str, int], threading.Thread] = {}
        self.version = 0                       # bumped on every change: the UI redraws when it moves
        self.stop = threading.Event()
        self.poll_sec = 40
        self.push_retries = 12
        self.push_retry_sec = 30
        self.accelerator = "NvidiaTeslaT4"
        self.pid = os.getpid()
        self.owner_file = self.dir / "owner.json"
        self.bak = self.dir / "jobs.bak"
        self.owns = True
        self._last_claim = 0.0
        self._load()

    # ------------------------------------------------------------------ persistence
    @staticmethod
    def _sanitize_job(j):
        """Returns a usable job dict (missing fields filled in, e.g. a file written by an older version) or None."""
        if not isinstance(j, dict) or not isinstance(j.get("id"), str) or not j["id"]:
            return None
        parts = j.get("parts")
        if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
            return None
        for n, p in enumerate(parts):
            for key, default in (("account", ""), ("status", "error"), ("msg", ""), ("url", ""), ("ds_slug", ""),
                                 ("k_slug", ""), ("files", 0), ("failed_at", "")):
                p.setdefault(key, default)
            p["i"] = n
            fs = p.get("fs")
            if not (isinstance(fs, list) and len(fs) == 3 and all(isinstance(x, int) for x in fs)):
                return None
        for key, default in (("label", j["id"]), ("created", 0), ("out_dir", ""), ("prefix", "frame"),
                             ("blend_name", "scene.blend"), ("frames", [1, 1, 1]), ("eevee", False), ("force_gpu", True),
                             ("delete_dataset", True), ("status", "error"), ("output", "png"), ("crf", 18)):
            j.setdefault(key, default)
        m = j.get("merge")
        if m is not None:
            if not isinstance(m, dict):
                return None
            for key, default in (("status", "error"), ("msg", ""), ("url", ""), ("ds_slug", ""), ("k_slug", ""),
                                 ("failed_at", ""), ("account", parts[0]["account"]), ("file", "")):
                m.setdefault(key, default)
        return j

    def _read_jobs(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
            raise ValueError("jobs file has an unexpected structure")
        out = {}
        for raw in data.get("jobs", []):
            job = self._sanitize_job(raw)
            if job:
                out[job["id"]] = job
        return out

    def _load(self) -> None:
        """Reads jobs.json; if it is damaged (power cut while writing) falls back to the last good copy."""
        for path in (self.file, self.bak):
            if not path.exists():
                continue
            try:
                self.jobs = self._read_jobs(path)
                return
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                if path == self.file:
                    try:
                        path.replace(self.dir / "jobs.corrupt")
                    except OSError:
                        pass
        self.jobs = {}

    def _save(self) -> None:
        if not self.owns:   # another Blender window controls the jobs: never overwrite its state
            return
        tmp = self.file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"jobs": list(self.jobs.values())}, indent=1))
            f.flush()
            os.fsync(f.fileno())   # the bytes are really on disk before the old file is replaced
        if self.file.exists():
            try:
                shutil.copyfile(self.file, self.bak)
            except OSError:
                pass
        tmp.replace(self.file)

    def claim(self) -> bool:
        """Only ONE Blender window may drive the jobs (two would upload/download the same things twice)."""
        now = time.time()
        if self.owns and now - self._last_claim < 5:
            return True
        o = {}
        for attempt in range(4):   # the other window may be in the middle of rewriting the file
            try:
                o = json.loads(self.owner_file.read_text(encoding="utf-8"))
                if not isinstance(o, dict):
                    o = {}
                break
            except FileNotFoundError:
                break
            except (OSError, ValueError):
                time.sleep(0.02)
        try:
            other_alive = o.get("pid") not in (None, self.pid) and now - float(o.get("ts", 0)) < OWNER_STALE_SEC
        except (TypeError, ValueError):
            other_alive = False
        if other_alive:
            self.owns = False
            return False
        was_owner = self.owns
        self._write_owner(now)
        self.owns, self._last_claim = True, now
        if not was_owner:
            with self.lock:
                self._load()   # the other window may have advanced the jobs: start from its latest state
        return True

    def _write_owner(self, ts: float) -> None:
        """Atomic: a reader never sees a half-written owner file (it would think nobody owns the jobs)."""
        tmp = self.owner_file.with_name(f"owner.{self.pid}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps({"pid": self.pid, "ts": ts}), encoding="utf-8")
            tmp.replace(self.owner_file)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _touch(self) -> None:
        self.version += 1
        try:
            self._save()
        except OSError:
            pass

    # ------------------------------------------------------------------ public API (main thread)
    def update_creds(self, creds: dict[str, Creds]) -> None:
        with self.lock:
            new = dict(creds)
            # an account removed from the preferences while one of its parts is still running keeps working
            for job in self.jobs.values():
                for part in job["parts"]:
                    acc = part["account"]
                    if acc not in new and acc in self.creds and part["status"] in ACTIVE:
                        new[acc] = self.creds[acc]
                m = job.get("merge")
                if m and m["account"] not in new and m["account"] in self.creds and m["status"] in MACTIVE:
                    new[m["account"]] = self.creds[m["account"]]
            self.creds = new

    def snapshot(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(self.jobs.values()))

    def start_job(self, label: str, blend_file: Path, frames: tuple[int, int, int], accounts: list[str],
                  out_dir: str, prefix: str, eevee: bool = False, force_gpu: bool = True,
                  delete_dataset: bool = True, output: str = "video", crf: int = 18) -> str:
        if not accounts:
            raise KaggleError("Choose at least one Kaggle account.")
        if not self.claim():
            raise KaggleError("Another Blender window is already running Kaggle jobs. Use that window.")
        chunks = split_frames(*frames, n=len(accounts))
        jid = uuid.uuid4().hex[:8]
        stage = self.dir / "stage" / jid
        stage.mkdir(parents=True, exist_ok=True)
        blend_name = ascii_slug(label.rsplit(".", 1)[0], "scene") + ".blend"
        shutil.move(str(blend_file), str(stage / blend_name))
        parts = [{"i": i, "account": accounts[i], "fs": list(c), "status": "upload", "msg": "Uploading the scene...",
                  "url": "", "ds_slug": "", "k_slug": "", "files": 0, "failed_at": ""}
                 for i, c in enumerate(chunks)]
        job = {"id": jid, "label": label, "created": int(time.time()), "out_dir": str(out_dir),
               "prefix": prefix, "blend_name": blend_name, "frames": list(frames), "eevee": bool(eevee),
               "force_gpu": bool(force_gpu), "delete_dataset": bool(delete_dataset), "status": "running",
               "output": output, "crf": int(crf), "parts": parts,
               "merge": ({"status": "wait", "msg": "", "url": "", "ds_slug": "", "k_slug": "", "failed_at": "",
                          "account": accounts[0], "file": ""} if output == "video" else None)}
        with self.lock:
            self.jobs[jid] = job
            self._touch()
        self.resume()
        return jid

    def resume(self) -> None:
        """Starts a worker for every unfinished part that has none (also used after Blender restarts)."""
        if not self.claim():
            return
        with self.lock:
            for job in self.jobs.values():
                for part in job["parts"]:
                    key = (job["id"], part["i"])
                    t = self.threads.get(key)
                    if part["status"] in ACTIVE and not (t and t.is_alive()):
                        t = threading.Thread(target=self._run_part, args=key, daemon=True,
                                             name=f"kaggle-{job['id']}-{part['i']}")
                        self.threads[key] = t
                        t.start()
                m = job.get("merge")
                if not m:
                    continue
                if m["status"] == "wait" and all(p["status"] == "done" for p in job["parts"]):
                    m["status"] = "finish" if len(job["parts"]) == 1 else "upload"
                    m["msg"] = "Saving the video..." if m["status"] == "finish" else "Uploading the segments..."
                    self._refresh_job(job)
                    self._touch()
                mkey = (job["id"], -1)
                t = self.threads.get(mkey)
                if m["status"] in MACTIVE and not (t and t.is_alive()):
                    t = threading.Thread(target=self._run_merge, args=(job["id"],), daemon=True,
                                         name=f"kaggle-merge-{job['id']}")
                    self.threads[mkey] = t
                    t.start()

    def retry_part(self, jid: str, i: int) -> None:
        if i == -1:
            return self.retry_merge(jid)
        with self.lock:
            try:
                part = self._part(jid, i)
            except (KeyError, IndexError):
                return   # the render was removed from the list in the meantime
            back = part.get("failed_at") or ""
            if part["status"] != "error" or back not in ("upload", "push", "queued", "running", "download"):
                return
            part.update(status=back, msg="Retrying...", failed_at="")
            self._refresh_job(self.jobs[jid])
            self._touch()
        self.resume()

    def retry_merge(self, jid: str) -> None:
        with self.lock:
            m = (self.jobs.get(jid) or {}).get("merge")
            if not m or m["status"] != "error" or not m.get("failed_at"):
                return
            m.update(status=m["failed_at"], msg="Retrying...", failed_at="")
            self._refresh_job(self.jobs[jid])
            self._touch()
        self.resume()

    def _cleanup_remote(self, job: dict) -> None:
        """Best effort: delete the scene/segment datasets this job left on Kaggle (they count against storage)."""
        slugs = [(p["account"], p["ds_slug"]) for p in job["parts"] if p.get("ds_slug")]
        m = job.get("merge")
        if m and m.get("ds_slug"):
            slugs.append((m["account"], m["ds_slug"]))
        for account, slug in slugs:
            with self.lock:
                c = self.creds.get(account)
            if c:
                try:
                    kaggle_cli.delete_dataset(c, slug)
                except Exception:  # noqa: BLE001
                    pass

    def remove_job(self, jid: str, force: bool = False) -> bool:
        with self.lock:
            job = self.jobs.get(jid)
            busy = job and (any(p["status"] in ACTIVE for p in job["parts"])
                            or (job.get("merge") and job["merge"]["status"] in MACTIVE))
            if not job or (busy and not force):
                return False
            del self.jobs[jid]
            self._touch()
        threading.Thread(target=self._cleanup_remote, args=(job,), daemon=True).start()
        for sub in ("stage", "dl", "push", "seg", "mstage", "mpush", "mdl"):
            shutil.rmtree(self.dir / sub / jid, ignore_errors=True)
        return True

    def release(self) -> None:
        if self.owns:
            self._write_owner(0)

    def shutdown(self) -> None:
        self.stop.set()
        self.release()

    # ------------------------------------------------------------------ state helpers
    def _part(self, jid: str, i: int) -> dict:
        return self.jobs[jid]["parts"][i]

    def _set(self, jid: str, i: int, **fields) -> None:
        with self.lock:
            self._part(jid, i).update(fields)
            self._refresh_job(self.jobs[jid])
            self._touch()

    def _refresh_job(self, job: dict) -> None:
        st = [p["status"] for p in job["parts"]]
        m = job.get("merge")
        parts_done = all(s == "done" for s in st)
        if parts_done and (not m or m["status"] == "done"):
            job["status"] = "done"
        elif any(s in ACTIVE for s in st) or (m and (m["status"] in MACTIVE or (parts_done and m["status"] == "wait"))):
            job["status"] = "running"
        else:
            job["status"] = "error"

    @staticmethod
    def _transient(e: Exception) -> bool:
        if isinstance(e, RenderFailed):
            return False
        low = str(e).lower()
        return any(w in low for w in _TRANSIENT)

    def _backoff(self, tries: int) -> None:
        self._sleep(min(300, 30 * tries))
        if self.stop.is_set():
            raise _Stopped()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stop.is_set():
            time.sleep(min(0.25, max(0.0, end - time.monotonic())))

    def _creds_for(self, account: str) -> Creds:
        for _ in range(480):  # right after Blender starts the add-on may not have handed over the tokens yet
            with self.lock:
                c = self.creds.get(account)
            if c:
                return c
            if self.stop.is_set():
                raise _Stopped()
            time.sleep(0.25)
        raise KaggleError(f"The account '{account}' is not in the add-on preferences (or was not verified).")

    # ------------------------------------------------------------------ worker
    def _run_part(self, jid: str, i: int) -> None:
        tries = 0
        try:
            while not self.stop.is_set():
                with self.lock:
                    job = copy.deepcopy(self.jobs.get(jid))
                    if not job:
                        return
                    part = job["parts"][i]
                st = part["status"]
                if st in TERMINAL:
                    return
                creds = self._creds_for(part["account"])
                try:
                    if st == "upload":
                        self._do_upload(job, part, creds)
                    elif st == "push":
                        self._do_push(job, part, creds)
                    elif st in ("queued", "running"):
                        self._do_poll(job, part, creds)
                    elif st == "download":
                        self._do_download(job, part, creds)
                    else:
                        return
                    tries = 0
                except _Stopped:
                    raise
                except Exception as e:  # noqa: BLE001
                    if self._transient(e) and tries < NET_TRIES:
                        tries += 1
                        self._set(jid, i, msg=f"Network problem, retrying ({tries}/{NET_TRIES}): {str(e)[-120:]}")
                        self._backoff(tries)
                        continue
                    raise
        except _Stopped:
            return
        except Exception as e:  # noqa: BLE001 - a broken part must never kill the add-on
            with self.lock:
                cur = self.jobs.get(jid)
                if not cur:
                    return
                prev = cur["parts"][i]["status"]
            if isinstance(e, RenderFailed):   # the kernel's own verdict: a retry means rendering again
                self._set(jid, i, status="error", failed_at="push", msg=str(e)[-500:] or "The render failed.")
            else:
                self._set(jid, i, status="error", failed_at=prev, msg=with_hint(str(e)) or e.__class__.__name__)
        finally:
            self._maybe_cleanup(jid)
            self.resume()

    def _do_upload(self, job: dict, part: dict, creds: Creds) -> None:
        jid, i = job["id"], part["i"]
        if not part["ds_slug"]:
            src = self.dir / "stage" / jid / job["blend_name"]
            if not src.exists():
                raise KaggleError("Blender was closed while the scene was uploading. Start the render again.")
            folder = self.dir / "stage" / jid / f"ds{i}"
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            try:
                import os
                os.link(src, folder / job["blend_name"])   # no second copy of a big file
            except OSError:
                shutil.copy2(src, folder / job["blend_name"])
            slug = f"krb-{jid}-{i}"
            if not kaggle_cli.dataset_exists(creds, slug):
                kaggle_cli.create_dataset(creds, folder, slug, f"Blender scene {jid} part {i + 1}")
            shutil.rmtree(folder, ignore_errors=True)
            self._set(jid, i, ds_slug=slug, status="push", msg="Scene uploaded. Starting the render...")
        else:
            self._set(jid, i, status="push")

    def _do_push(self, job: dict, part: dict, creds: Creds) -> None:
        jid, i = job["id"], part["i"]
        slug = part["k_slug"] or f"kr-render-{jid}-{i}"
        if not part["k_slug"]:
            known = self._existing_kernel(creds, slug)
            if known in ("queued", "running"):
                return self._set(jid, i, k_slug=slug, url=kaggle_cli.kernel_url(creds.username, slug),
                                 status=known, msg="Found the kernel already running on Kaggle.")
            if known == "complete":
                return self._set(jid, i, k_slug=slug, url=kaggle_cli.kernel_url(creds.username, slug),
                                 status="download", msg="Found the finished render on Kaggle. Downloading...")
        fs = part["fs"]
        cfg = {"blend_slug": part["ds_slug"], "blend_name": job["blend_name"], "prefix": job["prefix"],
               "frame_start": fs[0], "frame_end": fs[1], "frame_step": fs[2],
               "eevee_to_cycles": job["eevee"], "force_gpu": job["force_gpu"],
               "output": job.get("output", "png"), "crf": job.get("crf", 18),
               "video_name": f"{job['prefix']}_part{i + 1:02d}.mp4"}
        folder = self.dir / "push" / jid / f"k{i}"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        (folder / "render.py").write_text(render_script(cfg), encoding="utf-8")
        kaggle_cli.wait_dataset_ready(creds, part["ds_slug"], timeout=180, sleep=self._sleep)
        url, last = "", None
        for attempt in range(self.push_retries):
            try:
                url = kaggle_cli.push_kernel(creds, folder, slug, [part["ds_slug"]], self.accelerator)
                break
            except KaggleError as e:
                last = e
                low = str(e).lower()
                # the dataset is still being processed by Kaggle -> wait and try again
                if not any(w in low for w in ("not found", "does not exist", "does not resolve", "processing",
                                              "not ready", "not yet", "still being")):
                    raise
                self._set(jid, i, msg=f"Kaggle is still preparing the scene ({attempt + 1}/{self.push_retries})...")
                self._sleep(self.push_retry_sec)
                if self.stop.is_set():
                    raise _Stopped()
        else:
            raise last or KaggleError("Could not start the Kaggle kernel.")
        shutil.rmtree(folder, ignore_errors=True)
        self._set(jid, i, k_slug=slug, url=url, status="queued", msg="Waiting for a Kaggle GPU...")

    def _existing_kernel(self, creds: Creds, slug: str) -> str:
        """Status of a kernel that may already exist ('' when it does not). Network errors are re-raised."""
        try:
            return kaggle_cli.kernel_status(creds, slug)[0]
        except KaggleError as e:
            if self._transient(e) and "unexpected answer" not in str(e).lower():
                raise
            return ""

    def _do_poll(self, job: dict, part: dict, creds: Creds) -> None:
        jid, i = job["id"], part["i"]
        fails = 0
        while not self.stop.is_set():
            try:
                st, fail = kaggle_cli.kernel_status(creds, part["k_slug"])
                fails = 0
            except KaggleError as e:
                fails += 1
                if fails >= (30 if self._transient(e) else 4):   # a rejected token / missing kernel will not heal
                    raise KaggleError(("Lost contact with Kaggle: " if self._transient(e) else "") + str(e)[-200:])
                self._sleep(self.poll_sec)
                continue
            if st in ("queued", "running"):
                with self.lock:
                    cur = self._part(jid, i)["status"]
                if cur != st:
                    self._set(jid, i, status=st, msg="Rendering on Kaggle..." if st == "running"
                              else "Waiting for a Kaggle GPU...")
                self._sleep(self.poll_sec)
                continue
            if st == "complete":
                self._set(jid, i, status="download", msg="Downloading frames...")
                return
            if st in ("error", "cancelled"):
                reason = fail or ("The render was cancelled on Kaggle." if st == "cancelled"
                                  else "The Kaggle kernel failed.")
                if st != "cancelled":   # a cancelled run has no failure to explain: keep the plain message
                    reason = self._failure_details(job, part, creds) or reason
                self._set(jid, i, status="error", failed_at="push", msg=reason[-500:])
                return
            self._sleep(self.poll_sec)  # 'unknown': keep waiting
        raise _Stopped()

    def _failure_details(self, job: dict, part: dict, creds: Creds) -> str:
        """Best effort: the kernel's own reason + log tail (saved next to the frames)."""
        tmp = self.dir / "dl" / job["id"] / f"f{part['i']}"
        try:
            files = {p.name: p for p in kaggle_cli.download_output(creds, part["k_slug"], tmp)}
            reason, tail = "", ""
            if "render_status.json" in files:
                reason = str(json.loads(files["render_status.json"].read_text(encoding="utf-8")).get("reason", ""))
            if "render_log.txt" in files:
                log = files["render_log.txt"].read_text(encoding="utf-8", errors="replace")
                tail = log[-600:]
                logs = Path(job["out_dir"]) / "_kaggle_logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / f"part{part['i'] + 1}_render_log.txt").write_text(log, encoding="utf-8")
            return (reason + ("\n" + tail if tail and not reason else "")).strip()
        except Exception:  # noqa: BLE001
            return ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _do_download(self, job: dict, part: dict, creds: Creds) -> None:
        jid, i = job["id"], part["i"]
        tmp = self.dir / "dl" / jid / f"p{i}"
        shutil.rmtree(tmp, ignore_errors=True)
        files, last = [], None
        for attempt in range(3):
            try:
                files = kaggle_cli.download_output(creds, part["k_slug"], tmp)
                break
            except KaggleError as e:
                last = e
                self._sleep(10 * (attempt + 1))
                if self.stop.is_set():
                    raise _Stopped()
        else:
            raise last or KaggleError("Could not download the frames.")
        by_name = {p.name: p for p in files}
        out = Path(job["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        video_mode = job.get("output", "png") == "video"
        status = {}
        if "render_status.json" in by_name:
            try:
                status = json.loads(by_name["render_status.json"].read_text(encoding="utf-8"))
            except ValueError:
                status = {}
        failed = status.get("ok") is False
        if "render_log.txt" in by_name and (failed or not video_mode):  # video mode: no extra files on success
            logs = out / "_kaggle_logs"
            logs.mkdir(exist_ok=True)
            shutil.copyfile(by_name["render_log.txt"], logs / f"part{i + 1}_render_log.txt")
        if failed:
            raise RenderFailed(str(status.get("reason") or "The render failed on Kaggle.")[-400:])
        if video_mode:
            vids = [p for p in files if p.suffix.lower() == ".mp4"]
            if not vids:
                raise KaggleError("Kaggle finished but no video came back.")
            seg_dir = self.dir / "seg" / jid
            seg_dir.mkdir(parents=True, exist_ok=True)
            target = seg_dir / f"part{i}.mp4"
            if target.exists():
                target.unlink()
            shutil.move(str(vids[0]), str(target))
            shutil.rmtree(tmp, ignore_errors=True)
            if job["delete_dataset"] and part["ds_slug"]:
                try:
                    kaggle_cli.delete_dataset(creds, part["ds_slug"])
                except KaggleError:
                    pass
            n = int(status.get("frames") or 0)
            self._set(jid, i, status="done", files=n, msg=f"{n} frames rendered and encoded on Kaggle.")
            return
        frames = [p for p in files if p.suffix.lower() == ".png" and p.parent.name == "frames"] \
            or [p for p in files if p.suffix.lower() == ".png"]
        for p in frames:
            target = out / p.name
            if target.exists():
                target.unlink()
            shutil.move(str(p), str(target))
        shutil.rmtree(tmp, ignore_errors=True)
        fs = part["fs"]
        expected = len(range(fs[0], fs[1] + 1, fs[2]))
        if len(frames) < expected:
            raise KaggleError(f"Only {len(frames)} of {expected} frames came back from Kaggle.")
        if job["delete_dataset"] and part["ds_slug"]:
            try:
                kaggle_cli.delete_dataset(creds, part["ds_slug"])
            except KaggleError:
                pass  # harmless: only leaves a private scene copy on Kaggle
        self._set(jid, i, status="done", files=len(frames), msg=f"{len(frames)} frames downloaded.")

    # ------------------------------------------------------------------ merge stage (video, several accounts)
    def _mset(self, jid: str, **fields) -> None:
        with self.lock:
            self.jobs[jid]["merge"].update(fields)
            self._refresh_job(self.jobs[jid])
            self._touch()

    def _run_merge(self, jid: str) -> None:
        tries = 0
        try:
            while not self.stop.is_set():
                with self.lock:
                    job = copy.deepcopy(self.jobs.get(jid))
                    if not job or not job.get("merge"):
                        return
                    m = job["merge"]
                st = m["status"]
                if st not in MACTIVE:
                    return
                if st == "finish":
                    self._merge_finish(job)
                    continue
                creds = self._creds_for(m["account"])
                try:
                    if st == "upload":
                        self._merge_upload(job, m, creds)
                    elif st == "push":
                        self._merge_push(job, m, creds)
                    elif st in ("queued", "running"):
                        self._merge_poll(job, m, creds)
                    elif st == "download":
                        self._merge_download(job, m, creds)
                    else:
                        return
                    tries = 0
                except _Stopped:
                    raise
                except Exception as e:  # noqa: BLE001
                    if self._transient(e) and tries < NET_TRIES:
                        tries += 1
                        self._mset(jid, msg=f"Network problem, retrying ({tries}/{NET_TRIES}): {str(e)[-120:]}")
                        self._backoff(tries)
                        continue
                    raise
        except _Stopped:
            return
        except Exception as e:  # noqa: BLE001
            with self.lock:
                cur = self.jobs.get(jid)
                if not cur or not cur.get("merge"):
                    return
                prev = cur["merge"]["status"]
            if isinstance(e, RenderFailed):
                self._mset(jid, status="error", failed_at="push", msg=str(e)[-500:] or "The merge failed.")
            else:
                self._mset(jid, status="error", failed_at=prev, msg=with_hint(str(e)) or e.__class__.__name__)

    def _seg_names(self, job: dict) -> list[str]:
        return [f"part{i + 1:02d}.mp4" for i in range(len(job["parts"]))]

    def _merge_upload(self, job: dict, m: dict, creds: Creds) -> None:
        jid = job["id"]
        if not m["ds_slug"]:
            folder = self.dir / "mstage" / jid
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            for i, name in enumerate(self._seg_names(job)):
                src = self.dir / "seg" / jid / f"part{i}.mp4"
                if not src.exists():
                    # the temporary segment was lost (cleaned up / disk problem): fetch it again from Kaggle
                    shutil.rmtree(folder, ignore_errors=True)
                    self._set(jid, i, status="download", msg="Segment missing on this PC. Downloading it again...")
                    self._mset(jid, status="wait", msg="Waiting for the parts...")
                    return
                shutil.copy2(src, folder / name)
            slug = f"krb-{jid}-m"
            if not kaggle_cli.dataset_exists(creds, slug):
                kaggle_cli.create_dataset(creds, folder, slug, f"Video segments {jid}")
            shutil.rmtree(folder, ignore_errors=True)
            self._mset(jid, ds_slug=slug, status="push", msg="Segments uploaded. Starting the merge...")
        else:
            self._mset(jid, status="push")

    def _merge_push(self, job: dict, m: dict, creds: Creds) -> None:
        jid = job["id"]
        slug = m["k_slug"] or f"kr-merge-{jid}"
        if not m["k_slug"]:
            known = self._existing_kernel(creds, slug)
            if known in ("queued", "running"):
                return self._mset(jid, k_slug=slug, url=kaggle_cli.kernel_url(creds.username, slug),
                                  status=known, msg="Found the merge already running on Kaggle.")
            if known == "complete":
                return self._mset(jid, k_slug=slug, url=kaggle_cli.kernel_url(creds.username, slug),
                                  status="download", msg="Found the finished merge on Kaggle. Downloading...")
        cfg = {"mode": "merge", "seg_slug": m["ds_slug"], "names": self._seg_names(job),
               "video_name": f"{job['prefix']}.mp4"}
        folder = self.dir / "mpush" / jid
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        (folder / "render.py").write_text(render_script(cfg), encoding="utf-8")
        kaggle_cli.wait_dataset_ready(creds, m["ds_slug"], timeout=180, sleep=self._sleep)
        url, last = "", None
        for attempt in range(self.push_retries):
            try:
                url = kaggle_cli.push_kernel(creds, folder, slug, [m["ds_slug"]], gpu=False)  # CPU is enough
                break
            except KaggleError as e:
                last = e
                low = str(e).lower()
                if not any(w in low for w in ("not found", "does not exist", "does not resolve", "processing",
                                              "not ready", "not yet", "still being")):
                    raise
                self._mset(jid, msg=f"Kaggle is still preparing the segments ({attempt + 1}/{self.push_retries})...")
                self._sleep(self.push_retry_sec)
                if self.stop.is_set():
                    raise _Stopped()
        else:
            raise last or KaggleError("Could not start the merge kernel.")
        shutil.rmtree(folder, ignore_errors=True)
        self._mset(jid, k_slug=slug, url=url, status="queued", msg="Waiting for Kaggle to merge the video...")

    def _merge_poll(self, job: dict, m: dict, creds: Creds) -> None:
        jid = job["id"]
        fails = 0
        while not self.stop.is_set():
            try:
                st, fail = kaggle_cli.kernel_status(creds, m["k_slug"])
                fails = 0
            except KaggleError as e:
                fails += 1
                if fails >= (30 if self._transient(e) else 4):
                    raise KaggleError(("Lost contact with Kaggle: " if self._transient(e) else "") + str(e)[-200:])
                self._sleep(self.poll_sec)
                continue
            if st in ("queued", "running"):
                with self.lock:
                    cur = self.jobs[jid]["merge"]["status"]
                if cur != st:
                    self._mset(jid, status=st, msg="Merging the video on Kaggle...")
                self._sleep(self.poll_sec)
                continue
            if st == "complete":
                self._mset(jid, status="download", msg="Downloading the final video...")
                return
            reason = fail or ("The merge was cancelled on Kaggle." if st == "cancelled"
                              else "The Kaggle merge kernel failed.")
            if st != "cancelled":
                reason = self._merge_failure(job, m, creds) or reason
            self._mset(jid, status="error", failed_at="push", msg=reason[-500:])
            return
        raise _Stopped()

    def _merge_failure(self, job: dict, m: dict, creds: Creds) -> str:
        tmp = self.dir / "mdl" / job["id"] / "fail"
        try:
            files = {p.name: p for p in kaggle_cli.download_output(creds, m["k_slug"], tmp)}
            if "render_status.json" in files:
                return str(json.loads(files["render_status.json"].read_text(encoding="utf-8")).get("reason", ""))
            return ""
        except Exception:  # noqa: BLE001
            return ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _merge_download(self, job: dict, m: dict, creds: Creds) -> None:
        jid = job["id"]
        tmp = self.dir / "mdl" / jid / "ok"
        shutil.rmtree(tmp, ignore_errors=True)
        files, last = [], None
        for attempt in range(3):
            try:
                files = kaggle_cli.download_output(creds, m["k_slug"], tmp)
                break
            except KaggleError as e:
                last = e
                self._sleep(10 * (attempt + 1))
                if self.stop.is_set():
                    raise _Stopped()
        else:
            raise last or KaggleError("Could not download the final video.")
        by_name = {p.name: p for p in files}
        if "render_status.json" in by_name:
            try:
                status = json.loads(by_name["render_status.json"].read_text(encoding="utf-8"))
            except ValueError:
                status = {}
            if status.get("ok") is False:
                raise RenderFailed(str(status.get("reason") or "The merge failed on Kaggle.")[-400:])
        vids = [p for p in files if p.suffix.lower() == ".mp4"]
        if not vids:
            raise KaggleError("Kaggle finished but the final video did not come back.")
        seg_dir = self.dir / "seg" / jid
        seg_dir.mkdir(parents=True, exist_ok=True)
        final_tmp = seg_dir / "final.mp4"
        if final_tmp.exists():
            final_tmp.unlink()
        shutil.move(str(vids[0]), str(final_tmp))
        shutil.rmtree(tmp, ignore_errors=True)
        if job["delete_dataset"] and m["ds_slug"]:
            try:
                kaggle_cli.delete_dataset(creds, m["ds_slug"])
            except KaggleError:
                pass
        self._mset(jid, status="finish", msg="Saving the video...")

    def _merge_finish(self, job: dict) -> None:
        jid = job["id"]
        seg_dir = self.dir / "seg" / jid
        src = seg_dir / ("part0.mp4" if len(job["parts"]) == 1 else "final.mp4")
        out = Path(job["out_dir"])
        m = job["merge"]
        target = Path(m["file"]) if m.get("file") else unique_path(out / f"{job['prefix']}.mp4")
        if not m.get("file"):
            self._mset(jid, file=str(target))   # remembered, so a crash and restart keeps the same name
        if not src.exists():
            if target.exists() and target.stat().st_mtime >= job.get("created", 0):
                shutil.rmtree(seg_dir, ignore_errors=True)   # crashed after the move but before saving the state
                self._mset(jid, status="done", file=str(target), msg=f"Video saved: {target.name}")
                return
            # the temporary file is gone: get it from Kaggle again instead of asking the user to re-render
            if len(job["parts"]) == 1:
                self._set(jid, 0, status="download", msg="Video file missing on this PC. Downloading it again...")
                self._mset(jid, status="wait", msg="Waiting for the parts...")
            else:
                self._mset(jid, status="download" if job["merge"]["k_slug"] else "upload",
                           msg="Video file missing on this PC. Fetching it again...")
            return
        out.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.move(str(src), str(target))
        shutil.rmtree(seg_dir, ignore_errors=True)  # the temporary segments are gone
        self._mset(jid, status="done", file=str(target), msg=f"Video saved: {target.name}")

    def _maybe_cleanup(self, jid: str) -> None:
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return
            past_upload = all(p["ds_slug"] for p in job["parts"])   # an upload that failed can still be retried
            all_end = all(p["status"] in TERMINAL for p in job["parts"])
        if past_upload:
            shutil.rmtree(self.dir / "stage" / jid, ignore_errors=True)
        if all_end:
            for sub in ("dl", "push"):
                shutil.rmtree(self.dir / sub / jid, ignore_errors=True)


class RenderFailed(KaggleError):
    """The render itself failed on Kaggle (Blender/ffmpeg problem). Never explained as an account problem."""


class _Stopped(Exception):
    pass
