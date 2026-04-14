"""Модели базы данных — User, Channel, TwitchDownload"""
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовый класс для всех моделей"""
    pass


class User(Base):
    """Пользователь бота"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    download_count: Mapped[int] = mapped_column(default=0)
    language: Mapped[str] = mapped_column(String(5), default="ru")

    def __repr__(self) -> str:
        return f"<User {self.telegram_id} ({self.username})>"


class Channel(Base):
    """Канал/группа для обязательной подписки"""
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255))
    invite_link: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Channel {self.channel_id} ({self.title})>"


class TwitchDownload(Base):
    """Кэш скачанных VOD/клипов/хайлайтов — хранит file_id Telegram.
    Обрезанные фрагменты НЕ кэшируются (format_key всегда без таймкодов).
    Сплитнутые файлы (несколько частей) тоже не кэшируем — сложно восстанавливать.
    """
    __tablename__ = "twitch_downloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ссылка на VOD/клип/highlight
    twitch_url: Mapped[str] = mapped_column(String(500), index=True)
    # ID VOD или slug клипа (для поиска/дедупликации)
    twitch_id: Mapped[str] = mapped_column(String(100), index=True)
    # тип медиа: vod / clip / highlight
    media_type: Mapped[str] = mapped_column(String(20), default="vod")
    # ключ формата: video_720, video_1080, audio
    format_key: Mapped[str] = mapped_column(String(50))
    # file_id Telegram (одиночный файл; для сплита не кэшируем)
    file_id: Mapped[str] = mapped_column(String(255))
    # тип телеграм-медиа: video / audio (для answer_video vs answer_audio)
    tg_media_type: Mapped[str] = mapped_column(String(20), default="video")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    download_count: Mapped[int] = mapped_column(default=1)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now() + timedelta(days=1),
    )

    __table_args__ = (
        Index("ix_twitch_dl_url_fmt", "twitch_url", "format_key"),
    )

    @property
    def is_expired(self) -> bool:
        return datetime.now(self.expires_at.tzinfo) > self.expires_at

    def __repr__(self) -> str:
        return f"<TwitchDownload {self.twitch_id} [{self.format_key}]>"
