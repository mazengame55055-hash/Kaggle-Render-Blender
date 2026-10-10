# Changelog

## 1.2.1 (bug-fix release)
- A damaged or unexpected `jobs.json` (empty, `null`, a list, missing fields, files from older versions) no longer breaks the
  add-on at startup: unusable entries are skipped, the rest is kept and missing fields are filled in.
- **Retry** now works for every failure that shows the button: failed uploads (the scene copy is kept for that), failed or
  cancelled Kaggle kernels and failed merges (the kernel is started again). Retrying a vanished render no longer raises.
- Cancelled runs (`CANCEL_ACKNOWLEDGED`, `CANCEL_REQUESTED`) are recognised; before, they were polled forever.
- A rejected token / missing kernel fails after 4 polls instead of ~20 minutes; real network trouble is still retried.
- Error explanations are precise: Blender messages that merely contain `401`, `phone` or `quota` are no longer shown as
  Kaggle account problems; render failures never get account hints and are never retried as "network problems".
- `datasets status` text like "not ready" is no longer mistaken for "ready".
- Two Blender windows: the owner file is written atomically (a half-written file could let both windows drive the jobs).
- An account removed from the preferences keeps working for renders already running with it.
- Negative frame numbers are encoded in the right order.
- The temporary scene folder is removed after every start attempt (it leaked on errors / when another window was in charge).
- Long Kaggle CLI calls have (very long) timeouts, so a stalled connection is retried instead of hanging forever.
- Removing an account with a stale index in the preferences no longer raises.

## 1.2.0
- An existing video is never overwritten: a new one is saved as name_2.mp4, name_3.mp4 ...
- **Stop tracking** button for running renders (Kaggle keeps rendering; the datasets left on Kaggle are cleaned up).
- Removing a finished/failed render also deletes its scene/segment datasets from Kaggle (storage).
- Plain-language hints for the common Kaggle errors (rejected token, quota, phone verification).
- Pre-flight warnings before the upload: linked libraries, missing textures, image sequences / movie clips / VDB,
  Blender version newer than the one on Kaggle. Refuses to start with less than 500 MB free in the output folder.

## 1.1.0
- Crash / power-cut safety: jobs.json is fsynced and has a backup; a damaged file is restored from the backup.
- After Blender or the PC restarts, the add-on resumes every job. It never uploads the scene or starts a kernel twice
  (it detects what already exists on Kaggle).
- Network problems are retried automatically (up to 10 times with waiting) instead of failing the part.
- Only one Blender window drives the jobs at a time.
- Lost temporary video files are downloaded again from Kaggle instead of failing.
- Kaggle session guard: a part that would not finish within ~9 h is stopped early with advice on how many accounts to use.

## 1.0.1
- Checks that the scene has an active camera before uploading.
- Shows the first real Blender error instead of the generic "script failed".

## 1.0.0
- Video output: frames are encoded to mp4 on Kaggle; only the final video is downloaded.
- Several accounts: each renders a slice, a small CPU kernel on Kaggle joins the segments.
- PNG frame output is still available.
