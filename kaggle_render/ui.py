"""Blender side: preferences (accounts), the render panel, operators and the timer. Needs `bpy`."""
import os
import re
import shutil
import tempfile
from pathlib import Path

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty,
                       StringProperty)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup

from . import kaggle_cli, runner as runner_mod
from .kaggle_cli import Creds, KaggleError

PKG = __package__ or "kaggle_render"
_runner = None
_last_version = -1

STATUS_TEXT = {"upload": "uploading scene", "push": "starting", "queued": "waiting for GPU",
               "running": "rendering", "download": "downloading", "done": "done", "error": "FAILED"}
MERGE_TEXT = {"wait": "waiting for the parts", "upload": "uploading segments", "push": "starting merge",
              "queued": "waiting for Kaggle", "running": "merging on Kaggle", "download": "downloading video",
              "finish": "saving video", "done": "done", "error": "FAILED"}
STATUS_ICON = {"upload": "EXPORT", "push": "PLAY", "queued": "TIME", "running": "RENDER_ANIMATION",
               "download": "IMPORT", "done": "CHECKMARK", "error": "ERROR"}


def work_dir() -> Path:
    try:
        return Path(bpy.utils.extension_path_user(PKG, path="work", create=True))
    except Exception:  # noqa: BLE001  (running as a legacy add-on)
        d = Path(bpy.utils.user_resource("CONFIG", path="kaggle_render", create=True))
        return d


def prefs():
    return bpy.context.preferences.addons[PKG].preferences


# --------------------------------------------------------------------------- data
class KR_Account(PropertyGroup):
    name: StringProperty(name="Kaggle user")
    secret: StringProperty(name="Token / key", subtype="PASSWORD")
    kind: StringProperty()  # 'token' or 'legacy'
    use: BoolProperty(name="Use", description="Render on this account", default=True)


class KR_Settings(PropertyGroup):
    output_mode: EnumProperty(
        name="Output", default="VIDEO",
        items=[("VIDEO", "Video (mp4)", "Frames are merged into an mp4 ON Kaggle; only the final video is downloaded"),
               ("PNG", "PNG frames", "Download every frame as a PNG (no video)")])
    video_quality: EnumProperty(
        name="Quality", default="18",
        items=[("18", "High", "CRF 18: near lossless, bigger file"), ("23", "Medium", "CRF 23: balanced"),
               ("28", "Small file", "CRF 28: smaller file, visible compression")])
    output_dir: StringProperty(name="Save to", subtype="DIR_PATH",
                               description="Folder that receives the final video (or the PNG frames)")
    prefix: StringProperty(name="File name", default="",
                           description="Video: <name>.mp4. PNG: <name>_0001.png. Empty = the .blend file name")
    use_scene_range: BoolProperty(name="Scene frame range", default=True)
    frame_start: IntProperty(name="Start", default=1, min=0)
    frame_end: IntProperty(name="End", default=250, min=0)
    frame_step: IntProperty(name="Step", default=1, min=1)
    eevee_to_cycles: BoolProperty(name="EEVEE -> Cycles", default=False,
                                  description="EEVEE is unreliable on Kaggle: render EEVEE scenes with Cycles")
    pack_resources: BoolProperty(name="Pack textures into the upload", default=True,
                                 description="Textures that live outside the .blend are packed into the uploaded "
                                             "copy (your open file is not changed)")
    delete_dataset: BoolProperty(name="Delete the uploaded scene afterwards", default=True)


class KR_Prefs(AddonPreferences):
    bl_idname = PKG

    accounts: CollectionProperty(type=KR_Account)
    new_username: StringProperty(name="Kaggle username", description="Only needed for old-style API keys")
    new_secret: StringProperty(name="Token or API key", subtype="PASSWORD")

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Kaggle accounts", icon="USER")
        for i, a in enumerate(self.accounts):
            row = col.row()
            row.label(text=a.name, icon="CHECKMARK")
            op = row.operator("kaggle_render.remove_account", text="", icon="X")
            op.index = i
        box = col.box()
        box.label(text="Add an account (get a token from kaggle.com > Settings > API)")
        box.prop(self, "new_username")
        box.prop(self, "new_secret")
        box.operator("kaggle_render.add_account", icon="ADD")
        col.label(text="Tokens are stored in Blender's preferences as plain text on this computer.", icon="INFO")


# --------------------------------------------------------------------------- operators
class KR_OT_add_account(Operator):
    bl_idname = "kaggle_render.add_account"
    bl_label = "Verify and add"
    bl_description = "Checks the token with Kaggle and adds the account"

    def execute(self, context):
        p = prefs()
        secret = p.new_secret.strip()
        if not secret:
            self.report({"ERROR"}, "Paste the token (or API key) first.")
            return {"CANCELLED"}
        kind = "token" if secret.startswith("KGAT_") else "legacy"
        user = p.new_username.strip()
        if kind == "legacy" and not user:
            self.report({"ERROR"}, "An old-style API key needs the Kaggle username too.")
            return {"CANCELLED"}
        try:
            real = kaggle_cli.verify(Creds(user, secret, kind))
        except kaggle_cli.AuthError:
            self.report({"ERROR"}, "Kaggle refused this token/key.")
            return {"CANCELLED"}
        except KaggleError as e:
            self.report({"ERROR"}, f"Could not reach Kaggle: {str(e)[-150:]}")
            return {"CANCELLED"}
        existing = next((a for a in p.accounts if a.name.lower() == real.lower()), None)
        acc = existing or p.accounts.add()
        acc.name, acc.secret, acc.kind, acc.use = real, secret, kind, True
        p.new_secret = ""
        p.new_username = ""
        self.report({"INFO"}, f"Account '{real}' is ready.")
        return {"FINISHED"}


class KR_OT_remove_account(Operator):
    bl_idname = "kaggle_render.remove_account"
    bl_label = "Remove account"
    index: IntProperty()

    def execute(self, context):
        accounts = prefs().accounts
        if 0 <= self.index < len(accounts):   # the list may have changed since the button was drawn
            accounts.remove(self.index)
        return {"FINISHED"}


class KR_OT_open_prefs(Operator):
    bl_idname = "kaggle_render.open_prefs"
    bl_label = "Add Kaggle accounts"

    def execute(self, context):
        bpy.ops.screen.userpref_show(section="ADDONS")
        return {"FINISHED"}


def _pack_for_upload(report) -> list:
    """Packs loose image files so the uploaded COPY is self-contained. Returns what we packed (to unpack after)."""
    packed = []
    for img in bpy.data.images:
        try:
            if img.source == "FILE" and img.packed_file is None and img.filepath:
                path = bpy.path.abspath(img.filepath)
                if os.path.isfile(path):
                    img.pack()
                    packed.append(img)
        except Exception as e:  # noqa: BLE001
            report(f"Could not pack {img.name}: {e}")
    return packed


def _unpack_again(packed: list) -> None:
    for img in packed:
        try:
            img.unpack(method="USE_ORIGINAL")
        except Exception:  # noqa: BLE001
            pass


def _preflight() -> list:
    """Things that usually look different on Kaggle than on this PC (the render still starts)."""
    out = []
    try:
        if bpy.data.libraries:
            out.append(f"{len(bpy.data.libraries)} linked .blend librar{'y' if len(bpy.data.libraries) == 1 else 'ies'} "
                       "are not uploaded (make the assets local)")
        missing = [i.name for i in bpy.data.images
                   if i.source == "FILE" and i.filepath and i.packed_file is None
                   and not os.path.exists(bpy.path.abspath(i.filepath, library=i.library))]
        if missing:
            out.append(f"{len(missing)} texture file(s) are missing on this PC (e.g. {missing[0]}) and will be pink")
        seq = [i.name for i in bpy.data.images if i.source in {"SEQUENCE", "MOVIE"} and i.filepath]
        if seq or [c for c in bpy.data.movieclips if c.filepath] or [v for v in bpy.data.volumes if v.filepath]:
            out.append("image sequences, movie clips and volume (VDB) files are not uploaded")
        if bpy.app.version[:2] > (5, 2):
            out.append(f"Blender {bpy.app.version_string} is newer than the Blender used on Kaggle (5.2): "
                       "newer features may not render")
    except Exception:  # noqa: BLE001 - a check must never block a render
        pass
    return out


class KR_OT_render(Operator):
    bl_idname = "kaggle_render.render"
    bl_label = "Render on Kaggle"
    bl_description = "Uploads this scene to Kaggle and renders the frames on its GPUs"

    def execute(self, context):
        global _runner
        s = context.scene.kaggle_render
        p = prefs()
        accounts = [a.name for a in p.accounts if a.use]
        if not accounts:
            self.report({"ERROR"}, "Tick at least one Kaggle account (add them in the add-on preferences).")
            return {"CANCELLED"}
        if not s.output_dir.strip():
            self.report({"ERROR"}, "Choose the folder where the result should be saved.")
            return {"CANCELLED"}
        out_dir = bpy.path.abspath(s.output_dir)
        if s.output_dir.startswith("//") and not bpy.data.filepath:
            self.report({"ERROR"}, "Save the .blend first, or pick a normal folder path.")
            return {"CANCELLED"}
        sc = context.scene
        if sc.camera is None:
            self.report({"ERROR"}, "The scene has no active camera. Add one (Add > Camera) and set it in "
                                   "Scene properties > Camera, then try again.")
            return {"CANCELLED"}
        frames = (sc.frame_start, sc.frame_end, sc.frame_step) if s.use_scene_range \
            else (s.frame_start, s.frame_end, s.frame_step)
        if frames[1] < frames[0]:
            self.report({"ERROR"}, "The end frame is before the start frame.")
            return {"CANCELLED"}
        try:
            probe = Path(out_dir)
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            if shutil.disk_usage(probe).free < 500 * 1024 ** 2:
                self.report({"ERROR"}, "Less than 500 MB free in the output folder. Free some space or choose another folder.")
                return {"CANCELLED"}
        except OSError:
            pass
        warns = _preflight()
        if warns:
            self.report({"WARNING"}, "Heads up: " + " | ".join(warns))
        label = Path(bpy.data.filepath).name if bpy.data.filepath else "untitled.blend"
        base = Path(label).stem
        prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", s.prefix.strip() or base).strip("_") or "frame"
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.report({"ERROR"}, f"Cannot use that folder: {e}")
            return {"CANCELLED"}

        tmp_dir = Path(tempfile.mkdtemp(prefix="kr_"))
        tmp = tmp_dir / "scene.blend"
        packed = _pack_for_upload(lambda m: self.report({"WARNING"}, m)) if s.pack_resources else []
        try:
            bpy.ops.wm.save_as_mainfile(filepath=str(tmp), copy=True, compress=True)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self.report({"ERROR"}, f"Could not save a copy of the scene: {e}")
            return {"CANCELLED"}
        finally:
            _unpack_again(packed)
        try:
            ensure_runner().start_job(label, tmp, frames, accounts, out_dir, prefix,
                                      eevee=s.eevee_to_cycles, delete_dataset=s.delete_dataset,
                                      output="video" if s.output_mode == "VIDEO" else "png",
                                      crf=int(s.video_quality))
        except KaggleError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001  (disk full while moving the scene, unwritable work folder, ...)
            self.report({"ERROR"}, f"Could not start the render: {e}")
            return {"CANCELLED"}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)   # start_job moved the scene out; never leave the temp folder behind
        n = len(accounts)
        self.report({"INFO"}, f"Started on {n} account{'s' if n > 1 else ''}. You can keep working in Blender.")
        return {"FINISHED"}


class KR_OT_open_folder(Operator):
    bl_idname = "kaggle_render.open_folder"
    bl_label = "Open frames folder"
    path: StringProperty()

    def execute(self, context):
        if os.path.isdir(self.path):
            bpy.ops.wm.path_open(filepath=self.path)
        return {"FINISHED"}


class KR_OT_open_url(Operator):
    bl_idname = "kaggle_render.open_url"
    bl_label = "Open on Kaggle"
    url: StringProperty()

    def execute(self, context):
        bpy.ops.wm.url_open(url=self.url)
        return {"FINISHED"}


class KR_OT_retry(Operator):
    bl_idname = "kaggle_render.retry"
    bl_label = "Retry"
    job: StringProperty()
    part: IntProperty()

    def execute(self, context):
        ensure_runner().retry_part(self.job, self.part)
        return {"FINISHED"}


class KR_OT_remove_job(Operator):
    bl_idname = "kaggle_render.remove_job"
    bl_label = "Remove from list"
    bl_description = ("Remove this render from the list. For a running render this only STOPS TRACKING it: "
                      "Kaggle keeps rendering until it finishes (stop it on the Kaggle page to save quota)")
    job: StringProperty()
    force: BoolProperty(default=False)

    def invoke(self, context, event):
        if self.force:
            try:
                return context.window_manager.invoke_confirm(self, event)
            except Exception:  # noqa: BLE001  (older Blender: no confirm popup)
                pass
        return self.execute(context)

    def execute(self, context):
        if not ensure_runner().remove_job(self.job, force=self.force):
            self.report({"WARNING"}, "This render is still running.")
        return {"FINISHED"}


# --------------------------------------------------------------------------- panel
class KR_PT_panel(Panel):
    bl_label = "Kaggle Render"
    bl_idname = "KR_PT_panel"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        lay = self.layout
        s = context.scene.kaggle_render
        p = prefs()
        if not p.accounts:
            lay.label(text="No Kaggle account yet.", icon="INFO")
            lay.operator("kaggle_render.open_prefs", icon="PREFERENCES")
        else:
            box = lay.box()
            box.label(text="Accounts (tick the ones to use)", icon="USER")
            for a in p.accounts:
                box.prop(a, "use", text=a.name)
        lay.prop(s, "output_mode", expand=True)
        if s.output_mode == "VIDEO":
            lay.prop(s, "video_quality")
        col = lay.column(align=True)
        col.prop(s, "output_dir")
        col.prop(s, "prefix")
        col = lay.column(align=True)
        col.prop(s, "use_scene_range")
        if not s.use_scene_range:
            row = col.row(align=True)
            row.prop(s, "frame_start")
            row.prop(s, "frame_end")
            row.prop(s, "frame_step")
        lay.prop(s, "eevee_to_cycles")
        lay.prop(s, "pack_resources")
        lay.prop(s, "delete_dataset")
        n = sum(1 for a in p.accounts if a.use)
        row = lay.row()
        row.scale_y = 1.5
        row.enabled = n > 0
        row.operator("kaggle_render.render", icon="RENDER_ANIMATION",
                     text="Render on Kaggle" if n <= 1 else f"Split across {n} accounts")

        jobs = list(reversed(_runner.snapshot())) if _runner else []
        if jobs:
            lay.separator()
            lay.label(text="Renders", icon="TIME")
        for job in jobs[:8]:
            box = lay.box()
            head = box.row()
            head.label(text=f"{job['label']}  ({job['id']})", icon="FILE_BLEND")
            if job["status"] != "running":
                head.operator("kaggle_render.remove_job", text="", icon="X").job = job["id"]
            else:
                op = head.operator("kaggle_render.remove_job", text="Stop tracking", icon="CANCEL")
                op.job, op.force = job["id"], True
            for part in job["parts"]:
                row = box.row(align=True)
                fs = part["fs"]
                row.label(text=f"{part['account']}  [{fs[0]}-{fs[1]}]  {STATUS_TEXT.get(part['status'], part['status'])}",
                          icon=STATUS_ICON.get(part["status"], "DOT"))
                if part["url"]:
                    row.operator("kaggle_render.open_url", text="", icon="URL").url = part["url"]
                if part["status"] == "error" and part.get("failed_at"):
                    op = row.operator("kaggle_render.retry", text="Retry")
                    op.job, op.part = job["id"], part["i"]
                if part["msg"] and part["status"] in ("error", "download", "push"):
                    for line in _wrap(part["msg"], 46)[:4]:
                        box.label(text=line)
            m = job.get("merge")
            if m:
                row = box.row(align=True)
                row.label(text=f"Video merge  {MERGE_TEXT.get(m['status'], m['status'])}",
                          icon=STATUS_ICON.get(m["status"], "SEQUENCE") if m["status"] != "wait" else "SEQUENCE")
                if m["url"]:
                    row.operator("kaggle_render.open_url", text="", icon="URL").url = m["url"]
                if m["status"] == "error" and m.get("failed_at"):
                    op = row.operator("kaggle_render.retry", text="Retry")
                    op.job, op.part = job["id"], -1
                if m["msg"] and m["status"] in ("error", "download", "push"):
                    for line in _wrap(m["msg"], 46)[:4]:
                        box.label(text=line)
            if job["status"] == "done" and m:
                row = box.row()
                row.label(text=f"Saved: {Path(m.get('file') or '').name}", icon="CHECKMARK")
                row.operator("kaggle_render.open_folder", text="Open folder", icon="FILE_FOLDER").path = job["out_dir"]
            elif job["status"] == "done":
                total = sum(pt["files"] for pt in job["parts"])
                row = box.row()
                row.label(text=f"{total} frames saved", icon="CHECKMARK")
                row.operator("kaggle_render.open_folder", text="Open folder", icon="FILE_FOLDER").path = job["out_dir"]
            elif job["status"] == "error":
                box.operator("kaggle_render.open_folder", text="Open folder", icon="FILE_FOLDER").path = job["out_dir"]


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    out = []
    for line in str(text).splitlines() or [""]:
        out += textwrap.wrap(line, width) or [""]
    return out


# --------------------------------------------------------------------------- runner + timer
def ensure_runner() -> runner_mod.Runner:
    global _runner
    if _runner is None:
        kaggle_cli.configure(work_dir())
        _runner = runner_mod.Runner(work_dir())
    return _runner


def _creds_from_prefs() -> dict[str, Creds]:
    out = {}
    for a in prefs().accounts:
        out[a.name] = Creds(a.name, a.secret, a.kind or "legacy")
    return out


def _tick():
    global _last_version
    try:
        r = ensure_runner()
        r.update_creds(_creds_from_prefs())
        r.resume()
        if r.version != _last_version:
            _last_version = r.version
            for win in bpy.context.window_manager.windows:
                for area in win.screen.areas:
                    if area.type == "PROPERTIES":
                        area.tag_redraw()
    except Exception as e:  # noqa: BLE001 - the timer must survive anything
        print("[kaggle_render] tick error:", e)
    return 1.0


CLASSES = (KR_Account, KR_Settings, KR_Prefs, KR_OT_add_account, KR_OT_remove_account, KR_OT_open_prefs,
           KR_OT_render, KR_OT_open_folder, KR_OT_open_url, KR_OT_retry, KR_OT_remove_job, KR_PT_panel)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.kaggle_render = PointerProperty(type=KR_Settings)
    bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)


def unregister():
    global _runner
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _runner:
        _runner.shutdown()   # the jobs stay saved and are resumed next time
        _runner = None
    del bpy.types.Scene.kaggle_render
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
