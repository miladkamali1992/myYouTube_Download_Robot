"""منطق مربوط به یوتیوب: استخراج «کد اختصاصی ویدیو»، دریافت اطلاعات و دانلود.

نکته: تمام توابع این ماژول **بلوکینگ** هستند و باید با run_in_executor اجرا شوند.
"""
import glob
import os
import re
from typing import Callable, Dict, List, Optional

import yt_dlp

import config

# --- استخراج کد اختصاصی ویدیو از انواع لینک‌ها ---
VIDEO_ID_RE = re.compile(r'^[0-9A-Za-z_-]{11}$')
URL_RE = re.compile(r'https?://[^\s<>"\']+')

# youtube.com/watch?v=ID ، /shorts/ID ، /embed/ID ، /live/ID ، /v/ID ، /e/ID
_PATTERNS = [
    re.compile(r'youtube\.com/watch\?(?:[^#\s]*&)?v=([0-9A-Za-z_-]{11})'),
    re.compile(r'youtube\.com/(?:shorts|embed|live|v|e)/([0-9A-Za-z_-]{11})'),
    re.compile(r'youtu\.be/([0-9A-Za-z_-]{11})'),
    re.compile(r'(?:^|[?&/])([0-9A-Za-z_-]{11})(?:[&#?/]|$)'),  # آخرین راه‌حل
]


def socks_opener():
    """opener پروکسی SOCKS برای urllib (اگر PROXY_URL تنظیم شده باشد)."""
    if not config.PROXY_URL:
        return None
    try:
        import socks
        import sockshandler
        import urllib.request
        from urllib.parse import urlparse
        u = urlparse(config.PROXY_URL)
        ptype = {'socks5': socks.SOCKS5, 'socks4': socks.SOCKS4,
                 'http': socks.HTTP}.get((u.scheme or '').lower())
        if ptype is None:
            return None
        creds = []
        if u.username:
            creds = [u.username, u.password]
        return urllib.request.build_opener(sockshandler.SocksiPyHandler(
            ptype, u.hostname, int(u.port or 10808), True, *creds))
    except Exception:  # noqa: BLE001
        return None


def looks_like_url(text: str) -> bool:
    return bool(URL_RE.search(text or ''))


def extract_video_id(text: str) -> Optional[str]:
    """کد ۱۱ کاراکتری ویدیو را از هر شکل لینک یوتیوب استخراج می‌کند.

    نمونه‌های پشتیبانی‌شده:
      https://www.youtube.com/watch?v=ID
      https://youtu.be/ID?t=30
      https://m.youtube.com/watch?v=ID&list=...
      https://www.youtube.com/shorts/ID
      https://www.youtube.com/embed/ID
      https://www.youtube.com/live/ID
      youtube.com/watch?v=ID (بدون https)
    برای دامنه‌های شورت ناشناخته، caller باید از fetch_info کمک بگیرد.
    """
    if not text:
        return None
    text = text.strip()
    if VIDEO_ID_RE.match(text):
        return text
    # اول الگوهای دقیق؛ الگوی عمومی آخر است که ممکن است false-positive بدهد
    for pat in _PATTERNS[:3]:
        m = pat.search(text)
        if m:
            return m.group(1)
    if 'youtu' in text:
        m = _PATTERNS[3].search(text)
        if m:
            return m.group(1)
    return None


# تنظیمات مخصوص «گرفتن اطلاعات»: سریع شکست بخورد تا کاربر معطل چند
# تلاش طولانی نشود؛ مسیر دانلود همان تنظیمات پایه را دارد.
_INFO_OPTS = {'socket_timeout': 15, 'retries': 1}


def _base_opts() -> Dict:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 3,
        'nocheckcertificate': False,
        'cachedir': False,
    }
    if config.YOUTUBE_COOKIES:
        opts['cookiefile'] = config.YOUTUBE_COOKIES
    if config.FFMPEG_LOCATION:
        opts['ffmpeg_location'] = config.FFMPEG_LOCATION
    if config.PROXY_URL:
        # دانلود/گرفتن اطلاعات از طریق پروکسی (v2rayN)
        opts['proxy'] = config.PROXY_URL
    if config.DENO_PATH:
        # بدون JS runtime، استخراج یوتیوب ناقص است (هشدار خود yt-dlp)
        opts['js_runtimes'] = {'deno': {'path': config.DENO_PATH}}
    return opts


# زنجیرهٔ کلاینت‌های یوتیوب: اول پیش‌فرض، بعد کلاینت‌های جایگزین.
# بعضی ویدیوها فقط با یک کلاینت خاص در دسترس‌اند.
_CLIENT_FALLBACKS: List[Optional[List[str]]] = [
    None,
    ['android'],
    ['tv', 'ios'],
    ['web_safari', 'mweb'],
]

# خطاهایی که با تغییر کلاینت درست نمی‌شوند (خطای قطعی ویدیو)
_FATAL_HINTS = (
    'removed by the uploader', 'private video', 'members-only',
    'join this channel', 'not available in your country',
    'blocked', 'confirm your age', 'sign in to confirm',
    'video has been removed', 'inappropriate',
)

# نگاشت خطای yt-dlp به پیام فارسی کاربرپسند
_ERROR_MAP = (
    (('removed by the uploader', 'video has been removed'), '❌ این ویدیو توسط سازنده حذف شده است.'),
    (('private video',), '❌ این ویدیو خصوصی است و قابل دانلود نیست.'),
    (('members-only', 'join this channel'), '❌ این ویدیو مخصوص اعضای کانال است.'),
    (("you're not a bot", 'confirm you’re not a bot',
       'sign in to confirm you are not a bot', 'use --cookies'),
     '❌ یوتیوب موقتاً دسترسی ربات را محدود کرده است. لطفاً چند دقیقه بعد دوباره امتحان کن.'),
    (('confirm your age', 'age-restricted'),
     '❌ این ویدیو محدودیت سنی دارد و بدون لاگین قابل دانلود نیست.'),
    (('not available in your country', 'blocked in your country',
      'not available in your region'), '❌ این ویدیو در کشور/منطقهٔ این سرور قابل دسترسی نیست.'),
    (('copyright',), '❌ این ویدیو به دلیل کپی‌رایت مسدود شده است.'),
    (('live event will begin', 'premieres in', 'not currently live'),
     '❌ این ویدیو هنوز شروع نشده است (پخش زنده/پرمیر).'),
    (('no video formats found', 'requested format is not available'),
     '❌ فرمتی برای دانلود این ویدیو پیدا نشد.'),
    (("you're not a bot", 'sign in to confirm you are not a bot',
       'use --cookies'),
     '❌ یوتیوب موقتاً دسترسی ربات را محدود کرده است. لطفاً چند دقیقه بعد دوباره امتحان کن.'),
    (('video unavailable', 'this video is not available', 'unavailable'),
     '❌ این ویدیو در دسترس نیست (حذف/خصوصی/محدودیت منطقه).'),
)


def _web_playability(url: str) -> Dict[str, str]:
    """خواندن وضعیت پخش مستقیم از صفحهٔ یوتیوب (منبع معتبر برای دلیل خطا).

    کلاینت‌های جایگزین yt-dlp گاهی پیام گمراه‌کننده می‌دهند (مثلاً
    «removed by the uploader» برای ویدیوی موجود). صفحهٔ وب دقیق است.
    """
    import json
    import re
    import urllib.request

    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                          'Accept-Language': 'en-US,en;q=0.9'})
        opener = socks_opener()
        if opener is not None:
            resp = opener.open(req, timeout=20)
        else:
            resp = urllib.request.urlopen(req, timeout=20)
        with resp:
            html_text = resp.read().decode('utf-8', 'ignore')
    except Exception:  # noqa: BLE001
        return {}
    m_text = _find_json_object(html_text, '"playabilityStatus"')
    if not m_text:
        return {}
    try:
        data = json.loads(m_text)
    except Exception:  # noqa: BLE001
        return {}
    reason = str(data.get('reason') or '')
    # دلیل دقیق‌تر داخل errorScreen می‌نشیند
    try:
        desc = (data.get('errorScreen', {})
                    .get('playerInterstitialRenderer', {})
                    .get('content', {})
                    .get('interstitialViewModel', {})
                    .get('description', {})
                    .get('content') or '')
    except Exception:  # noqa: BLE001
        desc = ''
    return {'status': str(data.get('status') or ''), 'reason': reason,
            'description': str(desc)}


def _find_json_object(text: str, key: str) -> Optional[str]:
    """پیدا کردن آبجکت JSON بعد از یک کلید با تطبیق آکولادها."""
    i = text.find(key)
    if i < 0:
        return None
    start = text.find('{', i)
    if start < 0:
        return None
    depth = 0
    limit = min(len(text), start + 20000)
    for k in range(start, limit):
        ch = text[k]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:k + 1]
    return None


def friendly_error(exc: Exception, url: Optional[str] = None) -> str:
    """تبدیل خطای خام yt-dlp به پیام فارسی قابل نمایش به کاربر.

    اگر صفحهٔ یوتیوب در دسترس باشد، دلیل واقعی از خود صفحه خوانده می‌شود
    (چون کلاینت‌های fallback ممکن است پیام اشتباه بدهند).
    """
    if url:
        info = _web_playability(url)
        status = (info.get('status') or '').upper()
        reason = info.get('reason') or ''
        detail = info.get('description') or ''
        if status in ('OK', 'LIVE_STREAM_OFFLINE'):
            # ویدیو سالم است؛ مشکل فقط در استخراج بوده
            return ('❌ خطا در دریافت این ویدیو از یوتیوب. '
                    'لطفاً چند دقیقه بعد دوباره امتحان کن.')
        if detail:
            return _map_error_text(detail)
        if reason:
            return _map_error_text(reason)
    return _map_error_text(str(exc))


def _map_error_text(text: str) -> str:
    low = text.lower()
    for keys, message in _ERROR_MAP:
        if any(k in low for k in keys):
            return message
    return '❌ خطا در دریافت این ویدیو. لطفاً لینک دیگری امتحان کن.'


def _extract(url: str, download: bool, extra_opts: Optional[Dict] = None) -> Dict:
    """استخراج با fallback بین کلاینت‌های یوتیوب. (بلوکینگ)"""
    last_error: Optional[Exception] = None
    for clients in _CLIENT_FALLBACKS:
        opts = _base_opts()
        if extra_opts:
            opts.update(extra_opts)
        if clients:
            opts['extractor_args'] = {'youtube': {'player_client': clients}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as e:  # noqa: BLE001
            last_error = e
            low = str(e).lower()
            if any(h in low for h in _FATAL_HINTS):
                raise  # دیگر با کلاینت دیگری درست نمی‌شود
    raise last_error if last_error else RuntimeError('extract failed')


def _format_size(f: Dict) -> Optional[int]:
    return f.get('filesize') or f.get('filesize_approx')


def _approx_size(f: Dict, duration: Optional[float]) -> int:
    """حجم فایل: اول از filesize واقعی، وگرنه از tbr × مدت‌زمان (تخمینی)."""
    size = _format_size(f)
    if size:
        return int(size)
    tbr = f.get('tbr')  # مگابیت بر ثانیه نیست؛ kilobit/s کل فرمت
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * float(duration))
    return 0



def _pick_sizes(info: Dict) -> Dict[str, int]:
    """تخمین حجم هر کیفیتِ **واقعاً موجود** در ویدیو.

    کلیدهای خروجی همان ارتفاع‌های واقعی هستند (مثلاً '1080' یا '240')؛
    برای همین دکمه‌ها همیشه کیفیت واقعی را نشان می‌دهند و هیچ‌وقت
    «بهترین کیفیت» مبهم نمی‌آید.
    """
    formats: List[Dict] = info.get('formats') or []
    sizes: Dict[str, int] = {}
    duration = info.get('duration')

    # فقط ارتفاع‌های واقعی ویدیو (بدون storyboard/تصویرهای mhtml)
    video_heights = sorted({f.get('height') for f in formats
                            if f.get('height')
                            and f.get('vcodec') not in (None, 'none')})
    heights_present = set(video_heights)

    audios = [f for f in formats
              if f.get('acodec') not in (None, 'none')
              and f.get('vcodec') in (None, 'none')]
    best_audio = max(audios, key=lambda f: _approx_size(f, duration)) if audios else None
    audio_bytes = _approx_size(best_audio, duration) if best_audio else 0

    def size_for_height(h: int) -> int:
        cands = [f for f in formats
                 if f.get('height') and f['height'] <= h
                 and f.get('vcodec') not in (None, 'none')]
        if not cands:
            return 0
        v = max(cands, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0))
        total = _approx_size(v, duration)
        # اگر فرمت ویدیو صدا ندارد باید با بهترین صدا ادغام شود → حجم صدا هم اضافه می‌شود
        if v.get('acodec') in (None, 'none'):
            total += audio_bytes
        return total

    # کیفیت‌های استاندارد، فقط اگر ارتفاع دقیقشان در ویدیو باشد
    for q in config.QUALITIES:
        h = int(q)
        if h not in heights_present:
            continue
        total = size_for_height(h)
        if total:
            sizes[q] = total

    # صوت
    if audio_bytes:
        sizes[config.AUDIO_QUALITY] = audio_bytes

    # اگر حداکثر کیفیت ویدیو جزو کیفیت‌های استاندارد نیست (مثل 240p یا 1440p)
    # با همان عنوان واقعی‌اش نمایش داده می‌شود
    if video_heights:
        top = video_heights[-1]
        if str(top) not in sizes:
            total = size_for_height(top)
            if total:
                sizes[str(top)] = total
    return sizes


def fetch_info(url: str) -> Dict:
    """دریافت اطلاعات ویدیو بدون دانلود. (بلوکینگ — در executor اجرا شود)"""
    raw = _extract(url, download=False, extra_opts=_INFO_OPTS)

    formats = raw.get('formats') or []
    widths = [f.get('width') for f in formats
              if f.get('width') and f.get('height')
              and f.get('vcodec') not in (None, 'none')]
    heights = [f.get('height') for f in formats
               if f.get('height') and f.get('vcodec') not in (None, 'none')]
    # عمودی بودن ویدیو (مثل Shorts): پهن‌ترین ارتفاع واقعی باریک‌تر از عرضش است
    is_vertical = False
    if widths and heights:
        top = max(formats, key=lambda f: (f.get('height') or 0))
        if (top.get('width') or 0) < (top.get('height') or 0):
            is_vertical = True
    return {
        'id': raw.get('id'),
        'title': raw.get('title') or 'ویدیوی یوتیوب',
        'channel': raw.get('channel') or raw.get('uploader'),
        'thumbnail': raw.get('thumbnail'),
        'url': raw.get('webpage_url') or url,
        'duration': raw.get('duration'),
        'max_height': max(heights) if heights else None,
        'heights': sorted(set(heights)),
        'is_vertical': is_vertical,
        'sizes': _pick_sizes(raw),
    }


def build_format_selector(quality: str) -> str:
    """سلکتور yt-dlp برای هر کیفیت (ادغام تصویر و صدا با ffmpeg)."""
    if quality == config.AUDIO_QUALITY:
        return 'bestaudio/best'
    if quality == 'best':
        return 'bv*+ba/b'
    h = int(quality)
    # اولویت با فرمت‌های سازگار تلگرام (mp4 + h264/m4a)، در غیر این صورت هر فرمتی
    return (
        f'bv*[height<={h}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/'
        f'bv*[height<={h}][ext=mp4]+ba[ext=m4a]/'
        f'bv*[height<={h}]+ba/'
        f'b[height<={h}]/bv*[height<={h}]/b'
    )


def safe_filename(name: str, max_len: int = 120) -> str:
    """پاک‌سازی عنوان برای استفاده به عنوان نام فایل در ویندوز."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', name or '')
    name = re.sub(r'#[^\s#]+', ' ', name)  # حذف هشتگ‌ها از نام فایل
    name = name.replace('#', ' ')
    name = re.sub(r'\s+', ' ', name).strip(' .')
    return name[:max_len].strip(' .') or 'video'


def output_basename(quality: str, title: str) -> str:
    """نام پایه فایل خروجی = عنوان ویدیو (+ کیفیت در پرانتز برای ویدیوها).

    نمونه: 'My Video (720p)'  |  موزیک: 'My Song'
    """
    base = safe_filename(title)
    if quality == config.AUDIO_QUALITY:
        return base
    return f'{base} ({quality}p)' if quality != 'best' else f'{base} (best)'


def download(url: str, quality: str, title: str, progress_hook: Callable) -> str:
    """دانلود + پردازش نهایی. مسیر فایل نهایی را برمی‌گرداند. (بلوکینگ)"""
    out_dir = config.DOWNLOAD_DIR
    os.makedirs(out_dir, exist_ok=True)
    base = output_basename(quality, title)
    # پاک‌سازی فایل‌های ناقص/قبلی با همین نام
    for old in glob.glob(os.path.join(out_dir, base + '.*')):
        try:
            os.remove(old)
        except OSError:
            pass

    opts = _base_opts()
    opts['format'] = build_format_selector(quality)
    opts['outtmpl'] = os.path.join(out_dir, base + '.%(ext)s')
    opts['progress_hooks'] = [progress_hook]
    opts['postprocessors'] = []
    if quality == config.AUDIO_QUALITY:
        opts['postprocessors'].append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        })
    elif quality != 'best':
        opts['merge_output_format'] = 'mp4'

    info = _extract(url, download=True, extra_opts={
        'format': opts['format'],
        'outtmpl': opts['outtmpl'],
        'progress_hooks': opts['progress_hooks'],
        'postprocessors': opts['postprocessors'],
        **({'merge_output_format': 'mp4'} if opts.get('merge_output_format') else {}),
    })

    # مسیر نهایی (پسوند بعد از پردازش ممکن است عوض شود: m4a → mp3)
    rd = (info.get('requested_downloads') or [{}])[0]
    path = rd.get('filepath')
    if not path or not os.path.exists(path):
        ext = rd.get('ext') or info.get('ext')
        if ext:
            candidate = os.path.join(out_dir, f'{base}.{ext}')
            if os.path.exists(candidate):
                path = candidate
    if path and os.path.exists(path):
        if quality == config.AUDIO_QUALITY and not path.lower().endswith('.mp3'):
            mp3 = os.path.splitext(path)[0] + '.mp3'
            if os.path.exists(mp3):
                return mp3
        return path

    # آخرین راه‌حل: جستجو بر اساس نام پایه
    wanted_ext = ('mp3',) if quality == config.AUDIO_QUALITY else ('mp4', 'mkv', 'webm')
    candidates = [c for c in glob.glob(os.path.join(out_dir, base + '.*'))
                  if not c.endswith(('.part', '.ytdl'))]
    for ext in wanted_ext:
        for c in candidates:
            if c.lower().endswith('.' + ext):
                return c
    if candidates:
        return max(candidates, key=os.path.getsize)
    raise RuntimeError(f'فایل دانلودشده پیدا نشد: {base}')

