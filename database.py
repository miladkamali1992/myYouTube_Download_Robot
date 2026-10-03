"""لایه دیتابیس (PostgreSQL + SQLAlchemy async) و مدل‌های ORM.

جدول‌ها:
  - users: کاربران ربات
  - videos: ویدیوهای یوتیوب (با شناسه یکتای ۱۱ کاراکتری)
  - media: فایل‌های آپلودشده در تلگرام (شامل Document کامل Telethon برای ارسال سریع)
  - download_logs: لاگ درخواست‌های دانلود
"""
import datetime
from typing import Dict, Optional, Set

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, LargeBinary,
    String, Text, UniqueConstraint, func, select, delete as sa_delete,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now())
    last_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())


class Video(Base):
    __tablename__ = 'videos'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # کد اختصاصی ویدیو در یوتیوب (مثلاً dQw4w9WgXcQ)؛ ثابت و یکتاست
    video_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    channel: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # مدت زمان ویدیو بر حسب ثانیه (برای نمایش «زمان» در پست اول)
    duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # تخمین حجم هر کیفیت: {"1080": 123456789, "audio": 9876543, ...}
    format_sizes: Mapped[Optional[Dict]] = mapped_column(JSON, nullable=True)
    # بایت‌های تامبنیل (تا پس از حذف ویدیو از یوتیوب هم قابل استفاده بماند)
    thumbnail_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    # ویدیوی عمودی (مثل Shorts)؟ → پست با تامبنیل باریک 9:16 ساخته می‌شود
    is_vertical: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now())


class Media(Base):
    """فایل ارسال‌شده در تلگرام؛ document_data = Document کامل Telethon به صورت bytes.

    با داشتن این ردیف، ارسال بعدی بدون دانلود مجدد («موشکی») انجام می‌شود.
    """
    __tablename__ = 'media'
    __table_args__ = (UniqueConstraint('video_id', 'quality', name='uq_media_video_quality'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        String(32), ForeignKey('videos.video_id'), index=True)
    quality: Mapped[str] = mapped_column(String(10))  # '2160'...,'360','audio','best'
    # مدل جدید کش: شناسهٔ پیام تلگرامی که فایل نهایی در آن است (سبک و همیشه معتبر)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # سازگاری با ردیف‌های قدیمی: بایت‌های Document (ممکن است NULL باشد)
    document_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now())


class DownloadLog(Base):
    __tablename__ = 'download_logs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    video_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    quality: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # cached | done | failed
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True)



# ---------------------------------------------------------------- helpers ---

async def upsert_user(tg_user, is_admin: bool = False) -> None:
    if tg_user is None:
        return
    async with SessionLocal() as s:
        user = await s.get(User, tg_user.id)
        if user is None:
            user = User(
                user_id=tg_user.id,
                username=getattr(tg_user, 'username', None),
                first_name=getattr(tg_user, 'first_name', None),
                last_name=getattr(tg_user, 'last_name', None),
                is_admin=is_admin,
            )
            s.add(user)
        else:
            user.username = getattr(tg_user, 'username', None)
            user.first_name = getattr(tg_user, 'first_name', None)
            user.last_name = getattr(tg_user, 'last_name', None)
            user.is_admin = user.is_admin or is_admin
            user.last_seen = func.now()
        await s.commit()


async def upsert_video(info: Dict, thumbnail_data: Optional[bytes] = None) -> Video:
    """اطلاعات گرفته‌شده از یوتیوب را در جدول videos ذخیره/به‌روزرسانی می‌کند."""
    async with SessionLocal() as s:
        video = await s.scalar(select(Video).where(Video.video_id == info['id']))
        if video is None:
            video = Video(video_id=info['id'])
            s.add(video)
        video.title = info.get('title')
        video.channel = info.get('channel')
        video.thumbnail_url = info.get('thumbnail')
        video.url = info.get('url')
        video.max_height = info.get('max_height')
        dur = info.get('duration')
        if dur:
            video.duration = int(dur)
        video.format_sizes = info.get('sizes')
        video.is_vertical = bool(info.get('is_vertical'))
        if thumbnail_data:
            video.thumbnail_data = thumbnail_data
        await s.commit()
        await s.refresh(video)
        return video


async def save_thumbnail(video_id: str, data: bytes) -> None:
    """ذخیرهٔ بایت‌های تامبنیل برای یک ویدیو (برای تحویل سریع و پس از حذف ویدیو)."""
    async with SessionLocal() as s:
        video = await s.scalar(select(Video).where(Video.video_id == video_id))
        if video is not None and not video.thumbnail_data:
            video.thumbnail_data = data
            await s.commit()


def has_meta(video: Video) -> bool:
    """آیا پست را می‌شود فقط از دیتابیس و فوری ساخت؟

    فقط عنوان، لیست کیفیت‌ها و زمان لازم است (بدون هیچ تماسی با یوتیوب)؛
    تامبنیل اگر باشد بهتر است، ولی نبودنش جلوی سرعت را نمی‌گیرد.
    """
    return bool(video is not None and video.title and video.format_sizes
                and video.duration)


async def get_video(video_id: str) -> Optional[Video]:
    async with SessionLocal() as s:
        return await s.scalar(select(Video).where(Video.video_id == video_id))


async def get_cached_media(video_id: str, quality: str) -> Optional[Media]:
    async with SessionLocal() as s:
        return await s.scalar(
            select(Media).where(Media.video_id == video_id, Media.quality == quality))


async def get_cached_qualities(video_id: str) -> Set[str]:
    async with SessionLocal() as s:
        rows = await s.scalars(
            select(Media.quality).where(Media.video_id == video_id))
        return set(rows.all())


async def save_media(video_id: str, quality: str, chat_id: Optional[int],
                     message_id: Optional[int], size_bytes: Optional[int],
                     mime_type: Optional[str], document_bytes: Optional[bytes] = None,
                     ) -> None:
    """ذخیره/به‌روزرسانی ردیف کش یک کیفیت.

    مدل جدید: به‌جای بایت‌های Document، شناسهٔ پیام تلگرامی که فایل در آن است
    (`chat_id`/`message_id`) ذخیره می‌شود تا ردیف‌ها سبک بمانند و همیشه معتبر باشند.
    اگر پیام تلگرام در دسترس نبود (حذف شده/قدیمی)، `document_bytes` هم پذیرفته
    می‌شود تا سازگاری با ردیف‌های قدیمی حفظ شود.
    """
    async with SessionLocal() as s:
        row = await s.scalar(
            select(Media).where(Media.video_id == video_id, Media.quality == quality))
        if row is None:
            row = Media(video_id=video_id, quality=quality)
            s.add(row)
        row.chat_id = chat_id
        row.message_id = message_id
        row.document_data = document_bytes
        row.size_bytes = size_bytes
        row.mime_type = mime_type
        await s.commit()


async def delete_media(video_id: str, quality: str) -> None:
    """وقتی file_reference منقضی شده باشد ردیف کش حذف می‌شود تا دوباره دانلود شود."""
    async with SessionLocal() as s:
        await s.execute(sa_delete(Media).where(
            Media.video_id == video_id, Media.quality == quality))
        await s.commit()


async def add_log(user_id: int, video_id: Optional[str], quality: Optional[str],
                  status: str) -> None:
    async with SessionLocal() as s:
        s.add(DownloadLog(user_id=user_id, video_id=video_id,
                          quality=quality, status=status))
        await s.commit()


async def get_stats() -> Dict[str, int]:
    """آمار ربات برای دستور /amar ادمین:
      - total_users: تعداد کل کاربرانی که ربات را استارت زده‌اند
      - today_users: تعداد کاربرانی که امروز فعالیت داشته‌اند (یا عضو شده‌اند)
      - downloaded_links: تعداد کل لینک‌های دانلودشده (یکتا بر اساس video_id)
      - cached_videos: تعداد ویدیوهای یکتای موجود در دیتابیس (حتی با چند کیفیت = ۱)
      - total_files: تعداد کل فایل‌های موجود در دیتابیس (با احتساب کیفیت‌ها)
    """
    from sqlalchemy import func as sa_func, distinct
    async with SessionLocal() as s:
        # کل کاربران
        total_users = await s.scalar(select(sa_func.count()).select_from(User))

        # کاربران امروز (بر اساس تایم‌زون دیتابیس: Asia/Tehran)
        # کاربری که امروز عضوشده یا آخرین فعالیتش امروز بوده
        today_users = await s.scalar(
            select(sa_func.count()).select_from(User).where(
                sa_func.date(User.last_seen) == sa_func.current_date()
            )
        )

        # تعداد کل لینک‌های دانلودشده یکتا (که دانلود موفق یا از کش تحویل شده‌اند)
        downloaded_links = await s.scalar(
            select(sa_func.count(distinct(DownloadLog.video_id)))
            .select_from(DownloadLog)
            .where(DownloadLog.status.in_(['done', 'cached']))
        )

        # تعداد ویدیوهای موجود در دیتابیس (یکتا — اگر ۴ کیفیت دارد هم ۱ شمارش می‌شود)
        cached_videos = await s.scalar(
            select(sa_func.count(distinct(Media.video_id))).select_from(Media)
        )

        # تعداد کل فایل‌های آپلودشده (با احتساب چند کیفیت برای هر ویدیو)
        total_files = await s.scalar(
            select(sa_func.count()).select_from(Media)
        )

        return {
            'total_users': total_users or 0,
            'today_users': today_users or 0,
            'downloaded_links': downloaded_links or 0,
            'cached_videos': cached_videos or 0,
            'total_files': total_files or 0,
        }


engine = create_async_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # زمان دیتابیس با زمان لوکال سرور (ایران) هم‌تراز شود
    connect_args={'server_settings': {'timezone': config.DB_TIMEZONE}},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """ایجاد جدول‌ها در صورت نبودن + مهاجرت سبک ستون‌های جدید (idempotent)."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        # ستون‌های جدید باید پیش از create_all اضافه/اطمینان شوند چون مدل ORM
        # ممکن است ستونی را بخواند که create_all برای جدول موجود نمی‌سازد.
        for ddl in (
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS thumbnail_data bytea",
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS is_vertical boolean DEFAULT FALSE",
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS duration integer",
            "ALTER TABLE media ADD COLUMN IF NOT EXISTS chat_id bigint",
            "ALTER TABLE media ADD COLUMN IF NOT EXISTS message_id integer",
            # قدیمی: NOT NULL بود؛ حالا بایت‌ها اجباری نیستند (ارجاع به پیام کافی است)
            "ALTER TABLE media ALTER COLUMN document_data DROP NOT NULL",
        ):
            try:
                await conn.execute(text(ddl))
            except Exception:  # noqa: BLE001
                pass  # جدول هنوز وجود ندارد → create_all آن را می‌سازد
        await conn.run_sync(Base.metadata.create_all)
