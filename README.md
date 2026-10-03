# myYouTube Download Robot 🤖🎬

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![YT-DLP](https://img.shields.io/badge/yt--dlp-latest-red)

<p align="center">
  <img src="assets/cover.png" alt="Cover" width="100%">
</p>

ربات تلگرامی دانلود ویدیو و موزیک از یوتیوب؛ کافی است لینک را بفرستید،
کیفیت دلخواه را انتخاب کنید تا فایل با سرعت بالا و بدون افت کیفیت برایتان ارسال شود.

## ✨ امکانات

- 📥 دریافت لینک یوتیوب → نمایش عنوان، تامبنیل و کیفیت‌های واقعاً موجود
- 🎥 دانلود ویدیو در کیفیت‌های `2160 / 1080 / 720 / 480 / 360` (بهترین ترکیب صدا+تصویر با ادغام ffmpeg)
- 🎵 تبدیل به MP3 با کیفیت دلخواه
- 🚀 **ارسال موشکی (کش):** پس از اولین دانلود، فایل در دیتابیس ذخیره می‌شود و ارسال‌های بعدی فوری است
- 📊 پیام پیشرفت زنده: حجم، سرعت، درصد و نوار لودینگ 🟩⬜
- 🖼️ تامبنیل اختصاصی هر پست + برش عمودی خودکار برای Shorts
- 🧾 کپشن فارسی شامل عنوان، لینک، کیفیت و حجم
- 🔒 حالت تست: فقط ادمین‌ها در حالت توسعه سرویس می‌گیرند
- 🛡️ خطاهای یوتیوب به فارسی روان ترجمه می‌شوند
- ⚡ بدون وب‌هوک — Polling با Telethon (مناسب سرورهای ایران)

## 🏗ی ساختار پروژه

| فایل | وظیفه |
|---|---|
| `bot.py` | هندلرهای پیام/کال‌بک، جریان دانلود و آپلود، نقطهٔ ورود |
| `config.py` | خواندن `.env`، مسیرها و ثابت‌ها |
| `database.py` | مدل‌های SQLAlchemy + توابع دسترسی (async) |
| `youtube_service.py` | استخراج شناسه، متادیتا (yt-dlp)، دانلود/ادغام/تبدیل |
| `progress.py` | متن پیشرفت، نوار اموجی، ویرایش‌دهندهٔ throttle شده |
| `.env.example` | الگوی تنظیمات (کپی به `.env` کنید) |

## 🚀 نصب و اجرا

### ۱) پیش‌نیازها
- Python 3.12
- PostgreSQL 15
- فایل‌های `tools/ffmpeg-*` و `tools/deno/deno.exe` (الزامی برای yt-dlp)

### ۲) مراحل راه‌اندازی

```powershell
# کلون پروژه
git clone https://github.com/<یوزرنیم شما>/myYouTube_Download_Robot.git
cd myYouTube_Download_Robot

# محیط مجازی پایتون
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt

# تنظیمات را وارد کنید
copy .env.example .env
notepad .env
```

### ۳) ساخت ربات تلگرام
1. در `@BotFather` ربات بسازید و توکن را در `BOT_TOKEN` بگذارید
2. در `my.telegram.org` `api_id` و `api_hash` را بگیرید و در `TELEGRAM_API_ID` / `TELETHON_API_HASH` بگذارید
3. یوزر آی‌دی عددی خودتان را در `ADMIN_USER_IDS` قرار دهید

### ۴) اجرا

```powershell
venv\Scripts\python.exe bot.py
# یا
run.bat
```

## 📸 کاور پروژه

عکس کاور را در مسیر `assets/cover.png` قرار دهید
(ابعاد پیشنهادی: **1280×640** برای Social Preview گیت‌هاب و **1600×400** برای بنر README).

## ⚠️ نکات امنیتی

- فایل `.env` شامل توکن ربات، api_hash و رمز دیتابیس است و **هرگز** نباید کامیت شود
  (در `.gitignore` قرار دارد و نباید آن را حذف کنید).
- `bot_session.session`، `downloads/`، `cookies/`، `tools/` و لاگ‌ها نیز کامیت نمی‌شوند.
- اطلاعات حساس را هرگز در کد هاردکد نکنید.

## 📄 لایسنس

MIT — استفاده و تغییر آزاد است.

> ⚖️ فقط برای دانلود محتوایی که مجوزش را دارید استفاده کنید.
