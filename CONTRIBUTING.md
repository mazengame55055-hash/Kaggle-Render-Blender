# Contributing / المشاركة في التطوير

## الخطوات / Steps
1. اعمل **Fork** للمشروع من زرار Fork فوق.
2. حمّل نسختك: `git clone https://github.com/<اسمك>/kaggle-render.git`
3. اعمل branch: `git checkout -b my-fix`
4. عدّل الكود جوه فولدر `kaggle_render/` (ده كود الإضافة).
5. جرّب: `python tools/build_zip.py` وثبّت الـ zip من `dist/` في Blender.
6. `git add . && git commit -m "وصف التعديل" && git push origin my-fix`
7. افتح **Pull Request** على GitHub.

## قواعد بسيطة / Rules
- لا ترفع أي توكن أو `kaggle.json` (الـ `.gitignore` بيمنعها، بس خد بالك).
- لو غيّرت سلوك، حدّث `CHANGELOG.md` ورقم `version` في `kaggle_render/blender_manifest.toml`.
- الرخصة GPL-3.0-or-later.
