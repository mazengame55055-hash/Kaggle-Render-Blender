<div align="center">

# 🎬 Kaggle Render

**Blender add-on: render your scene on Kaggle's free GPUs and download only the finished video.**
**إضافة Blender: ارندر مشهدك على كروت Kaggle المجانية وانزّل الفيديو النهائي بس.**

![Blender](https://img.shields.io/badge/Blender-4.2%2B-orange) ![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue) ![Version](https://img.shields.io/badge/version-1.2.1-green)

[العربية](#-بالعربي) · [English](#-english)

</div>

---

## 🇪🇬 بالعربي

### إيه هي الإضافة؟
بتبعت مشهد Blender بتاعك لـ Kaggle، الرندر بيتم على GPU هناك، والفريمات بتتجمّع في **فيديو mp4 على Kaggle نفسه**، وبينزل لك الفيديو النهائي بس.
مفيش ffmpeg على جهازك ومفيش سيرفر ولا Discord. تقدر تقفل Blender والرندر يكمّل، ولما تفتحه تاني الإضافة تكمّل المتابعة لوحدها.

### المتطلبات
- Blender **4.2 أو أحدث**
- حساب Kaggle (مجاني) — يفضّل توثيق رقم التليفون
- نت

### 1) التحميل والتثبيت (للمستخدم العادي)
1. ادخل على صفحة [**Releases**](../../releases) في المشروع ده.
2. نزّل ملف **`kaggle_render-X.X.X.zip`** (من قسم Assets). ⚠️ **ماتفكّهوش**.
   - لو مفيش Release لسه: نزّل المشروع، شغّل `python tools/build_zip.py`، وهتلاقي الـ zip في فولدر `dist/`.
   - ⚠️ زرار "Download ZIP" الأخضر بتاع GitHub **مش** هو ملف التثبيت.
3. في Blender: `Edit > Preferences > Get Extensions` ← السهم الصغير (⌄) فوق يمين ← **Install from Disk...** ← اختار الـ zip.
4. فعّل **Kaggle Render**.

### 2) إضافة حساب Kaggle
1. من kaggle.com: **Settings > API > Create New Token** (التوكن بيبدأ بـ `KGAT_`).
2. في Blender: `Preferences > Add-ons > Kaggle Render` ← الصق التوكين ← **Verify and add**.
3. كرر لكل حساب عايز تستعمله (حسابات تملكها أنت بس).

### 3) الاستخدام
`Properties > Render > Kaggle Render`:
1. علّم الحسابات اللي هتشارك (حساب واحد = رندر عادي، أكتر = تقسيم الفريمات).
2. اختار **Output**: `Video (mp4)` أو `PNG frames`، وإعدادات الجودة.
3. اختار **Save to** (فولدر الحفظ) و**File name** (اختياري).
4. اضغط **Render on Kaggle**.
5. لما يخلص اضغط **Open folder**.

> 💡 جرّب كام فريم الأول (شيل علامة *Scene frame range*). المشهد لازم يكون فيه Camera فعّالة. استخدم **Cycles**.

📖 الشرح الكامل + جدول حل المشاكل: [`docs/ADDON_README_AR.md`](docs/ADDON_README_AR.md)

### ⚠️ تنبيهات مهمة
- التوكن بيتخزن في تفضيلات Blender كنص عادي على جهازك. **ماتشاركوش مع حد ولا ترفعه على GitHub.**
- التزم بشروط استخدام Kaggle. الحصة الأسبوعية للـ GPU على كل حساب، ومينفعش تعمل حسابات للتحايل عليها.
- مش مدعوم: الصوت، UDIM/Image Sequence، الـ Linked Libraries. EEVEE مش مضمون على Kaggle.

---

## 🇬🇧 English

### What is it?
Sends your Blender scene to Kaggle, renders on Kaggle's GPU, encodes frames into an **mp4 on Kaggle**, and downloads **only the final video**. No local ffmpeg, no server. You can close Blender; the add-on resumes watching the job when you reopen it.

### Requirements
Blender **4.2+**, a free Kaggle account, internet.

### Install
1. Open [**Releases**](../../releases) and download **`kaggle_render-X.X.X.zip`** (do **not** unzip it).
   - No release yet? Run `python tools/build_zip.py` → zip is created in `dist/`.
   - The green "Download ZIP" button on GitHub is **not** the installable file.
2. Blender: `Edit > Preferences > Get Extensions > (⌄) > Install from Disk...` → pick the zip → enable **Kaggle Render**.

### Add a Kaggle account
Kaggle → **Settings > API > Create New Token** (starts with `KGAT_`) → Blender `Preferences > Add-ons > Kaggle Render` → paste → **Verify and add**.

### Use
`Properties > Render > Kaggle Render` → choose accounts, output (mp4 / PNG), save folder → **Render on Kaggle** → **Open folder** when done.

Full docs & troubleshooting table: [`docs/ADDON_README_EN.md`](docs/ADDON_README_EN.md)

---

## 📁 هيكل المشروع / Repository layout

```
kaggle-render/
├── kaggle_render/            ← كود الإضافة نفسه (ده اللي بيتضغط في الـ zip)
│   ├── blender_manifest.toml    معلومات الإضافة (الاسم، النسخة، المطوّر، الصلاحيات)
│   ├── __init__.py              نقطة دخول الإضافة (register/unregister)
│   ├── ui.py                    الواجهة داخل Blender
│   ├── runner.py                منطق الشغل: رفع، تشغيل، متابعة، تنزيل، استئناف
│   ├── kaggle_cli.py            التعامل مع Kaggle API
│   ├── kernel_job.py            السكريبت اللي بيشتغل على Kaggle (رندر + ffmpeg + دمج)
│   └── wheels/                  مكتبات Python اللازمة، متضمّنة جوه الإضافة (offline)
├── tools/build_zip.py        ← بيبني ملف التثبيت dist/kaggle_render-<version>.zip
├── .github/workflows/        ← بناء Release أوتوماتيك لما ترفع tag v*
├── docs/                     ← الشرح القديم بالعربي والإنجليزي
├── CHANGELOG.md · CONTRIBUTING.md · LICENSE (GPL-3.0-or-later)
└── UPLOAD_GUIDE_AR.md        ← خطوات رفع المشروع على GitHub (للي هيرفعه)
```

## 🤝 المشاركة / Contributing
شوف [`CONTRIBUTING.md`](CONTRIBUTING.md).

## 📜 الرخصة / License
GPL-3.0-or-later. غير تابعة لـ Kaggle ولا لـ Blender Foundation. / Not affiliated with Kaggle or the Blender Foundation.
