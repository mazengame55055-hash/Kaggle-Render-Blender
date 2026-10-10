# Kaggle Render

Render your Blender scene on Kaggle's free GPUs without leaving Blender. The frames are encoded into an
**mp4 on Kaggle**, and only the **finished video** is downloaded: no PNG sequence and no ffmpeg on your PC.
(Arabic guide: `README_AR.md`)

## Install (Blender 4.2+)
`Edit > Preferences > Get Extensions > (v) > Install from Disk...` and pick the zip. Enable **Kaggle Render**.

## Add a Kaggle account
1. kaggle.com > **Settings > API > Create New Token** (starts with `KGAT_`).
2. Blender: `Preferences > Add-ons > Kaggle Render`, paste the token, click **Verify and add**.

## Use
`Properties > Render > Kaggle Render`:
1. Choose **Output**: Video (mp4) or PNG frames, and the quality.
2. Choose **Save to** and an optional **File name**.
3. Click **Render on Kaggle**. You can keep working or close Blender; the add-on resumes watching the job.
4. When it finishes, click **Open folder**.

The scene needs an active camera. Try a few frames first (untick *Scene frame range*).

## How merging works
- One account: Kaggle renders and encodes the video, you download it.
- Several accounts: each encodes its slice; a small CPU kernel on Kaggle joins the slices. Temporary segments pass
  through the add-on's work folder and are deleted automatically.

## If something goes wrong
| What happened | What the add-on does |
|---|---|
| Blender crashes / is closed / PC is turned off | Kaggle keeps rendering. Open Blender again: the job resumes by itself (Kaggle quota is used meanwhile). |
| Power cut while saving its state | The state file is restored from its backup copy. |
| Internet drops | Retried automatically up to 10 times; if it still fails the part shows **Retry**. |
| Crash during upload or right after starting the kernel | Nothing is uploaded or started twice. |
| Two Blender windows open | Only the first one controls the jobs. |
| Temporary video file deleted | It is downloaded again from Kaggle. |
| One part fails (upload, kernel, merge) | The other parts continue. Use **Retry** on the failed one: it redoes only that step. |
| A run is cancelled on Kaggle | Shown as failed with **Retry**. |
| `jobs.json` is damaged or from an old version | Usable jobs are kept, broken entries are skipped. |
| Part too long for one Kaggle session (~9 h) | Stopped early with a message telling how many accounts to use. |
| Kaggle rejects the token / quota is used up / phone not verified | A plain-language explanation is shown with the Kaggle message. |
| A video with the same name already exists | It is kept; the new one is saved as `name_2.mp4`. |
| You want to give up on a running render | **Stop tracking** (Kaggle keeps rendering: stop it on its page to save quota). |

## Notes
- A copy of your open .blend is uploaded (your file is not changed). External textures can be packed into the copy.
- Kaggle's weekly GPU quota applies per account. Use only accounts you own and follow Kaggle's Terms of Use;
  do not create accounts to get around its limits.
- Tokens are stored in Blender's preferences as plain text on your computer.
- Not supported: audio, UDIM/sequence textures, linked libraries. EEVEE is unreliable on Kaggle: use Cycles
  (or enable *EEVEE -> Cycles*).

License: GPL-3.0-or-later. Not affiliated with Kaggle or the Blender Foundation.
