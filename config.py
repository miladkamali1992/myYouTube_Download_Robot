"""پیکربندی مرکزی ربات. تمام مقادیر حساس از فایل .env خوانده می‌شوند."""
import glob
import os
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

# --- تلگرام ---
API_ID = int(os.getenv('TELEGRAM_API_ID', '0'))
API_HASH = os.getenv('TELETHON_API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'myYouTube_Download_Robot')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '0'))
# لیست تمام یوزر آی‌دی‌های ادمین (با کاما یا فاصله جدا می‌شوند)
# هم ADMIN_USER_ID و هم ADMIN_USER_IDS پشتیبانی می‌شوند
_admin_raw = os.getenv('ADMIN_USER_IDS', os.getenv('ADMIN_USER_ID', ''))
ADMIN_IDS = set()
for _part in _admin_raw.replace(';', ',').split(','):
    _part = _part.strip()
    if _part.isdigit():
        ADMIN_IDS.add(int(_part))
if ADMIN_USER_ID:
    ADMIN_IDS.add(ADMIN_USER_ID)


def is_admin(user_id: int) -> bool:
    """آیا یوزر آی‌دی جزو ادمین‌های ربات است؟"""
    return bool(user_id and user_id in ADMIN_IDS)

# --- دیتابیس ---
DATABASE_URL = os.getenv('DATABASE_URL', '')
# تایم‌زون اتصال دیتابیس (سرور Postgres پیش‌فرض روی America/Los_Angeles بود)
DB_TIMEZONE = os.getenv('DB_TIMEZONE', 'Asia/Tehran')

# --- مسیرها ---
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
TELETHON_SESSION = os.path.join(BASE_DIR, 'bot_session')
LOG_FILE = os.path.join(BASE_DIR, 'bot.log')

_ffmpeg_dirs = glob.glob(os.path.join(BASE_DIR, 'tools', 'ffmpeg-*', 'bin'))
FFMPEG_LOCATION = _ffmpeg_dirs[0] if _ffmpeg_dirs else None

# کوکی یوتیوب (فایل Netscape cookies.txt) برای عبور از «Sign in to confirm
# you're not a bot». مسیر را در .env با YOUTUBE_COOKIES بدهید (پیش‌فرض: cookies/youtube.txt).
YOUTUBE_COOKIES = os.getenv('YOUTUBE_COOKIES', '').strip() or os.path.join(
    BASE_DIR, 'cookies', 'youtube.txt')
if not os.path.exists(YOUTUBE_COOKIES):
    YOUTUBE_COOKIES = None

# JS runtime برای yt-dlp (deno) — بدون آن استخراج یوتیوب ناقص/ناسازگار است
_deno_exe = os.path.join(BASE_DIR, 'tools', 'deno', 'deno.exe')
DENO_PATH = os.getenv('DENO_PATH') or (_deno_exe if os.path.exists(_deno_exe) else None)

# --- پروکسی (v2rayN در حالت پروکسی سیستمی، بدون TUN) ---
# نمونه در .env:  PROXY_URL=socks5://127.0.0.1:10808
# خالی بماند = بدون پروکسی (اتصال مستقیم).
PROXY_URL = os.getenv('PROXY_URL', '').strip()
if PROXY_URL and '://' not in PROXY_URL:
    PROXY_URL = 'socks5://' + PROXY_URL


def proxy_dict() -> Optional[dict]:
    """پروکسی به شکل مورد انتظار Telethon (python-socks)."""
    if not PROXY_URL:
        return None
    from urllib.parse import urlparse
    u = urlparse(PROXY_URL)
    return {
        'proxy_type': (u.scheme or 'socks5').lower(),
        'addr': u.hostname or '127.0.0.1',
        'port': int(u.port or 10808),
        'rdns': True,
        'username': u.username,
        'password': u.password,
    }


# --- رفتار ربات ---
# حالت تست: فقط ادمین سرویس می‌گیرد؛ بقیه پیام «در حال توسعه» می‌گیرند.
# برای انتشار عمومی مقدار را در .env برابر 0 کنید.
TEST_MODE = os.getenv('TEST_MODE', '1').strip().lower() in ('1', 'true', 'yes')

# کیفیت‌های ویدیو به ترتیب نمایش در دکمه‌ها
QUALITIES = ['2160', '1080', '720', '480', '360']
AUDIO_QUALITY = 'audio'

# حداکثر دانلود/آپلود همزمان
MAX_CONCURRENT_JOBS = 2
# حداقل فاصله بین ویرایش‌های پیام پیشرفت (ثانیه)
PROGRESS_EDIT_INTERVAL = 2.0
# حداکثر حجم قابل آپلود برای ربات‌ها در تلگرام (~2 گیگابایت)
MAX_UPLOAD_BYTES = 1_950_000_000

# برش عمودی تامبنیل برای ویدیوهای عمودی (Shorts) — قاب تیرهٔ تلگرام حذف می‌شود
VERTICAL_CROP = True


def validate() -> None:
    """قبل از اجرای ربات، مقادیر لازم را بررسی می‌کند."""
    missing = []
    if not API_ID:
        missing.append('TELEGRAM_API_ID')
    if not API_HASH:
        missing.append('TELETHON_API_HASH')
    if not BOT_TOKEN:
        missing.append('BOT_TOKEN')
    if not DATABASE_URL:
        missing.append('DATABASE_URL')
    if missing:
        raise SystemExit(f'Missing config values: {", ".join(missing)}')
