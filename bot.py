"""ربات تلگرام دانلود ویدیوی یوتیوب — Telethon با حالت Polling.

نیازی به دامنه/وب‌هوک نیست؛ ربات با long-polling کار می‌کند.

جریان کار:
  1) کاربر لینک یوتیوب می‌فرستد (هر شکلی: watch/shorts/youtu.be/embed/...).
  2) «کد اختصاصی ویدیو» استخراج و در دیتابیس ذخیره می‌شود.
  3) پستی شامل تامبنیل + عنوان + دکمه کیفیت‌ها (2160/1080/720/480/360/Audio) ارسال می‌شود.
  4) با انتخاب کیفیت، همان پست ویرایش شده و حجم/لودینگ/درصد دانلود و سپس آپلود نمایش داده می‌شود.
  5) پس از آپلود، همان پست به ویدیو تبدیل می‌شود و Document کامل Telethon
     در دیتابیس ذخیره می‌گردد تا درخواست‌های بعدی همان کیفیت «موشکی» ارسال شوند
     (دکمه‌ی کیفیت ذخیره‌شده 🚀 می‌گیرد) و فایل محلی از سرور حذف می‌شود.
"""
import asyncio
import glob
import logging
import os
import subprocess
import sys
import urllib.request
from typing import Dict, List, Optional

from telethon import Button, TelegramClient, events, types
from telethon.extensions.binaryreader import BinaryReader

import config
import database as db
import progress as prog
import youtube_service as yt

# ------------------------------------------------------------------ logging --
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('bot')

# ------------------------------------------------------------------- client --
client = TelegramClient(
    config.TELETHON_SESSION, config.API_ID, config.API_HASH,
    flood_sleep_threshold=60,
    proxy=config.proxy_dict(),  # همهٔ ترافیک تلگرام از پروکسی عبور می‌کند
)
semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
job_locks: Dict[str, asyncio.Lock] = {}
# نگهداری شناسهٔ پست اصلی (تامبنیل+دکمه‌ها) برای هر (چت، ویدیو)
# تا بعد از کش شدن یک کیفیت، دکمهٔ آن در پست اصلی 🚀 بگیرد.
post_msgs: Dict[tuple, int] = {}


def get_lock(key: str) -> asyncio.Lock:
    lock = job_locks.get(key)
    if lock is None:
        lock = job_locks[key] = asyncio.Lock()
    return lock


async def safe_answer(event: events.CallbackQuery.Event, text: str,
                      alert: bool = False) -> None:
    """answer فقط یک بار مجاز است؛ خطاها نادیده گرفته می‌شوند."""
    try:
        await event.answer(text, alert=alert)
    except Exception:  # noqa: BLE001
        pass


async def _clear_post_buttons(chat_id: int, msg_id: Optional[int]) -> None:
    """حذف دکمه‌های شیشه‌ای کیفیت‌ها از پست (تصویر و متن دست‌نخورده می‌مانند)."""
    if not msg_id:
        return
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
        if msg is None:
            return
        await client.edit_message(
            chat_id, msg_id, msg.raw_text or '', buttons=[],
            formatting_entities=msg.entities, parse_mode=None)
    except Exception as e:  # noqa: BLE001
        log.debug('clear post buttons failed: %s', e)


# -------------------------------------------------------------------- texts --
WELCOME = """سلام {name} 👋

من ربات دانلود ویدیو از یوتیوب هستم.
کافیه لینک ویدیو رو بفرستی — هر شکلی که باشد:
youtube.com/watch، youtu.be، Shorts، Embed، بدون https و...

همین حالا یک لینک بفرست! 🎬"""

NOT_YOUTUBE = '❌ فقط لینک یوتیوب پشتیبانی می‌شود.'
NO_INFO = '❌ نتوانستم اطلاعات این ویدیو را دریافت کنم. لینک را بررسی کن.'
LINK_EXPIRED = '❌ این ویدیو دیگر در دسترس نیست. لینک جدید بفرست.'
UNDER_DEV = '🤖 ربات در حال توسعه است؛ به‌زودی آماده می‌شود.'


def quality_label(quality: str) -> str:
    if quality == 'audio':
        return 'Audio'
    if quality == 'best':
        return 'بهترین کیفیت'
    return f'{quality}p'


def _truncate(text: str, limit: int = 900) -> str:
    text = (text or '').strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def available_qualities(video: db.Video) -> List[str]:
    """کیفیت‌هایی که برای این ویدیو دکمه دارند، به ترتیب نزولی.

    فقط کیفیت‌های واقعاً موجود (در `_pick_sizes` بر اساس ارتفاع دقیق فرمت‌ها
    ثبت شده‌اند) نمایش داده می‌شوند؛ مثلاً ویدیوی حداکثر 720 هرگز دکمه
    1080/2160 نمی‌گیرد و ویدیوی 240 دکمه `240p` می‌گیرد (نه «بهترین کیفیت»).
    """
    sizes = video.format_sizes or {}
    digits = [q for q in sizes if q.isdigit()]
    if not digits and video.max_height:
        # سازگاری با ردیف‌های قدیمی که با کلید 'best' ذخیره شده بودند:
        # ارتفاع واقعی ویدیو به عنوان کیفیت نمایش داده می‌شود
        digits = [str(video.max_height)]
    digits.sort(key=int, reverse=True)
    return digits


async def update_post_buttons(chat_id: int, video: db.Video) -> None:
    """دکمه‌های پست اصلی کاربر را بعد از کش شدن کیفیت، به‌روزرسانی می‌کند (🚀)."""
    post_id = post_msgs.get((chat_id, video.video_id))
    if not post_id:
        return
    try:
        msg = await client.get_messages(chat_id, ids=post_id)
        if msg is None:
            return
        cached = await db.get_cached_qualities(video.video_id)
        buttons = build_buttons(video.video_id, available_qualities(video), cached)
        await client.edit_message(
            chat_id, msg, msg.raw_text, buttons=buttons,
            formatting_entities=msg.entities, parse_mode=None)
    except Exception as e:  # noqa: BLE001
        log.debug('post buttons update failed: %s', e)


def build_buttons(video_id: str, qualities: List[str], cached: set,
                  audio_available: bool = True) -> list:
    """دکمه‌های شیشه‌ای کیفیت‌ها؛ کیفیت‌های ذخیره‌شده 🚀 می‌گیرند."""
    rows: List[List[Button]] = []
    row: List[Button] = []
    for q in qualities:
        label = quality_label(q) + (' 🚀' if q in cached else '')
        row.append(Button.inline(label, data=f'd:{video_id}:{q}'))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if audio_available or 'audio' in cached:
        audio_label = '🔊 Audio' + (' 🚀' if 'audio' in cached else '')
        rows.append([Button.inline(audio_label, data=f'd:{video_id}:audio')])
    return rows



# ------------------------------------------------------------- link handler --
def _socks_opener():
    """opener پروکسی SOCKS (پیاده‌سازی مشترک در youtube_service)."""
    return yt.socks_opener()


def _fetch_thumb_bytes(url: Optional[str]) -> Optional[bytes]:
    """دانلود تامبنیل و برگرداندن بایت‌های آن (بلوکینگ، داخل executor صدا زده شود)."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        opener = _socks_opener()
        if opener is not None:
            with opener.open(req, timeout=20) as resp:
                return resp.read()
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        log.warning('thumbnail download failed: %s', e)
        return None


def _write_thumb(data: bytes, video_id: str) -> str:
    """بایت‌های تامبنیل به فایل موقت محلی (برای ارسال به تلگرام)."""
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    path = os.path.join(config.DOWNLOAD_DIR, f'_thumb_{video_id}.jpg')
    with open(path, 'wb') as f:
        f.write(data)
    return path


def _purge_temp(video_id: str) -> None:
    """حذف فایل‌های موقتِ یک ویدیو (تامبنیل/فریم/اسنپ/بک‌فیل)."""
    if not video_id:
        return
    for pattern in (f'_thumb_{video_id}*', f'_frame_{video_id}*',
                    f'_snap_{video_id}*', f'_backfill_{video_id}*',
                    f'_cached_thumb_{video_id}*'):
        for f in glob.glob(os.path.join(config.DOWNLOAD_DIR, pattern)):
            try:
                os.remove(f)
            except OSError:
                pass


def _crop_vertical(path: str) -> str:
    """برش تامبنیل افقی به حالت باریک 9:16 برای ویدیوی عمودی (بلوکینگ).

    ریشهٔ مشکل: تامبنیل خودِ یوتیوب 16:9 است؛ تلگرام برای نمایش آن در حباب
    چت، پشتش یک قاب افقیِ تیره/زومی می‌کشد. با بریدن مرکز به 9:16، عکس
    دقیقاً مثل پیش‌نمایش لینک دیده می‌شود و دیگر قابی ندارد.
    خروجی: فایل جدید کنار فایل ورودی (`_v.jpg`)؛ اگر برش ممکن نباشد همان ورودی.
    """
    if not config.VERTICAL_CROP:
        return path
    ffmpeg = (os.path.join(config.FFMPEG_LOCATION, 'ffmpeg.exe')
              if config.FFMPEG_LOCATION else 'ffmpeg')
    out = path.rsplit('.', 1)[0] + '_v.jpg'
    try:
        # عرض مقصد = ارتفاع/16*9 (برش از مرکز، فقط عرض کم می‌شود، کیفیت عمودی حفظ می‌شود)
        subprocess.run(
            [ffmpeg, '-y', '-v', 'error', '-i', path,
             '-vf', 'crop=ih*9/16:ih,scale=540:-2,format=yuvj420p', '-q:v', '3', out],
            check=True, timeout=60)
        if os.path.exists(out) and os.path.getsize(out):
            try:
                os.remove(path)  # فایل تامبنیل خام دیگر لازم نیست
            except OSError:
                pass
            return out
    except Exception as e:  # noqa: BLE001
        log.warning('vertical crop failed: %s', e)
    return path


def _video_attributes(video_path: str, duration: Optional[float] = None
                      ) -> Optional[list]:
    """ابعاد و مدت واقعی فایل (ffprobe) → DocumentAttributeVideo.

    ریشهٔ مشکل «ویدیوی کوچک»: بدون attributes، تلگرام ابعاد ویدیو را نمی‌داند
    و حباب فایل را کوچک/تار نشان می‌دهد. با ابعاد و مدت واقعی، پیش‌نمایش
    دقیقاً مثل ویدیوی معمولی نمایش داده می‌شود.
    """
    ffdir = config.FFMPEG_LOCATION or ''
    ffprobe = os.path.join(ffdir, 'ffprobe.exe') if ffdir else 'ffprobe'
    w = h = 0
    dur = 0
    try:
        dur = int(float(duration or 0))
    except (TypeError, ValueError):
        dur = 0
    try:
        probe = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0',
             video_path],
            capture_output=True, text=True, timeout=30)
        parts = (probe.stdout or '').strip().split(',')
        if len(parts) == 2:
            w, h = int(parts[0] or 0), int(parts[1] or 0)
        if not dur:
            probe2 = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', video_path],
                capture_output=True, text=True, timeout=30)
            try:
                dur = int(float((probe2.stdout or '0').strip()))
            except (TypeError, ValueError):
                dur = 0
    except Exception as e:  # noqa: BLE001
        log.debug('ffprobe attributes failed: %s', e)
        return None
    if w <= 0 or h <= 0:
        return None
    return [types.DocumentAttributeVideo(
        duration=max(dur, 0), w=w, h=h, supports_streaming=True)]


def _video_frame_thumb(video_path: str, video_id: str,
                       video=None) -> Optional[bytes]:
    """تامبنیل فایل ارسالی با نسبت ابعاد واقعی ویدیو (بلوکینگ).

    ریشهٔ مشکل پیش‌نمایش مربع/تار: تامبنیل mqdefault (320×180) با ابعاد واقعی
    ویدیو نمی‌خواند و تلگرام آن را با حاشیهٔ تار نمایش می‌دهد. اینجا ابعاد
    واقعی با ffprobe خوانده و یک فریم با همان نسبت (حداکثر عرض 640 و ارتفاع
    زوج) ساخته می‌شود تا پیش‌نمایش دقیقاً مثل ویدیوی عادی باشد.
    برای ویدیوی عمودی، خروجی باریک 9:16 می‌شود.
    انکدر mjpeg این بیلد ffmpeg فقط yuvj را می‌پذیرد، پس format=yuvj420p.
    """
    ffdir = config.FFMPEG_LOCATION or ''
    ffmpeg = os.path.join(ffdir, 'ffmpeg.exe') if ffdir else 'ffmpeg'
    ffprobe = os.path.join(ffdir, 'ffprobe.exe') if ffdir else 'ffprobe'
    out = os.path.join(config.DOWNLOAD_DIR, f'_frame_{video_id}.jpg')
    try:
        probe = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0',
             video_path],
            capture_output=True, text=True, timeout=30)
        w, h = 0, 0
        parts = (probe.stdout or '').strip().split(',')
        if len(parts) == 2:
            w, h = int(parts[0] or 0), int(parts[1] or 0)
        if w > 0 and h > 0:
            if w >= h:
                vf = 'scale=640:-2,format=yuvj420p'
            else:
                vf = 'scale=-2:960,format=yuvj420p'
        else:
            vf = 'scale=640:-2,format=yuvj420p'
        if video is not None and getattr(video, 'is_vertical', False) and w >= h:
            # ویدیوی عمودی که فایلش افقی ذخیره شده (نادر): برش مرکزی 9:16
            vf = 'crop=ih*9/16:ih,' + vf
        subprocess.run(
            [ffmpeg, '-y', '-v', 'error', '-ss', '2', '-i', video_path,
             '-frames:v', '1', '-vf', vf, '-q:v', '4', out],
            check=True, timeout=90)
        if os.path.exists(out) and 0 < os.path.getsize(out) <= 200_000:
            with open(out, 'rb') as f:
                return f.read()
    except Exception as e:  # noqa: BLE001
        log.debug('video frame thumb failed: %s', e)
    finally:
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
    return None


def _snapshot_frame(video_path: str, video_id: str) -> Optional[str]:
    """گرفتن یک فریم از فایل ویدیو به عنوان تامبنیل ذخیره‌شده (بلوکینگ).

    برای ویدیوهایی که تامبنیل رسمی‌شان دیگر در دسترس نیست (حذف از یوتیوب +
    ردیف قدیمی بدون thumbnail_data): از فایل کش‌شدهٔ بعدی که دانلود/تحویل
    می‌شود یک فریم گرفته می‌شود تا پست‌های بعدی تصویر داشته باشند.
    """
    ffmpeg = (os.path.join(config.FFMPEG_LOCATION, 'ffmpeg.exe')
              if config.FFMPEG_LOCATION else 'ffmpeg')
    out = os.path.join(config.DOWNLOAD_DIR, f'_snap_{video_id}.jpg')
    try:
        subprocess.run(
            [ffmpeg, '-y', '-v', 'error', '-ss', '3', '-i', video_path,
             '-frames:v', '1', '-vf', 'format=yuvj420p', '-q:v', '4', out],
            check=True, timeout=90)
        if os.path.exists(out) and os.path.getsize(out):
            with open(out, 'rb') as f:
                return f.read()
    except Exception as e:  # noqa: BLE001
        log.debug('snapshot frame failed: %s', e)
    finally:
        try:
            if os.path.exists(out):
                os.remove(out)  # فقط بایت‌ها لازم است، فایل موقت پاک می‌شود
        except OSError:
            pass
    return None


async def _maybe_crop_vertical(path: Optional[str],
                               video: Optional[db.Video]) -> Optional[str]:
    """اگر ویدیو عمودی است (مثل Shorts) تامبنیل را نوار باریک می‌کند."""
    if not path or not config.VERTICAL_CROP:
        return path
    if video is not None:
        if not getattr(video, 'is_vertical', False):
            return path
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _crop_vertical, path)


async def _thumb_from_cache(video_id: str) -> Optional[str]:
    """گرفتن تامبنیل از thumb خودِ فایل کش‌شده در تلگرام.

    وقتی ویدیو از یوتیوب حذف شده باشد، آدرس تامبنیل هم ۴۰۴ می‌شود؛
    اما تامبنیل داخل Document ذخیره‌شده در تلگرام موجود است.
    برای ردیف‌های جدید اول پیامِ ذخیره‌شده خوانده می‌شود و بعد بایت‌های قدیمی.
    """
    for doc in await _cached_docs(video_id):
        path = os.path.join(config.DOWNLOAD_DIR, f'_cached_thumb_{video_id}.jpg')
        try:
            data = await client.download_media(
                types.MessageMediaDocument(document=doc), file=bytes, thumb=-1)
        except Exception as e:  # noqa: BLE001
            log.debug('cached thumb download failed: %s', e)
            continue
        if data:
            with open(path, 'wb') as f:
                f.write(data)
            return path
    return None


async def _cached_docs(video_id: str) -> list:
    """Documentهای قابل استفادهٔ یک ویدیو: اول از پیام ذخیره‌شده، بعد بایت‌های قدیمی.

    نتیجه لیستی از آبجکت‌های Telethon Document است؛ ردیف خراب نادیده گرفته
    و حذف می‌شود تا دفعهٔ بعد دوباره امتحان نشود.
    """
    docs = []
    qualities = await db.get_cached_qualities(video_id)
    # کیفیت‌های ویدیویی اول (احتمال داشتن thumb بیشتر)، بعد بقیه
    ordered = sorted(qualities, key=lambda q: (not q.isdigit(), q))
    for quality in ordered:
        row = await db.get_cached_media(video_id, quality)
        if row is None:
            continue
        doc = None
        if row.message_id and row.chat_id:
            try:
                msg = await client.get_messages(row.chat_id, ids=row.message_id)
                if msg is not None:
                    doc = getattr(msg, 'document', None)
                    if doc is None and getattr(msg, 'media', None):
                        doc = getattr(msg.media, 'document', None)
            except Exception as e:  # noqa: BLE001
                log.debug('cached msg read failed (%s/%s): %s',
                          video_id, quality, e)
        if doc is None and row.document_data:
            try:
                doc = BinaryReader(row.document_data).tgread_object()
            except Exception:  # noqa: BLE001
                doc = None
        if doc is None:
            await db.delete_media(video_id, quality)
            continue
        docs.append(doc)
    return docs


async def _thumb_file_for(video: Optional[db.Video], thumb_url: Optional[str],
                          video_id: str) -> Optional[str]:
    """یافتن فایل تامبنیل به ترتیب اولویت: بایت‌های دیتابیس ← یوتیوب ← فایل کش‌شده.

    اگر ویدیو عمودی است (Shorts)، تامبنیل برش 9:16 می‌خورد.
    """
    path = None
    if video is not None and video.thumbnail_data:
        path = _write_thumb(video.thumbnail_data, video_id)
    else:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _fetch_thumb_bytes, thumb_url)
        if data:
            path = _write_thumb(data, video_id)
        else:
            path = await _thumb_from_cache(video_id)
    return await _maybe_crop_vertical(path, video)


async def _publish_post(chat_id: int, placeholder, video_id: str, caption: str,
                        cap_entities, buttons: list,
                        thumb_path: Optional[str]) -> None:
    """ارسال/ویرایش پستِ تامبنیل + دکمه‌ها و به‌خاطر سپردن شناسهٔ آن.

    placeholder می‌تواند None باشد (مسیر سریع): در این صورت پیام جدید ارسال می‌شود.
    فایل تامبنیل پس از ارسال حذف می‌شود.
    """
    post_msg = None
    try:
        if thumb_path:
            if placeholder is not None:
                try:
                    # همان پیام placeholder به پست تامبنیل تبدیل می‌شود
                    post_msg = await client.edit_message(
                        chat_id, placeholder, caption,
                        file=thumb_path, buttons=buttons, parse_mode=None,
                        formatting_entities=cap_entities)
                except Exception as e:  # noqa: BLE001
                    log.warning('edit placeholder to photo failed: %s', e)
                    post_msg = None
            if post_msg is None:
                post_msg = await client.send_file(
                    chat_id, thumb_path, caption=caption,
                    buttons=buttons, parse_mode=None,
                    formatting_entities=cap_entities)
                if placeholder is not None:
                    await client.delete_messages(chat_id, placeholder)
        elif placeholder is not None:
            post_msg = await client.edit_message(
                chat_id, placeholder, caption,
                buttons=buttons, parse_mode=None,
                formatting_entities=cap_entities)
        else:
            post_msg = await client.send_message(
                chat_id, caption, buttons=buttons, parse_mode=None,
                formatting_entities=cap_entities)
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except OSError:
                pass

    # ذخیرهٔ شناسهٔ پست اصلی برای به‌روزرسانی دکمه‌ها (🚀) پس از کش شدن
    if post_msg is not None:
        if len(post_msgs) > 2000:
            post_msgs.clear()
        post_msgs[(chat_id, video_id)] = post_msg.id


def _buttons_for(video: db.Video, cached: set) -> list:
    """دکمه‌های کیفیت از روی متادیتای دیتابیس (کیفیت‌های موجود + 🚀 کش‌شده‌ها)."""
    sizes = video.format_sizes or {}
    return build_buttons(video.video_id, available_qualities(video), cached,
                         audio_available=bool(sizes.get('audio')))


async def _backfill_thumb_from_media(video: db.Video) -> bool:
    """پر کردن thumbnail_data خالی از روی فایل ویدیویی کش‌شده (پس‌زمینه).

    برای ردیف‌های قدیمی که بدون تامبنیل ثبت شده‌اند: فایل ویدیو از تلگرام
    دانلود، یک فریم از آن گرفته و در دیتابیس ذخیره می‌شود تا لینک‌های بعدی
    همین ویدیو سریع و با تصویر ساخته شوند. True یعنی چیزی ذخیره شد.
    """
    fresh = await db.get_video(video.video_id)
    if fresh is None or fresh.thumbnail_data:
        return False
    row = None
    for q in sorted((await db.get_cached_qualities(video.video_id))):
        if q.isdigit() or q == 'best':
            row = await db.get_cached_media(video.video_id, q)
            if row is not None:
                break
    if row is None:
        return False
    try:
        doc = BinaryReader(row.document_data).tgread_object()
        tmp = os.path.join(config.DOWNLOAD_DIR, f'_backfill_{video.video_id}.mp4')
        data = await client.download_media(doc, file=bytes)
        if not data:
            return False
        with open(tmp, 'wb') as f:
            f.write(data)
    except Exception as e:  # noqa: BLE001
        log.debug('backfill download failed: %s', e)
        return False
    try:
        loop = asyncio.get_running_loop()
        snap = await loop.run_in_executor(
            None, _snapshot_frame, tmp, video.video_id)
        if snap:
            await db.save_thumbnail(video.video_id, snap)
            log.info('backfilled thumb for %s (%d bytes)',
                     video.video_id, len(snap))
            return True
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _schedule_backfill(video: db.Video) -> None:
    """اجرای پس‌زمینهٔ _backfill_thumb_from_media بدون معطل کردن کاربر."""
    if getattr(video, 'thumbnail_data', None):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_backfill_thumb_from_media(video))


async def handle_link(event: events.NewMessage.Event, url: str) -> None:
    """لینک رسیده → ساخت پست تامبنیل + دکمه‌ها.

    سه مسیر:
      1) سریع: متادیتای ویدیو (عنوان، کیفیت‌ها، تامبنیل) از قبل در دیتابیس است
         → بدون هیچ تماسی با یوتیوب پست ساخته می‌شود.
      2) عادی: گرفتن اطلاعات زنده از یوتیوب و ذخیرهٔ متادیتا و تامبنیل.
      3) اگر یوتیوب اطلاعات نداد ولی فایلی از کش داشته باشیم، همان کیفیت‌های
         کش‌شده (🚀) نمایش داده می‌شوند.
    """
    await db.upsert_user(event.sender, config.is_admin(event.sender_id))
    chat_id = event.chat_id
    vid = yt.extract_video_id(url)

    # --- 1) مسیر سریع: فقط از دیتابیس ---
    if vid:
        known = await db.get_video(vid)
        if db.has_meta(known):
            cached = await db.get_cached_qualities(vid)
            buttons = _buttons_for(known, cached)
            caption = prog.post_caption(
                _truncate(known.title or ''), known.duration)
            thumb_path = await _maybe_crop_vertical(
                _write_thumb(known.thumbnail_data, vid), known)
            await _publish_post(chat_id, None, vid, caption, None, buttons,
                                thumb_path)
            log.info('instant post from cached meta: %s', vid)
            return

    placeholder = await event.respond('⏳ در حال دریافت اطلاعات ویدیو...')
    loop = asyncio.get_running_loop()
    # تامبنیل حدسی یوتیوب موازی با گرفتن اطلاعات دانلود می‌شود
    # (یک رفت‌وبرگشت کامل سریع‌تر)
    thumb_task = None
    if vid:
        thumb_task = loop.run_in_executor(
            None, _fetch_thumb_bytes,
            f'https://i.ytimg.com/vi/{vid}/maxresdefault.jpg')
    info = None
    error: Optional[Exception] = None
    try:
        info = await loop.run_in_executor(None, yt.fetch_info, url)
        if not info.get('id'):
            raise ValueError('video id not found')
    except Exception as e:  # noqa: BLE001
        error = e
        log.warning('fetch_info failed for %s: %s', url, e)

    if info is None:
        # --- 3) تلاش برای تحویل از کش (ویدیو ممکن است از یوتیوب حذف شده باشد) ---
        cached_video = await db.get_video(vid) if vid else None
        cached_qs = await db.get_cached_qualities(vid) if cached_video else set()
        if cached_video and cached_qs:
            qualities = sorted((q for q in cached_qs if q.isdigit()),
                               key=int, reverse=True)
            buttons = build_buttons(
                cached_video.video_id, qualities, cached_qs,
                audio_available=('audio' in cached_qs))
            caption = prog.post_caption(
                _truncate(cached_video.title or ''), cached_video.duration)
            await _publish_post(
                chat_id, placeholder, cached_video.video_id,
                caption, None, buttons,
                await _thumb_file_for(cached_video, cached_video.thumbnail_url, vid))
            # ردیف قدیمیِ بدون تامبنیل: در پس‌زمینه از فایل کش‌شده پرش می‌کنیم
            # تا دفعهٔ بعد همین لینک از مسیر سریع با تصویر بیاید
            _schedule_backfill(cached_video)
            return
        await client.edit_message(
            chat_id, placeholder, yt.friendly_error(error, url),
            parse_mode=None)
        return

    # --- 2) اطلاعات زنده گرفته شد: تامبنیل ذخیره می‌شود ---
    thumb_bytes = None
    if thumb_task is not None:
        try:
            thumb_bytes = await thumb_task  # نتیجهٔ دانلود موازی
        except Exception:  # noqa: BLE001
            thumb_bytes = None
    real_thumb = info.get('thumbnail')
    if not thumb_bytes or not (vid and real_thumb and f'/vi/{vid}/' in real_thumb):
        thumb_bytes = await loop.run_in_executor(
            None, _fetch_thumb_bytes, real_thumb)
    video = await db.upsert_video(info, thumb_bytes)
    cached = await db.get_cached_qualities(video.video_id)
    buttons = _buttons_for(video, cached)
    # کپشن پست اول: عنوان / زمان / راهنمای انتخاب کیفیت
    caption = prog.post_caption(_truncate(info['title']), video.duration)
    await _publish_post(
        chat_id, placeholder, video.video_id, caption, None, buttons,
        await _maybe_crop_vertical(
            _write_thumb(thumb_bytes, video.video_id), video) if thumb_bytes
        else await _thumb_file_for(video, info.get('thumbnail'), video.video_id))


# ---------------------------------------------------------- cached delivery --
async def deliver_cached(chat_id: int, prev_msg_id: Optional[int], video: db.Video,
                         quality: str, media_row: db.Media) -> bool:
    """ارسال موشکی فایل کش‌شده به صورت پست جدید.

    فایل همیشه با یک پیام جدید (فوروارد یا ارسال Document) تحویل داده می‌شود و
    پس از آن پیام قبلی (`prev_msg_id` = پست تامبنیل/پیشرفت) حذف می‌گردد.
    در صورت شکست (پیام پاک شده / file_reference منقضی) ردیف کش حذف و False
    برمی‌گردد تا مسیر دانلود عادی اجرا شود.
    """
    caption, cap_entities = prog.final_caption(
        _truncate(video.title or ''),
        quality_label(quality),
        prog.fmt_size(media_row.size_bytes),
        video.url,
    )
    try:
        msg = None
        if media_row.message_id and media_row.chat_id:
            try:
                msg = await client.get_messages(
                    media_row.chat_id, ids=media_row.message_id)
            except Exception as e:  # noqa: BLE001
                log.debug('cached msg missing (%s/%s): %s',
                          video.video_id, quality, e)
                msg = None
        if msg is not None and getattr(msg, 'media', None):
            # همان فایل با همان file_id دوباره ارسال می‌شود (بدون فوروارد)
            doc = (getattr(msg, 'document', None)
                   or getattr(getattr(msg, 'media', None), 'document', None))
            if doc is None:
                raise ValueError('no document in cached msg')
            await client.send_file(
                chat_id, doc, caption=caption, parse_mode=None,
                formatting_entities=cap_entities)
        elif media_row.document_data:
            doc = BinaryReader(media_row.document_data).tgread_object()
            await client.send_file(
                chat_id, doc, caption=caption, parse_mode=None,
                formatting_entities=cap_entities, supports_streaming=True)
        else:
            raise ValueError('empty cache row')
        # پست/پیام پیشرفت قبلی پس از ارسال موفق فایل حذف می‌شود
        if prev_msg_id is not None:
            try:
                await client.delete_messages(chat_id, prev_msg_id)
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception as e:  # noqa: BLE001
        log.warning('cached send failed (%s/%s): %s', video.video_id, quality, e)
        await db.delete_media(video.video_id, quality)
        return False



# ------------------------------------------------------------- download job --
def make_download_hook(editor: prog.ProgressEditor, title: str, size_str: str):
    """هک پیشرفت yt-dlp.

    نکته: yt-dlp هر استریم (ویدیو، سپس صدا) را جدا دانلود می‌کند و
    برای هرکدام «downloading/finished» می‌فرستد؛ پس درصد یکنوا نگه
    داشته می‌شود و وضعیت «انجام شد» فقط بعد از ادغام (در run_job) می‌آید.
    """
    state = {'pct': 0.0}

    def hook(d: dict) -> None:
        status = d.get('status')
        if status == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done = d.get('downloaded_bytes') or 0
            pct = (done * 100.0 / total) if total else 0.0
            state['pct'] = max(state['pct'], pct)  # هرگز عقب نمی‌رود
            editor.track(title, size_str, prog.PHASE_DOWNLOAD, state['pct'])
        elif status == 'finished':
            state['pct'] = 100.0
            editor.track(title, size_str, prog.PHASE_DOWNLOAD, 100.0)
    return hook


async def run_job(chat_id: int, msg_id: int, user_id: int,
                  video: db.Video, quality: str) -> None:
    """دانلود → پردازش → آپلود → ذخیره ارجاع پیام در دیتابیس → حذف فایل محلی."""
    title = _truncate(video.title or 'ویدیوی یوتیوب')
    sizes = video.format_sizes or {}
    # حجم تخمینی (از متادیتای یوتیوب) فقط برای پیام پیشرفت استفاده می‌شود؛
    # حجم واقعی بعد از دانلود محاسبه و در کپشن نهایی گذاشته می‌شود.
    est_size_str = prog.fmt_size(sizes.get(quality))
    editor = prog.ProgressEditor(client, chat_id, msg_id)
    loop = asyncio.get_running_loop()
    video_url = video.url or f'https://www.youtube.com/watch?v={video.video_id}'

    editor.track(title, est_size_str, prog.PHASE_DOWNLOAD, 0)

    path = None
    try:
        hook = make_download_hook(editor, title, est_size_str)
        try:
            path = await loop.run_in_executor(
                None, yt.download, video_url, quality,
                video.title or video.video_id, hook)
        except Exception as e:  # noqa: BLE001
            log.exception('download failed %s/%s', video.video_id, quality)
            await editor.finish(yt.friendly_error(e, video_url))
            await db.add_log(user_id, video.video_id, quality, 'failed')
            return

        file_size = os.path.getsize(path)
        if file_size > config.MAX_UPLOAD_BYTES:
            await editor.finish(
                f'❌ حجم فایل ({prog.fmt_size(file_size)}) از سقف ارسال تلگرام '
                f'({prog.fmt_size(config.MAX_UPLOAD_BYTES)}) بیشتر است.')
            await db.add_log(user_id, video.video_id, quality, 'failed')
            return

        # حجم واقعی فایل نهایی (نه تخمین یوتیوب) — همین در کپشن نمایش داده می‌شود
        real_size_str = prog.fmt_size(file_size)

        snapshot = None
        try:
            snapshot = await loop.run_in_executor(
                None, _snapshot_frame, path, video.video_id)
        except Exception:  # noqa: BLE001
            snapshot = None
        if snapshot:
            await db.save_thumbnail(video.video_id, snapshot)

        editor.track(title, real_size_str, prog.PHASE_UPLOAD, 0,
                     downloaded=True)

        def up_cb(current: int, total: int) -> None:
            pct = (current * 100.0 / total) if total else 0.0
            editor.track(title, real_size_str, prog.PHASE_UPLOAD, pct,
                         downloaded=True)

        caption, cap_entities = prog.final_caption(
            title, quality_label(quality), real_size_str, video.url)

        # تامبنیل ویدیو با همان نسبت ابعاد فایل (افقی 16:9 / عمودی 9:16):
        # از خود فایل نهایی یک فریم افقی گرفته می‌شود تا پیش‌نمایش تلگرام
        # دقیقاً مثل ویدیوی عادی (بدون حاشیهٔ تار و مربع کوچک) نمایش داده شود.
        thumb_path = None
        try:
            frame = await loop.run_in_executor(
                None, _video_frame_thumb, path, video.video_id, video)
        except Exception:  # noqa: BLE001
            frame = None
        if frame:
            thumb_path = _write_thumb(frame, f'{video.video_id}_file')
            await db.save_thumbnail(video.video_id, frame)

        # ابعاد و مدت واقعی فایل → تلگرام حباب ویدیو را کامل نشان می‌دهد
        attrs = None
        try:
            attrs = await loop.run_in_executor(
                None, _video_attributes, path, video.duration)
        except Exception:  # noqa: BLE001
            attrs = None

        if thumb_path and not os.path.exists(thumb_path):
            thumb_path = None

        new_msg = None
        try:
            input_file = await client.upload_file(
                path, file_size=file_size,
                file_name=os.path.basename(path), progress_callback=up_cb)
            # فایل به صورت پست جدید ارسال می‌شود (نه ویرایش پست پیشرفت)
            new_msg = await client.send_file(
                chat_id, input_file, caption=caption, thumb=thumb_path,
                attributes=attrs, supports_streaming=True, parse_mode=None,
                formatting_entities=cap_entities)
        except Exception as e:  # noqa: BLE001
            log.exception('send failed %s/%s: %s', video.video_id, quality, e)
            await editor.finish('❌ ارسال فایل به تلگرام ناموفق بود.')
            await db.add_log(user_id, video.video_id, quality, 'failed')
            return
        finally:
            if thumb_path and os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                except OSError:
                    pass

        # پست پیشرفت (همان پست تامبنیل) پس از ارسال موفق فایل حذف می‌شود
        if new_msg is not None:
            try:
                await client.delete_messages(chat_id, msg_id)
            except Exception as e:  # noqa: BLE001
                log.debug('delete progress post failed: %s', e)
            post_msgs.pop((chat_id, video.video_id), None)

        # ذخیرهٔ ارجاع به پیام فایل برای تحویل موشکی دفعات بعد (سبک و همیشه معتبر)
        if new_msg is not None:
            doc = getattr(new_msg, 'document', None)
            await db.save_media(
                video.video_id, quality, chat_id, new_msg.id,
                doc.size if doc else file_size,
                doc.mime_type if doc else None)
            if doc is not None:
                log.info('cached %s/%s msg=%s (%s bytes)',
                         video.video_id, quality, new_msg.id, doc.size)
            # دکمهٔ همین کیفیت در پست اصلی کاربر 🚀 می‌گیرد
            await update_post_buttons(chat_id, video)
        else:
            log.warning('no message returned for %s/%s', video.video_id, quality)
        await db.add_log(user_id, video.video_id, quality, 'done')
    finally:
        editor.stop()  # توقف انیمیشن نقطه‌ها
        if path and os.path.exists(path):
            try:
                os.remove(path)  # فایل محلی حذف می‌شود؛ دیگر به آن نیازی نیست
            except OSError:
                pass
        _purge_temp(video.video_id)  # تامبنیل/فریم‌های موقت



# --------------------------------------------------------------- callbacks ---
@client.on(events.CallbackQuery)
async def on_callback(event: events.CallbackQuery.Event) -> None:
    # حالت تست: فقط ادمین‌ها
    if config.TEST_MODE and not config.is_admin(event.sender_id):
        await safe_answer(event, UNDER_DEV)
        return
    data = (event.data or b'').decode('utf-8', 'ignore')
    if not data.startswith('d:'):
        return
    parts = data.split(':')
    if len(parts) != 3:
        await safe_answer(event, '❌ دکمه نامعتبر است.', alert=True)
        return
    _, video_id, quality = parts

    await db.upsert_user(event.sender, config.is_admin(event.sender_id))
    video = await db.get_video(video_id)
    if video is None:
        # پست اصلی را دست نمی‌زنیم؛ فقط یک هشدار به کاربر نشان می‌دهیم
        await safe_answer(event, '❌ منقضی شده؛ لطفاً لینک را دوباره بفرست.', alert=True)
        await event.respond(LINK_EXPIRED)
        return

    chat_id = event.chat_id
    title = _truncate(video.title or 'ویدیوی یوتیوب')
    sizes = video.format_sizes or {}
    size_str = prog.fmt_size(sizes.get(quality))

    # پست تامبنیل همان پست پیشرفت می‌شود: اول دکمه‌های شیشه‌ای حذف می‌شوند
    post_id = post_msgs.get((chat_id, video_id)) or event.message_id
    if post_id is None:
        prog_msg = await event.respond(
            prog.progress_text(title, size_str, prog.PHASE_DOWNLOAD, 0))
        post_id = prog_msg.id
    else:
        await _clear_post_buttons(chat_id, post_id)

    # کلیک تکراری/دیرکرد روی همان پست: اگر کاری در جریان است نادیده گرفته
    # می‌شود؛ وگرنه دو کار همزمان روی یک پیام رندر می‌کنند و متن/حجم می‌پرد.
    if prog.active_editor(chat_id, post_id) is not None:
        await safe_answer(event, '⏳ در حال پردازش...')
        return

    # --- مسیر موشکی: فایل کش‌شده به صورت پست جدید؛ پست قبلی حذف می‌شود ---
    cached_row = await db.get_cached_media(video_id, quality)
    if cached_row is not None:
        await safe_answer(event, 'ارسال شد')
        if await deliver_cached(chat_id, post_id, video, quality, cached_row):
            await db.add_log(event.sender_id, video_id, quality, 'cached')
            post_msgs.pop((chat_id, video_id), None)
            return

    # --- مسیر عادی: پیشرفت در همان پست (بدون پیام جدید) ---
    lock = get_lock(f'{video_id}:{quality}')
    if lock.locked():
        await safe_answer(event, '⏳ در صف پردازش...')
        start_phase = '⏳ در صف پردازش؛ چند لحظه دیگر شروع می‌شود...'
    else:
        await safe_answer(event, '⬇️ شروع دانلود...')
        start_phase = prog.PHASE_DOWNLOAD
    editor = prog.ProgressEditor(client, chat_id, post_id)
    editor.track(title, size_str, start_phase, 0)

    async with lock:
        # شاید همین الان کاربر دیگری همین کیفیت را تمام کرده باشد
        cached_row = await db.get_cached_media(video_id, quality)
        if cached_row is not None:
            if await deliver_cached(chat_id, post_id, video, quality, cached_row):
                await db.add_log(event.sender_id, video_id, quality, 'cached')
                post_msgs.pop((chat_id, video_id), None)
                return
        async with semaphore:
            await run_job(chat_id, post_id, event.sender_id, video, quality)


# ------------------------------------------------------------ message handler --
@client.on(events.NewMessage(incoming=True))
async def on_message(event: events.NewMessage.Event) -> None:
    sender = event.sender
    if sender is not None and getattr(sender, 'bot', False):
        return  # پیام ربات‌های دیگر نادیده گرفته می‌شود

    text = (event.raw_text or '').strip()
    if not text:
        return

    # حالت تست: فقط ادمین‌ها سرویس می‌گیرند
    if config.TEST_MODE and not config.is_admin(event.sender_id):
        await event.respond(UNDER_DEV)
        return

    if text.startswith('/start'):
        name = (event.sender.first_name if event.sender else None) or 'دوست'
        await db.upsert_user(event.sender, config.is_admin(event.sender_id))
        await event.respond(WELCOME.format(name=name))
        return

    # دستور گزارش آمار اختصاصی ادمین‌ها (/amar و /stats)
    if text in ('/amar', '/stats') or text.startswith(('/amar ', '/stats ')):
        if not config.is_admin(event.sender_id):
            return
        s = await db.get_stats()
        await event.respond(
            '📊 **آمار ربات**\n\n'
            f'👥 تعداد کل کاربران: {s["total_users"]:,}\n'
            f'📅 کاربران فعال امروز: {s["today_users"]:,}\n'
            f'🔗 تعداد کل لینک‌های دانلودشده: {s["downloaded_links"]:,}\n'
            f'🎬 ویدیوهای موجود در دیتابیس: {s["cached_videos"]:,}\n'
            f'💾 کل فایل‌های کش‌شده: {s["total_files"]:,}')
        return

    url_match = yt.URL_RE.search(text)
    if url_match:
        url = url_match.group(0).rstrip(').,»')
        if 'youtu' in url:
            await handle_link(event, url)
        else:
            await event.respond(NOT_YOUTUBE)
        return

    # شاید کاربر فقط کد ویدیو را فرستاده باشد
    vid = yt.extract_video_id(text)
    if vid:
        await handle_link(event, f'https://www.youtube.com/watch?v={vid}')


# -------------------------------------------------------------------- main ---
async def main() -> None:
    config.validate()
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    # پاک‌سازی فایل‌های ناقص باقی‌مانده از اجرای قبل
    for name in os.listdir(config.DOWNLOAD_DIR):
        if name.startswith('_') or name.endswith(('.part', '.ytdl')):
            try:
                os.remove(os.path.join(config.DOWNLOAD_DIR, name))
            except OSError:
                pass

    await db.init_db()
    log.info('database ready: %s', config.DATABASE_URL.split('@')[-1])

    await client.start(bot_token=config.BOT_TOKEN)
    me = await client.get_me()
    log.info('bot started as @%s (id=%s)', me.username, me.id)
    log.info('ffmpeg: %s', config.FFMPEG_LOCATION or 'NOT FOUND')
    log.info('deno (js runtime): %s', config.DENO_PATH or 'NOT FOUND')
    log.info('proxy: %s', config.PROXY_URL or 'disabled')
    log.info('test mode: %s | admins: %s', config.TEST_MODE, sorted(config.ADMIN_IDS))

    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('stopped by user')
