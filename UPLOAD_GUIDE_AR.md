# 🚀 دليل رفع المشروع على GitHub (من الصفر)

الفولدر ده **جاهز للرفع زي ما هو**. اتبع **طريقة 1 (من المتصفح، الأسهل)** أو **طريقة 2 (بالـ Git)**.

---

## قبل ما ترفع (دقيقتين)

1. افتح `kaggle_render/blender_manifest.toml` وعدّل السطرين:
   ```toml
   maintainer = "YOUR NAME <you@example.com>"      # ← اكتب اسمك وإيميلك
   # website = "https://github.com/YOUR_NAME/kaggle-render"   # ← شيل # وحط رابط مشروعك بعد ما تنشئه
   ```
2. تأكد إن مفيش `kaggle.json` أو أي توكن جوه الفولدر (مفيش فعلًا).

---

## طريقة 1: من المتصفح (من غير ما تثبّت حاجة)

1. ادخل على https://github.com وسجّل دخول (أو اعمل حساب مجاني).
2. اضغط **+** فوق يمين ← **New repository**.
3. املا:
   - **Repository name:** `kaggle-render`
   - **Description:** `Blender add-on: render on Kaggle GPUs and get the finished video`
   - اختار **Public**
   - ⚠️ **ماتعلّمش** على Add a README / .gitignore / license (إحنا عندنا كلهم).
   - اضغط **Create repository**.
4. في الصفحة الجديدة اضغط على الرابط الأزرق **uploading an existing file**.
5. افتح فولدر المشروع على جهازك واسحب **كل محتوياته** (مش الفولدر الخارجي) داخل المتصفح:
   `kaggle_render/` · `tools/` · `docs/` · `.github/` · `README.md` · `LICENSE` · `CHANGELOG.md` · `CONTRIBUTING.md` · `UPLOAD_GUIDE_AR.md` · `.gitignore`
   - ⚠️ الملفات اللي اسمها بيبدأ بنقطة (`.github` و`.gitignore`) مخفية في Windows/Mac. فعّل **Show hidden files** (Windows: View ← Hidden items | Mac: `Cmd+Shift+.`).
   - لو المتصفح ماقبلش فولدر `.github`، ارفعه بعدين: **Add file ← Upload files** ← اسحبه تاني.
6. اكتب في Commit message: `Initial commit` ← **Commit changes**.

## طريقة 2: بالـ Git (من الـ Terminal)

لازم يكون عندك [Git](https://git-scm.com/downloads) متثبّت. أنشئ الـ repo فاضي زي الخطوات 1-3 فوق، وبعدين جوه فولدر المشروع:

```bash
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<اسمك-على-GitHub>/kaggle-render.git
git push -u origin main
```
(هيطلب منك تسجيل دخول؛ استخدم Personal Access Token بدل الباسورد: GitHub ← Settings ← Developer settings ← Tokens.)

**أو بـ GitHub CLI** (أسرع):
```bash
gh auth login
gh repo create kaggle-render --public --source=. --push
```

---

## 📦 نشر ملف التثبيت (Release) — مهم جدًا

المستخدمين محتاجين ملف الـ zip عشان يثبّتوا الإضافة في Blender.

### الطريقة الأوتوماتيك (موصى بها)
```bash
git tag v1.2.1
git push origin v1.2.1
```
هيشتغل GitHub Actions لوحده (شوف تبويب **Actions**)، وبعد دقيقة هتلاقي الـ zip في **Releases**.

### الطريقة اليدوية
1. على جهازك: `python tools/build_zip.py` ← هيطلع `dist/kaggle_render-1.2.1.zip`.
2. على GitHub: **Releases ← Draft a new release** ← Tag: `v1.2.1` ← اسحب الـ zip ← **Publish release**.

---

## ✅ بعد الرفع
- راجع إن الـ README ظاهر في الصفحة الرئيسية.
- جرّب تنزّل الـ zip من Releases وتثبّته في Blender.
- (اختياري) في **Settings** ← **About ⚙️** ضيف Topics: `blender` `blender-addon` `kaggle` `render`.

## 🔄 لما تطلّع نسخة جديدة
1. عدّل `version` في `kaggle_render/blender_manifest.toml` وسجّل التغييرات في `CHANGELOG.md`.
2. `git add . && git commit -m "v1.2.2" && git push`
3. `git tag v1.2.2 && git push origin v1.2.2`
