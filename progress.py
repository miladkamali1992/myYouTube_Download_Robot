"""ابزارهای نمایش پیشرفت: فرمت حجم/سرعت، نوار لودینگ و ویرایشگر throttle شده."""
import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from telethon.errors import MessageNotModifiedError
from telethon.tl.types import (MessageEntityBold, MessageEntityCode,
                               TypeMessageEntity)

import config

log = logging.getLogger(__name__)

PHASE_DOWNLOAD = '⬇️ دانلود از یوتیوب...'
PHASE_PROCESS = '⚙️ پردازش نهایی (ادغام/تبدیل)...'
PHASE_UPLOAD = '⬆️ آپلود به تلگرام...'

# متن وضعیت‌های کپشن پیشرفت
# «در حال انجام» + نقطه‌های چرخشی (هر ثانیه یک نقطه اضافه/ریست می‌شود)
STATUS_RUNNING = 'در حال انجام'
STATUS_DONE = 'انجام شد'
DOT_INTERVAL = 1.0
_BOLD_RE = re.compile(r'(در حال انجام\.*|انجام شد)')

BAR_LEN = 10


def fmt_size(n: Optional[int]) -> str:
    """تبدیل بایت به شکل خوانا: 12.3 مگابایت"""
    if not n:
        return 'نامشخص'
    n = float(n)
    if n < 1024:
        return f'{int(n)} بایت'
    if n < 1024 ** 2:
        return f'{n / 1024:.1f} کیلوبایت'
    if n < 1024 ** 3:
        return f'{n / 1024 ** 2:.1f} مگابایت'
    return f'{n / 1024 ** 3:.2f} گیگابایت'


def fmt_speed(bps: Optional[float]) -> str:
    """تبدیل بایت بر ثانیه به شکل خوانا: 12.34 MB/s"""
    if not bps or bps <= 0:
        return '—'
    if bps < 1024:
        return f'{bps:.0f} B/s'
    if bps < 1024 ** 2:
        return f'{bps / 1024:.1f} KB/s'
    return f'{bps / 1024 ** 2:.2f} MB/s'


class SpeedMeter:
    """محاسبه سرعت لحظه‌ای از فراخوانی‌های پیشرفت (بایت/ثانیه).

    برای آپلود تلگرام که فقط (current, total) می‌دهد استفاده می‌شود.
    """

    def __init__(self) -> None:
        self._last_t = time.monotonic()
        self._last_b = 0
        self.speed = 0.0

    def update(self, done: int) -> float:
        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 0.5:
            self.speed = max(0.0, (done - self._last_b) / dt)
            self._last_t = now
            self._last_b = done
        return self.speed


def progress_bar(percent: float) -> str:
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * BAR_LEN)
    return '🟩' * filled + '⬜' * (BAR_LEN - filled)


def progress_text(title: str, size_str: str, phase: str, percent: float,
                  downloaded: bool = False, dots: int = 3) -> str:
    """متن پیام پیشرفت (بدون سرعت).

    دو خط وضعیت از همان ابتدا وجود دارند و «در حال انجام» با نقطه‌های
    چرخشی (dots) حس لودینگ می‌دهد.
    """
    running = STATUS_RUNNING + '.' * max(0, int(dots))
    lines = [
        title,
        '',
        f'📦 حجم: {size_str}',
        f'⬇️ دانلود از یوتیوب: {STATUS_DONE if downloaded else running}',
        (f'⬆️ آپلود: {running}' if downloaded else '⬆️ آپلود:'),
    ]
    if phase and phase not in (PHASE_DOWNLOAD, PHASE_UPLOAD):
        lines.append(phase)
    lines.append(f'{progress_bar(percent)} {int(percent)}%')
    return '\n'.join(lines)


def progress_entities(text: str):
    """بولد کردن وضعیت‌ها («در حال انجام...» با نقطه‌ها / «انجام شد»)."""
    spans = [(m.start(), m.group(0)) for m in _BOLD_RE.finditer(text)]
    if not spans:
        return None
    return [MessageEntityBold(offset=_u16_len(text[:i]), length=_u16_len(m))
            for i, m in spans]


def progress_payload(title: str, size_str: str, phase: str, percent: float,
                     downloaded: bool = False, dots: int = 3):
    """(متن، entities) آمادهٔ ارسال پیشرفت."""
    text = progress_text(title, size_str, phase, percent, downloaded, dots)
    return text, progress_entities(text)


def _u16_len(s: str) -> int:
    """طول رشته بر حسب واحد UTF-16 (واحد شمارش offset در تلگرام)."""
    return len(s.encode('utf-16-le')) // 2


def build_caption(title: str, url: Optional[str] = None,
                  extra_lines: Optional[List[str]] = None
                  ) -> Tuple[str, Optional[List[TypeMessageEntity]]]:
    """کپشن: هر آیتم در یک خط؛ لینک ویدیو زیر عنوان به صورت مونواسپیس.

    خروجی: (متن، entities) — باید با formatting_entities ارسال شود.
    """
    text = title
    entities: List[TypeMessageEntity] = []
    if url:
        text += f'\n{url}'
        offset = _u16_len(title) + 1  # +1 برای کاراکتر newline
        entities.append(MessageEntityCode(offset=offset, length=_u16_len(url)))
    if extra_lines:
        text += '\n\n' + '\n'.join(extra_lines)
    return text, (entities or None)


def final_caption(title: str, quality_label: str, size_str: str,
                  url: Optional[str]) -> Tuple[str, Optional[List]]:
    """کپشن نهایی بعد از ارسال فایل: عنوان / لینک / کیفیت / حجم / آی‌دی ربات."""
    return build_caption(
        title, url,
        [f'✅ کیفیت: {quality_label}', f'📦 حجم: {size_str}', f'@{config.BOT_USERNAME}'],
    )


def fmt_duration(seconds: Optional[float]) -> str:
    """مدت زمان به شکل خوانا: 3:45 یا 1:02:30."""
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return 'نامشخص'
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def post_caption(title: str, duration: Optional[float]) -> str:
    """کپشن پست اول: عنوان / زمان / راهنمای انتخاب کیفیت."""
    return (f'{title}\n'
            f'⏱ زمان: {fmt_duration(duration)}\n'
            f'کیفیت مورد نظر خود را انتخاب کنید 👇')



# مالکیت پیام پیشرفت: هر پیام فقط یک رندرکنندهٔ فعال دارد تا دو کار
# همزمان (کلیک تکراری) روی یک پست با هم نجنگند.
_ACTIVE: Dict[tuple, 'ProgressEditor'] = {}


def active_editor(chat_id: int, message_id: int) -> Optional['ProgressEditor']:
    """رندرکنندهٔ فعالِ یک پیام (یا None)."""
    return _ACTIVE.get((chat_id, message_id))


class ProgressEditor:
    """ویرایش یک پیام با حداقل فاصله‌ی زمانی (جلوگیری از FloodWait).

    - update(): از event loop (async) صدا زده می‌شود.
    - submit(): از thread دانلود/آپلود (sync) صدا می‌شود؛ امن است.
    """

    def __init__(self, client, chat_id: int, message_id: int):
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = client.loop
        self._last_sent = 0.0
        self._last_text: Optional[str] = None
        self._pending = None
        self._busy = False
        # انیمیشن نقطه‌های «در حال انجام...»
        self._dots = 3
        self._state = None
        self._ticker = None

    def track(self, title: str, size_str: str, phase: str, percent: float,
              downloaded: bool = False) -> None:
        """ثبت وضعیت پیشرفت؛ متن فقط در حلقهٔ انیمیشن رسم می‌شود.

        رسم تک‌منبعی است تا هیچ متن قدیمی/ناهمخوانی روی پیام نیفتد
        (علت پرش‌های قبلی: رندرهای موازی با متن‌های کهنه).
        """
        self._state = (title, size_str, phase, percent, downloaded)
        self._ensure_ticker()

    def _render(self):
        title, size_str, phase, percent, downloaded = self._state
        return progress_payload(title, size_str, phase, percent,
                                downloaded, self._dots)

    def _claim(self) -> None:
        """مالکیت این پیام را می‌گیرد و رندرکنندهٔ قبلی را می‌خواباند."""
        key = (self.chat_id, self.message_id)
        prev = _ACTIVE.get(key)
        if prev is not None and prev is not self:
            prev.stop()
        _ACTIVE[key] = self

    def _release(self) -> None:
        key = (self.chat_id, self.message_id)
        if _ACTIVE.get(key) is self:
            _ACTIVE.pop(key, None)

    def _ensure_ticker(self) -> None:
        """راه‌اندازی انیمیشن — از هر thread‌ای قابل صدا زدن است."""
        if self._ticker is not None and not self._ticker.done():
            return
        try:
            self._ticker = asyncio.run_coroutine_threadsafe(
                self._animate(), self.loop)
        except RuntimeError:
            self._ticker = None

    async def _animate(self) -> None:
        """رسم فوری، سپس هر ثانیه یک نقطه: «...» → «» → «.» → «..» → «...»"""
        self._claim()
        try:
            while self._state is not None:
                text, ents = self._render()
                await self.update(text, entities=ents, force=True)
                await asyncio.sleep(DOT_INTERVAL)
                if self._state is None:
                    break
                self._dots = (self._dots + 1) % 4
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug('progress animation stopped: %s', e)
        finally:
            self._release()

    def stop(self) -> None:
        """توقف انیمیشن (پایان دانلود/آپلود یا حذف پست)."""
        self._state = None
        self._release()
        task, self._ticker = self._ticker, None
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass

    async def update(self, text: str, force: bool = False,
                     entities: Optional[List] = None) -> None:
        now = time.monotonic()
        if text == self._last_text:
            return
        if not force and now - self._last_sent < config.PROGRESS_EDIT_INTERVAL:
            return
        if self._busy:
            return
        self._busy = True
        try:
            await self.client.edit_message(
                self.chat_id, self.message_id, text, parse_mode=None,
                formatting_entities=entities)
            self._last_sent = time.monotonic()
            self._last_text = text
        except MessageNotModifiedError:
            pass
        except Exception as e:  # noqa: BLE001 - خطای ویرایش نباید job را بکشد
            log.warning('edit_message failed: %s', e)
        finally:
            self._busy = False

    def submit(self, text: str, entities: Optional[List] = None) -> None:
        """فراخوانی امن از thread های دانلود/آپلود."""
        try:
            asyncio.run_coroutine_threadsafe(
                self.update(text, entities=entities), self.loop)
        except RuntimeError:
            pass

    async def finish(self, text: str) -> None:
        self.stop()
        await self.update(text, force=True)
