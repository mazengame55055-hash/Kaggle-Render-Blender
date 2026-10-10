"""
This file runs INSIDE a Kaggle kernel (not inside Blender on your PC).
The add-on copies it to render.py and replaces the CONFIG line below with the job's settings.

Two modes:
  render (default): renders the frames, then either
        output = "video"  -> encodes them to ONE mp4 segment ON KAGGLE (/kaggle/working/video/<name>.mp4);
                             the PNGs never leave the kernel, so only a small video is downloaded
        output = "png"    -> keeps the PNG frames in /kaggle/working/frames (old behaviour)
  merge: joins the mp4 segments of several accounts into the final video (ffmpeg -c copy, on Kaggle)

Results (Kaggle "Output" tab):
    /kaggle/working/video/<name>.mp4               the video (video / merge modes)
    /kaggle/working/frames/<prefix>_0001.png ...   the rendered frames (png mode only)
    /kaggle/working/render_log.txt                 human-readable log
    /kaggle/working/render_status.json             {"ok": true/false, "reason": "..."}
"""
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from fractions import Fraction
from pathlib import Path

CONFIG = None  # @@CONFIG@@

INPUT_ROOT = Path("/kaggle/input")
WORK_OUT = Path("/kaggle/working")
TMP = Path("/kaggle/temp") if Path("/kaggle/temp").exists() else Path("/tmp")
LOG_PATH = WORK_OUT / "render_log.txt"
STATUS_PATH = WORK_OUT / "render_status.json"
_log_file = None

BLENDER_CANDIDATES = [("5.2.2", "5.2"), ("4.2.3", "4.2"), ("4.2.0", "4.2"), ("4.1.1", "4.1")]
MAX_RUN_SEC = 8 * 3600 + 30 * 60   # Kaggle sessions end after ~9 h: stop cleanly before that
STALL_SEC = 30 * 60       # EEVEE only: kill Blender after this long without ANY output (stuck)
HEARTBEAT_SEC = 60      # log a "still alive" line when Blender prints nothing for this long
PROGRESS_LOG_SEC = 30   # at most one Blender "Fra:..." progress line per this many seconds
HARMLESS_NOISE = ("HIPEW", "oneAPI", "ONEAPI", "OpenImageDenoise", "Warning: Could not find")


class JobError(Exception):
    pass


def log(*parts):
    msg = " ".join(str(p) for p in parts)
    print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


# --------------------------------------------------------------------------- hardware / install
def detect_hardware():
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                log("GPU:", r.stdout.strip().replace("\n", " | "))
                return "GPU"
        except Exception:
            pass
    log("No GPU detected, rendering on CPU.")
    return "CPU"


def apt_install():
    base = ["libxi6", "libxrender1", "libglu1-mesa", "libxkbcommon0", "libsm6", "libice6", "libxfixes3",
            "libxcursor1", "libxinerama1", "libxrandr2", "libxss1", "libxtst6", "libxxf86vm1", "xvfb"]
    subprocess.run(["apt-get", "update", "-qq"], capture_output=True)
    for gl in ("libgl1-mesa-glx", "libgl1"):  # package was renamed on newer Ubuntu
        r = subprocess.run(["apt-get", "install", "-y", "-qq", gl] + base, capture_output=True, text=True)
        if r.returncode == 0:
            return
    raise JobError("apt-get install failed: " + (r.stderr or "")[-400:])


def install_blender():
    apt_install()
    if shutil.which("blender"):
        return "blender"
    TMP.mkdir(parents=True, exist_ok=True)
    for ver, major in BLENDER_CANDIDATES:
        url = f"https://download.blender.org/release/Blender{major}/blender-{ver}-linux-x64.tar.xz"
        archive = TMP / f"blender-{ver}.tar.xz"
        log(f"Downloading Blender {ver} ...")
        dl = subprocess.run(["wget", "-q", url, "-O", str(archive)])
        if dl.returncode == 0 and archive.exists() and archive.stat().st_size > 10_000_000:
            subprocess.run(["tar", "-xf", str(archive), "-C", str(TMP)], check=True)
            archive.unlink(missing_ok=True)
            binary = TMP / f"blender-{ver}-linux-x64" / "blender"
            if binary.exists():
                subprocess.run(["ln", "-sf", str(binary), "/usr/local/bin/blender"])
                log(f"Blender {ver} installed.")
                return "blender"
        archive.unlink(missing_ok=True)
    raise JobError("Could not download Blender from download.blender.org (is internet enabled for this kernel?).")


# --------------------------------------------------------------------------- inputs
def find_file(slug, name):
    """Kaggle mounts datasets as /kaggle/input/<slug> or /kaggle/input/datasets/<owner>/<slug>."""
    root = INPUT_ROOT
    if root.exists():
        for d in root.rglob(slug):
            if d.is_dir():
                hit = d / name
                if hit.exists():
                    return hit
                for cand in d.rglob(name):
                    if cand.is_file():
                        return cand
        # Fallback only when the match is unambiguous; guessing between same-named files could render the wrong one.
        matches = [p for p in root.rglob(name) if p.is_file()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise JobError(f"Input file {name} is ambiguous ({len(matches)} copies) and dataset {slug} was not found.")
    raise JobError(f"Input file not found: {name} (dataset {slug}). Is the dataset attached and processed?")


# --------------------------------------------------------------------------- Blender side
BLENDER_SCRIPT = r'''
import bpy, json, os

CFG = json.loads(@@CFG@@)
OUT_DIR = @@OUT_DIR@@
FORCE_GPU = @@FORCE_GPU@@
scene = bpy.context.scene


def log(*a):
    print(*a, flush=True)


def safe_set_png_format(scene):
    s = scene.render.image_settings
    if hasattr(s, "media_type"):
        try:
            s.media_type = "IMAGE"
        except Exception:
            pass
    try:
        s.file_format = "PNG"
    except Exception as e:
        log("WARNING: could not switch output format to PNG:", e)


def setup_render_device(scene):
    if scene.render.engine != "CYCLES":
        return
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for dtype in ("OPTIX", "CUDA"):
            try:
                prefs.compute_device_type = dtype
                refresh = getattr(prefs, "refresh_devices", None) or prefs.get_devices  # name differs per version
                refresh()
                devices = [d for d in prefs.devices if d.type == dtype]
                if devices:
                    for d in prefs.devices:
                        d.use = (d.type == dtype)
                    scene.cycles.device = "GPU"
                    log("GPU enabled using", dtype)
                    return
            except Exception:
                continue
        raise RuntimeError("no compatible GPU backend")
    except Exception as e:
        log("WARNING: GPU not enabled, using CPU:", e)
        scene.cycles.device = "CPU"


def pick_engine(scene, name):
    if name == "KEEP_ORIGINAL":
        return None
    items = [i.identifier for i in scene.render.bl_rna.properties["engine"].enum_items]
    for cand in (["CYCLES"] if name == "CYCLES" else ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]):
        if cand in items:
            return cand
    log("WARNING: engine", name, "not available in this Blender, keeping the file's engine")
    return None


def do_render(animation):
    kwargs = {"animation": True} if animation else {"write_still": True}
    try:
        bpy.ops.render.render(**kwargs)
    except Exception as e:
        err = str(e)
        if "OptiX" in err or "denoiser" in err.lower():
            log("WARNING: OptiX denoiser failed, switching to OpenImageDenoise:", err)
            try:
                scene.cycles.denoiser = "OPENIMAGEDENOISE"
                bpy.ops.render.render(**kwargs)
            except Exception as e2:
                log("WARNING: OIDN failed too, continuing without denoising:", e2)
                scene.cycles.use_denoising = False
                bpy.ops.render.render(**kwargs)
        else:
            raise


# 1) explicit opt-in: EEVEE scenes are unreliable on Kaggle (no real OpenGL display)
if CFG.get("eevee_to_cycles") and str(scene.render.engine).startswith("BLENDER_EEVEE"):
    eng = pick_engine(scene, "CYCLES")
    if eng:
        scene.render.engine = eng
        log("NOTE: scene engine switched from EEVEE to Cycles as requested")

# 2) the frames THIS job renders (the add-on decided them; everything else in the scene is untouched)
scene.frame_start = int(CFG["frame_start"])
scene.frame_end = int(CFG["frame_end"])
scene.frame_step = max(1, int(CFG["frame_step"]))

# 3) device selection LAST, so it sees the final engine (a CPU-saved Cycles scene must still get the GPU)
if FORCE_GPU:
    setup_render_device(scene)

total = len(range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
info = {
    "animation": True, "frames": total, "engine": scene.render.engine,
    "frame_start": scene.frame_start, "frame_end": scene.frame_end, "frame_step": scene.frame_step,
    "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage],
    "fps": scene.render.fps, "fps_base": scene.render.fps_base,
}
with open(os.path.join(OUT_DIR, "scene_info.json"), "w") as f:
    json.dump(info, f)
log("SCENE_INFO", json.dumps(info))

# Frames are always written as PNG, named <prefix>_<frame number>.png
safe_set_png_format(scene)
scene.render.use_file_extension = True
scene.render.filepath = os.path.join(OUT_DIR, CFG["prefix"] + "_####")
do_render(True)
log("RENDER_DONE")
'''


def build_blender_script(cfg, frames_dir, force_gpu):
    values = {"@@CFG@@": repr(json.dumps(cfg)), "@@OUT_DIR@@": repr(str(frames_dir)),
              "@@FORCE_GPU@@": repr(bool(force_gpu))}
    return re.sub(r"@@(?:CFG|OUT_DIR|FORCE_GPU)@@", lambda m: values[m.group(0)], BLENDER_SCRIPT)


def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()
    proc.wait()


def run_blender(blend, job_path, frames_dir):
    cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24",
           "blender", "-b", str(blend), "--python-exit-code", "1", "--python", str(job_path)]
    log("Starting Blender ...")
    t0 = time.time()
    total = None
    saved = 0
    failure = None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace",
                            start_new_session=True)  # own process group so we can kill Blender + xvfb together

    lines = queue.Queue()

    def reader():
        try:
            for raw in proc.stdout:
                lines.put(raw)
        except Exception as e:  # noqa: BLE001  never leave the main loop waiting for an end marker
            lines.put(f"WARNING: output reader stopped: {e}\n")
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    last_heartbeat = last_progress = last_output = time.time()
    eevee_scene = False
    t_first = None
    while True:
        if time.time() - t0 > MAX_RUN_SEC:
            _kill_group(proc)
            raise JobError(f"Kaggle session time limit reached after {saved} of {total or '?'} frames. "
                           "Split the render across more accounts (or lower samples/resolution) and run it again.")
        try:
            line = lines.get(timeout=5)
        except queue.Empty:  # Blender is silent: prove we are alive so a hang is visible in the log
            if eevee_scene and time.time() - last_output >= STALL_SEC:
                _kill_group(proc)
                raise JobError(
                    f"EEVEE printed nothing for {int(STALL_SEC // 60)} minutes, so it is almost certainly stuck "
                    "(Kaggle has no real OpenGL display). Use Cycles instead: turn on 'EEVEE→Cycles' in /render, "
                    "or pick Engine = Cycles in Custom mode.")
            if time.time() - last_heartbeat >= HEARTBEAT_SEC:
                log(f"  ... Blender still running ({int(time.time() - t0)}s elapsed, {saved} frame(s) saved)")
                last_heartbeat = time.time()
            continue
        if line is None:
            break
        last_output = time.time()
        if any(n in line for n in HARMLESS_NOISE):
            continue
        last_heartbeat = time.time()
        s = line.strip()
        if total is None:
            info = frames_dir / "scene_info.json"
            if info.exists():
                try:
                    data = json.loads(info.read_text())
                    total = int(data["frames"])
                    if "EEVEE" in str(data.get("engine", "")):
                        eevee_scene = True
                        log("  NOTE: this scene uses EEVEE. On Kaggle there is no real OpenGL display, so EEVEE "
                            "may fall back to software rendering and be extremely slow or hang. "
                            "Cycles (GPU) is the reliable choice here.")
                except Exception:
                    pass
        if "Saved:" in s:
            saved += 1
            if saved == 1:
                t_first = time.time()
            elif total and saved >= 4:   # project the total time early instead of failing after 9 hours
                est = (time.time() - t_first) / (saved - 1) * total
                if est > MAX_RUN_SEC * 1.1:
                    _kill_group(proc)
                    need = int(est / (MAX_RUN_SEC * 0.85)) + 1
                    raise JobError(f"Too slow for one Kaggle session: about {est / 3600:.1f} h for {total} frames "
                                   f"(limit ~9 h). Use at least {need} accounts or lower samples/resolution.")
            el = time.time() - t0
            per = el / saved
            if total:
                eta = max(0, total - saved) * per
                log(f"[{saved}/{total}] {per:.1f}s/frame, ETA {int(eta // 60)}m{int(eta % 60):02d}s")
            else:
                log(f"[{saved}] frames saved")
        elif s.startswith("Fra:"):  # Blender's own progress line (frame, memory, time, samples)
            if time.time() - last_progress >= PROGRESS_LOG_SEC:
                log("  " + s[:170])
                last_progress = time.time()
        elif any(k in s for k in ("SCENE_INFO", "RENDER_DONE", "GPU", "WARNING", "Error", "Traceback")):
            log(" ", s)
        if ("Error:" in s or "TypeError:" in s) and "RENDER_DONE" not in s \
                and "OptiX denoiser" not in s and "GPU denoiser creation" not in s \
                and "script failed" not in s and not failure:   # keep the FIRST real error, not Blender's generic last line
            failure = s
    proc.wait()
    log(f"Blender exited with code {proc.returncode} after {time.time() - t0:.0f}s")
    if proc.returncode != 0:
        raise JobError("Blender failed" + (f": {failure}" if failure else f" (exit code {proc.returncode})"))


# --------------------------------------------------------------------------- video (ffmpeg runs on Kaggle)
def ensure_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    log("ffmpeg not found, installing ...")
    subprocess.run(["apt-get", "update", "-qq"], capture_output=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], capture_output=True)
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "imageio-ffmpeg"], capture_output=True)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise JobError(f"Could not get ffmpeg on Kaggle: {e}")


def run_ffmpeg(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise JobError("ffmpeg failed: " + (r.stderr or "")[-400:])


def frame_rate(info):
    fps = Fraction(int(info.get("fps") or 24))
    base = Fraction(float(info.get("fps_base") or 1.0)).limit_denominator(1001)
    fr = fps / base
    return f"{fr.numerator}/{fr.denominator}"


def frame_no(path):
    m = re.search(r"_(-?\d+)\.png$", path.name)
    return int(m.group(1)) if m else 0


def encode_video(pngs, info, out_path, crf):
    ffmpeg = ensure_ffmpeg()
    seq = TMP / "rb_seq"
    shutil.rmtree(seq, ignore_errors=True)
    seq.mkdir(parents=True)
    for n, p in enumerate(sorted(pngs, key=frame_no), 1):  # gapless numbering, whatever the real frame numbers
        os.symlink(p, seq / f"f_{n:06d}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rate = frame_rate(info)
    log(f"Encoding {len(pngs)} frame(s) to video at {rate} fps (CRF {crf}) ...")
    run_ffmpeg([ffmpeg, "-y", "-loglevel", "error", "-framerate", rate, "-i", str(seq / "f_%06d.png"),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(int(crf)),
                "-movflags", "+faststart", str(out_path)])
    shutil.rmtree(seq, ignore_errors=True)
    log(f"Video ready: {out_path.name} ({out_path.stat().st_size / 1e6:.1f} MB)")


def run_merge(cfg):
    ffmpeg = ensure_ffmpeg()
    segs = [find_file(cfg["seg_slug"], n) for n in cfg["names"]]
    listing = TMP / "concat.txt"
    listing.write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
    out = WORK_OUT / "video" / cfg["video_name"]
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"Joining {len(segs)} segment(s) ...")
    run_ffmpeg([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-movflags", "+faststart", str(out)])
    log(f"Final video ready: {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return {"video": out.name, "segments": len(segs)}


# --------------------------------------------------------------------------- main
def run_job(cfg):
    if cfg.get("mode") == "merge":
        return run_merge(cfg)
    hardware = detect_hardware()
    install_blender()
    blend = find_file(cfg["blend_slug"], cfg["blend_name"])
    log("Blend file:", blend, "| frames:", cfg["frame_start"], "-", cfg["frame_end"], "step", cfg["frame_step"])

    frames_dir = TMP / "rb_frames"
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)
    job = TMP / "render_job.py"
    job.write_text(build_blender_script(cfg, frames_dir, cfg.get("force_gpu", True) and hardware == "GPU"),
                   encoding="utf-8")
    run_blender(blend, job, frames_dir)

    scene_info = frames_dir / "scene_info.json"
    if not scene_info.exists():
        raise JobError("Blender ended before preparing the scene (see the log above for the reason).")
    info = json.loads(scene_info.read_text())
    pngs = sorted(frames_dir.glob(cfg["prefix"] + "_*.png"))
    if len(pngs) < int(info["frames"]):  # never report a truncated render as a success
        raise JobError(f"Render incomplete: {len(pngs)} of {info['frames']} frames were produced.")
    if cfg.get("output") == "video":  # merge the frames into a video HERE; the PNGs stay in the temp folder
        video = WORK_OUT / "video" / cfg["video_name"]
        encode_video(pngs, info, video, cfg.get("crf", 18))
        shutil.rmtree(frames_dir, ignore_errors=True)
        return {"frames": len(pngs), "engine": info["engine"], "video": video.name}
    out = WORK_OUT / "frames"
    out.mkdir(parents=True, exist_ok=True)
    for p in pngs:
        shutil.move(str(p), str(out / p.name))
    log(f"{len(pngs)} frame(s) ready in {out}")
    shutil.rmtree(frames_dir, ignore_errors=True)
    return {"frames": len(pngs), "engine": info["engine"]}


def main():
    global _log_file
    WORK_OUT.mkdir(parents=True, exist_ok=True)
    _log_file = open(LOG_PATH, "w", encoding="utf-8")
    t0 = time.time()
    ok, status = False, {}
    try:
        status = run_job(CONFIG)
        ok = True
    except JobError as e:
        log("FAILED:", e)
        status = {"reason": str(e)}
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        status = {"reason": f"unexpected error: {e}"}
    status.update({"ok": ok, "seconds": int(time.time() - t0)})
    STATUS_PATH.write_text(json.dumps(status), encoding="utf-8")
    log("DONE" if ok else "ENDED WITH ERROR", json.dumps(status))
    _log_file.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
