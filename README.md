# 🎬 Kaggle Render

إضافة Blender بترندر مشهدك على كروت Kaggle المجانية وتنزّل لك الفيديو النهائي (mp4) بس.
*Blender add-on: render on Kaggle's free GPUs, download only the finished mp4.*

![Blender](https://img.shields.io/badge/Blender-4.2%2B-orange) ![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue) ![Version](https://img.shields.io/badge/version-1.2.1-green)

## التثبيت
1. من [**Releases**](../../releases) نزّل `kaggle_render-X.X.X.zip` (**ماتفكّهوش**).
2. Blender ← `Edit > Preferences > Get Extensions` ← ⌄ ← **Install from Disk...** ← اختار الـ zip.
3. فعّل **Kaggle Render**.

> زرار "Download ZIP" الأخضر مش ملف التثبيت.

## إضافة حساب Kaggle
kaggle.com ← **Settings > API > Create New Token** ← في Blender: `Preferences > Add-ons > Kaggle Render` ← الصق التوكين ← **Verify and add**.

## الاستخدام
`Properties > Render > Kaggle Render` ← اختار الحساب والـ Output والفولدر ← **Render on Kaggle** ← **Open folder** لما يخلص.

💡 جرّب كام فريم الأول، لازم Camera فعّالة، واستخدم **Cycles**.

## تنبيهات
- التوكن سري: ماتشاركوش ولا ترفعه على GitHub.
- التزم بشروط Kaggle (الحصة أسبوعية لكل حساب).
- مش مدعوم: الصوت، UDIM، Linked Libraries.

📖 تفاصيل أكتر: [`docs/`](docs/) · 🤝 [`CONTRIBUTING.md`](CONTRIBUTING.md) · 📜 GPL-3.0-or-later
