"""Сервис скачивания Twitch — yt-dlp без cookies/прокси.

Twitch не блокирует датацентровые IP, и публичные VOD/клипы доступны без авторизации.
Sub-only VOD — edge-кейс, игнорируем.

Поддерживается:
- VOD:      https://www.twitch.tv/videos/<id>
- clip long: https://www.twitch.tv/<channel>/clip/<slug>
- clip short: https://clips.twitch.tv/<slug>
- highlight / past broadcast — те же URL что VOD

Live-трансляции детектируем и отказываем.
"""
import asyncio
import fcntl
import glob
import logging
import os
import pty
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from bot.config import settings

logger = logging.getLogger(__name__)

# лимит Telegram 2 ГБ, берём с запасом под метаданные/контейнер
MAX_FILE_SIZE = settings.max_file_size                       # 2 ГБ
SAFE_LIMIT = int(1.9 * 1024 * 1024 * 1024)                   # ~1.9 ГБ

# целевые высоты (короткая сторона) для списка качеств
TARGET_HEIGHTS = [160, 360, 480, 720, 1080]

# семафор на количество одновременных скачиваний (ленивая инициализация — внутри активного loop)
_download_semaphore: asyncio.Semaphore | None = None


@dataclass
class QualityOption:
    """Одно доступное качество"""
    height: int                   # 720, 1080 и т.д.
    fps: int = 30                 # 30 / 60
    format_id: str = ""           # yt-dlp format_id (лучший для этой высоты)
    size_bytes: int = 0           # ожидаемый размер в байтах
    will_split: bool = False      # превысит ли SAFE_LIMIT → будет сплит
    label: str = ""               # «720p60» или «1080p»

    @property
    def size_mb(self) -> int:
        return int(self.size_bytes / 1024 / 1024) if self.size_bytes else 0

    @property
    def key(self) -> str:
        """Ключ для format_key (для кэша)."""
        return f"video_{self.height}{'p60' if self.fps >= 50 else ''}"


@dataclass
class VideoInfo:
    """Метаданные до скачивания"""
    title: str
    duration: int
    uploader: str | None = None
    thumbnail: str | None = None
    is_live: bool = False
    media_type: str = "vod"                                       # vod / clip / highlight
    twitch_id: str = ""                                           # VOD id или clip slug
    qualities: list[QualityOption] = field(default_factory=list)  # видео-качества
    audio_size: int = 0                                           # ожидаемый размер аудио-трека в байтах


@dataclass
class DownloadResult:
    """Результат скачивания"""
    file_paths: list[str]         # одна или несколько частей (после сплита)
    media_type: str               # video / audio (telegram media type)
    title: str
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    format_key: str = ""
    was_split: bool = False
    job_dir: str = ""             # рабочий каталог закачки — удаляется целиком по окончании


ProgressCallback = Callable[[float, float, int], None] | None


class FileTooLargeError(Exception):
    """Файл превышает лимит даже после сплита (практически не бывает для Twitch)."""
    pass


class LiveStreamError(Exception):
    """Попытка скачать активный live-стрим."""
    pass


def classify_error(exc: Exception | str) -> str:
    """Классифицирует ошибку yt-dlp / ffmpeg в категорию.
    Возвращает: 'live', 'network', 'unavailable', 'private', 'unknown'.
    """
    if isinstance(exc, LiveStreamError):
        return "live"
    msg = str(exc).lower()
    if "is_live" in msg or "live stream" in msg or "currently live" in msg:
        return "live"
    if "timeout" in msg or "timed out" in msg or "connection" in msg or "unreachable" in msg:
        return "network"
    if "404" in msg or "410" in msg or "not found" in msg or "does not exist" in msg:
        return "unavailable"
    if "private" in msg or "sub-only" in msg or "subscriber" in msg or "login required" in msg:
        return "private"
    if "unavailable" in msg or "deleted" in msg or "expired" in msg:
        return "unavailable"
    return "unknown"


class TwitchDownloader:
    """Скачиватель Twitch через yt-dlp (python API). Без cookies и без прокси."""

    def __init__(self):
        self.download_dir = tempfile.mkdtemp(prefix="twitch_bot_")
        # callback для алертов админу при инфраструктурных сбоях
        self.on_source_failed: Callable[[str, str], None] | None = None
        logger.info("TwitchDownloader: tmp=%s", self.download_dir)

    # ====================== INFO ======================

    def _base_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            # Twitch HLS: качаем по 10 сегментов параллельно (главный драйвер скорости)
            "concurrent_fragment_downloads": 10,
            # workaround для бага "Initialization fragment found after media fragments"
            "hls_use_mpegts": True,
        }
        if settings.proxy_url:
            opts["proxy"] = settings.proxy_url
        return opts

    @staticmethod
    def _wrap_with_proxy(cmd: list[str]) -> list[str]:
        """Если задан PROXY_URL, оборачивает subprocess в proxychains4 (нужно для ffmpeg/TWD,
        которые SOCKS5 нативно не умеют)."""
        if settings.proxy_url:
            return ["proxychains4", "-q", *cmd]
        return cmd

    async def get_info(self, url: str) -> VideoInfo:
        """Получает метаданные VOD/клипа. Live детектим и пробрасываем LiveStreamError."""
        t_start = time.monotonic()
        opts = {**self._base_opts(), "skip_download": True, "ignore_no_formats_error": True}

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, self._extract_info, url, opts)
        except Exception as e:
            cat = classify_error(e)
            if cat == "live":
                raise LiveStreamError("live stream not supported") from e
            if cat in ("unavailable", "private"):
                raise
            self._fire_source_failed("yt-dlp", e)
            raise

        if self._is_live(info):
            raise LiveStreamError("live stream not supported")

        qualities, audio_size = self._parse_qualities(info)
        media_type = self._detect_media_type(info, url)
        twitch_id = str(info.get("id") or "")

        elapsed = time.monotonic() - t_start
        logger.info(
            "[METRIC] twitch.get_info %.2fs type=%s qualities=%d url=%s",
            elapsed, media_type, len(qualities), url,
        )

        return VideoInfo(
            title=info.get("title", "Twitch video"),
            duration=int(info.get("duration") or 0),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=info.get("thumbnail"),
            is_live=False,
            media_type=media_type,
            twitch_id=twitch_id,
            qualities=qualities,
            audio_size=audio_size,
        )

    def _is_live(self, info: dict) -> bool:
        if info.get("is_live"):
            return True
        if info.get("live_status") == "is_live":
            return True
        return False

    def _detect_media_type(self, info: dict, url: str) -> str:
        # yt-dlp extractor: Twitch:vod, Twitch:clips, Twitch:video
        extractor = (info.get("extractor") or "").lower()
        if "clip" in extractor or "clips.twitch.tv" in url or "/clip/" in url:
            return "clip"
        # highlight vs vod — yt-dlp не всегда различает, по длительности определить нельзя
        # для кэширования и UX достаточно vod/clip
        return "vod"

    def _parse_qualities(self, info: dict) -> tuple[list[QualityOption], int]:
        """Парсит formats[], возвращает список QualityOption + оценочный размер аудио."""
        formats = info.get("formats") or []
        duration = int(info.get("duration") or 0)

        # Размер аудио-дорожки (если есть отдельная audio-only)
        audio_size = 0
        for fmt in formats:
            if fmt.get("vcodec", "none") != "none":
                continue
            if fmt.get("acodec", "none") == "none":
                continue
            sz = self._fmt_size(fmt, duration)
            if sz > audio_size:
                audio_size = sz

        # Для Twitch VOD чаще всего прогрессив-hls (vcodec+acodec в одном формате),
        # поэтому отдельного audio_size может не быть — это нормально.

        # Берём лучший video-format для каждой уникальной (height, fps) пары.
        by_bucket: dict[tuple[int, int], dict] = {}
        for fmt in formats:
            if fmt.get("vcodec", "none") == "none":
                continue
            h = fmt.get("height") or 0
            w = fmt.get("width") or 0
            # для Twitch clips yt-dlp часто не заполняет height/width — пробуем format_id/format_note
            if not h and not w:
                h = self._height_from_meta(fmt)
                if not h:
                    continue
            short_side = min(h, w) if (h and w) else (h or w)
            # привязка к ближайшей целевой высоте
            target = self._snap_to_target(short_side)
            if target is None:
                continue
            fps = int(round(fmt.get("fps") or 30))
            # округление до 30/60
            fps_bucket = 60 if fps >= 50 else 30
            key = (target, fps_bucket)
            sz = self._fmt_size(fmt, duration)
            existing = by_bucket.get(key)
            if existing is None or sz > self._fmt_size(existing, duration):
                by_bucket[key] = fmt

        # для клипов (прямой mp4-URL, protocol=https) Twitch не отдаёт tbr/filesize —
        # дёргаем HEAD за Content-Length, параллельно
        media_type_hint = self._detect_media_type(info, info.get("webpage_url") or "")
        if media_type_hint == "clip":
            self._fill_clip_sizes_via_head(list(by_bucket.values()))

        qualities: list[QualityOption] = []
        for (height, fps_bucket), fmt in by_bucket.items():
            sz = self._fmt_size(fmt, duration)
            # для прогрессивных форматов (vcodec+acodec в одном) audio уже учтён в sz —
            # прибавляем только если в этом формате нет аудио-дорожки
            if fmt.get("acodec", "none") == "none":
                sz += audio_size
            qo = QualityOption(
                height=height,
                fps=fps_bucket,
                format_id=str(fmt.get("format_id") or ""),
                size_bytes=sz,
                will_split=sz > SAFE_LIMIT,
                label=f"{height}p{'60' if fps_bucket >= 60 else ''}",
            )
            qualities.append(qo)

        # сортируем по высоте (asc), при равной — 30 < 60
        qualities.sort(key=lambda q: (q.height, q.fps))

        # если вдруг ничего не нашли — возвращаем дефолт «best»
        if not qualities:
            qualities.append(QualityOption(
                height=720, fps=30, format_id="best",
                size_bytes=0, will_split=False, label="720p",
            ))

        return qualities, audio_size

    @staticmethod
    def _fill_clip_sizes_via_head(formats: list[dict]) -> None:
        """Для клипов (прямой mp4-URL) делает HEAD за Content-Length и записывает filesize в формат."""
        import urllib.request
        for fmt in formats:
            if fmt.get("filesize") or fmt.get("filesize_approx"):
                continue
            url = fmt.get("url")
            if not url or not url.startswith("http"):
                continue
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        fmt["filesize"] = int(cl)
            except Exception:
                pass

    @staticmethod
    def _height_from_meta(fmt: dict) -> int:
        """Извлекает высоту из format_id/format_note для клипов (Twitch-extractor отдаёт '1080', '720p60')."""
        import re
        for field in ("format_id", "format_note", "format"):
            val = str(fmt.get(field) or "")
            m = re.search(r"(\d{3,4})p?", val)
            if m:
                h = int(m.group(1))
                if 100 <= h <= 2160:
                    return h
        return 0

    @staticmethod
    def _snap_to_target(side: int) -> int | None:
        """Привязка нестандартной высоты к целевой сетке (допуск ±40 px)."""
        best = None
        for t in TARGET_HEIGHTS:
            if abs(side - t) <= 40:
                if best is None or abs(side - t) < abs(side - best):
                    best = t
        return best

    @staticmethod
    def _fmt_size(fmt: dict, duration: int) -> int:
        """Оценка размера формата в байтах."""
        sz = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if not sz and fmt.get("tbr") and duration:
            # tbr в kbit/s → байт = tbr*1000/8 * duration
            # +10% запас: HLS-overhead контейнера + спайки битрейта
            sz = int(fmt["tbr"] * 1000 / 8 * duration * 1.10)
        return int(sz or 0)

    # ====================== DOWNLOAD ======================

    async def download_video(
        self,
        url: str,
        quality: QualityOption,
        progress_callback: ProgressCallback = None,
        sections: tuple[int, int] | None = None,
    ) -> DownloadResult:
        """Скачивает VOD/клип.
        sections=(start_sec, end_sec) — обрезка фрагмента через yt-dlp --download-sections.
        """
        self._cleanup_old_files()
        t_start = time.monotonic()

        # уникальный subdir для каждой закачки (исключает race между параллельными загрузками)
        job_dir = tempfile.mkdtemp(dir=self.download_dir)
        try:
            return await self._download_video_impl(
                url, quality, job_dir, progress_callback, sections, t_start,
            )
        except BaseException:
            # любая ошибка/отмена → чистим каталог целиком (частично скачанные файлы, .part, .ytdl)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    async def _download_video_impl(
        self,
        url: str,
        quality: QualityOption,
        job_dir: str,
        progress_callback: ProgressCallback,
        sections: tuple[int, int] | None,
        t_start: float,
    ) -> DownloadResult:
        # primary: TwitchDownloaderCLI — быстрее yt-dlp в 3-5x, поддерживает обрезку нативно
        try:
            return await self._download_via_twitch_cli(
                url, quality, job_dir, progress_callback, sections, t_start,
            )
        except LiveStreamError:
            raise
        except Exception as e:
            cat = classify_error(e)
            if cat in ("live", "unavailable", "private"):
                raise
            logger.warning("TwitchDownloaderCLI упал (%s), fallback на yt-dlp", str(e)[:120])

        # обрезка → ffmpeg fast-seek (качаем только нужные сегменты HLS)
        if sections is not None:
            return await self._download_video_trim(
                url, quality, job_dir, progress_callback, sections, t_start,
            )

        # полное скачивание → yt-dlp native с параллельным скачиванием сегментов
        height = quality.height
        fps = quality.fps
        fps_filter = f"[fps<={fps + 5}]"
        if quality.format_id:
            format_str = (
                f"{quality.format_id}+bestaudio"
                f"/{quality.format_id}"
                f"/bestvideo[height<={height}]{fps_filter}+bestaudio"
                f"/best[height<={height}]{fps_filter}"
                f"/best"
            )
        else:
            format_str = (
                f"bestvideo[height<={height}]{fps_filter}+bestaudio"
                f"/best[height<={height}]{fps_filter}"
                f"/best[height<={height}]"
                f"/best"
            )

        output_template = os.path.join(
            job_dir, "%(id)s_{q}.%(ext)s".format(q=quality.label),
        )
        ydl_opts = {
            **self._base_opts(),
            "format": format_str,
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "live_from_start": False,
        }

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(
                None, self._download, url, ydl_opts, progress_callback,
            )
        except Exception as e:
            cat = classify_error(e)
            if cat == "live":
                raise LiveStreamError("live stream not supported") from e
            if cat in ("unavailable", "private"):
                raise
            # fallback: если yt-dlp упал с "Initialization fragment" или похожим —
            # пробуем через ffmpeg-стриминг (медленнее, но надёжнее)
            msg = str(e).lower()
            if "initialization fragment" in msg or "unable to download" in msg:
                logger.warning("yt-dlp native упал (%s), fallback на ffmpeg", str(e)[:100])
                return await self._download_video_via_ffmpeg(
                    url, quality, job_dir, progress_callback, None, t_start,
                )
            self._fire_source_failed("yt-dlp", e)
            raise

        file_path = self._find_downloaded_file(info, "mp4", job_dir)
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("Не удалось найти скачанный видеофайл")

        duration = int(info.get("duration") or 0)
        paths, was_split = await self._split_if_needed(file_path, duration)

        elapsed = time.monotonic() - t_start
        try:
            total_mb = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
        except OSError:
            total_mb = 0
        logger.info(
            "[METRIC] twitch.download_video %.2fs quality=%s size=%.1fMB parts=%d split=%s",
            elapsed, quality.label, total_mb, len(paths), was_split,
        )

        return DownloadResult(
            file_paths=paths,
            media_type="video",
            title=info.get("title", "Twitch video"),
            duration=duration,
            width=info.get("width"),
            height=info.get("height"),
            format_key=quality.key,
            was_split=was_split,
            job_dir=job_dir,
        )

    async def _download_video_trim(
        self,
        url: str,
        quality: QualityOption,
        job_dir: str,
        progress_callback: ProgressCallback,
        sections: tuple[int, int],
        t_start: float,
    ) -> DownloadResult:
        """Обрезанный фрагмент — через ffmpeg fast-seek из HLS-URL."""
        return await self._download_video_via_ffmpeg(
            url, quality, job_dir, progress_callback, sections, t_start,
        )

    async def _download_video_via_ffmpeg(
        self,
        url: str,
        quality: QualityOption,
        job_dir: str,
        progress_callback: ProgressCallback,
        sections: tuple[int, int] | None,
        t_start: float,
    ) -> DownloadResult:
        """ffmpeg-путь: yt-dlp → stream URL, ffmpeg тянет HLS (-c copy)."""
        height = quality.height
        fps = quality.fps
        fps_filter = f"[fps<={fps + 5}]"
        if quality.format_id:
            format_str = (
                f"{quality.format_id}"
                f"/best[height<={height}]{fps_filter}"
                f"/best[height<={height}]"
                f"/best"
            )
        else:
            format_str = (
                f"best[height<={height}]{fps_filter}"
                f"/best[height<={height}]"
                f"/best"
            )
        info_opts = {**self._base_opts(), "format": format_str, "skip_download": True}
        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, self._extract_info, url, info_opts)
        except Exception as e:
            cat = classify_error(e)
            if cat == "live":
                raise LiveStreamError("live stream not supported") from e
            if cat in ("unavailable", "private"):
                raise
            self._fire_source_failed("yt-dlp", e)
            raise

        stream_url = info.get("url")
        if not stream_url:
            rf = info.get("requested_formats") or []
            if rf:
                stream_url = rf[0].get("url")
        if not stream_url:
            raise RuntimeError("Не удалось получить stream-URL")

        vod_duration = int(info.get("duration") or 0)
        if sections is not None:
            start_sec, end_sec = sections
            duration = end_sec - start_sec
            suffix = "_trim"
        else:
            start_sec = None
            end_sec = None
            duration = vod_duration
            suffix = ""

        output_path = os.path.join(
            job_dir, f"{info.get('id') or 'vod'}_{quality.label}{suffix}.mp4",
        )
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if start_sec is not None:
            cmd += ["-ss", str(start_sec), "-to", str(end_sec)]
        cmd += [
            "-i", stream_url,
            "-c", "copy",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            output_path,
        ]

        await self._run_ffmpeg_progress(cmd, duration, progress_callback)

        if not os.path.exists(output_path):
            raise RuntimeError("ffmpeg не создал выходной файл")

        paths, was_split = await self._split_if_needed(output_path, duration)
        elapsed = time.monotonic() - t_start
        try:
            total_mb = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
        except OSError:
            total_mb = 0
        logger.info(
            "[METRIC] twitch.download_video_ffmpeg %.2fs quality=%s size=%.1fMB trim=%s",
            elapsed, quality.label, total_mb, sections is not None,
        )

        return DownloadResult(
            file_paths=paths,
            media_type="video",
            title=info.get("title", "Twitch video"),
            duration=duration,
            width=info.get("width"),
            height=info.get("height"),
            format_key=quality.key,
            was_split=was_split,
            job_dir=job_dir,
        )

    async def _download_via_twitch_cli(
        self,
        url: str,
        quality: QualityOption,
        job_dir: str,
        progress_callback: ProgressCallback,
        sections: tuple[int, int] | None,
        t_start: float,
    ) -> DownloadResult:
        """Primary: TwitchDownloaderCLI — через Twitch GraphQL, параллельная загрузка чанков.
        Поддерживает VOD и clips, нативная обрезка через --beginning/--ending.
        """
        from bot.utils.helpers import extract_twitch_id, seconds_to_timecode

        twitch_id = extract_twitch_id(url) or ""
        if not twitch_id:
            raise RuntimeError("Не удалось извлечь ID из URL")

        is_clip = ("/clip/" in url) or ("clips.twitch.tv" in url)
        subcmd = "clipdownload" if is_clip else "videodownload"

        output_path = os.path.join(
            job_dir, f"{twitch_id}_{quality.label}{'_trim' if sections else ''}.mp4",
        )

        # TwitchDownloaderCLI quality names совпадают с нашими label: "1080p60", "720p", ...
        cmd = [
            "TwitchDownloaderCLI", subcmd,
            "--id", twitch_id,
            "--quality", quality.label,
            "-o", output_path,
            "--collision", "Overwrite",
            "--temp-path", job_dir,
            "--threads", "20",
        ]
        # для VOD — нативная обрезка на уровне сегментов HLS
        if sections is not None and not is_clip:
            start_sec, end_sec = sections
            cmd += [
                "-b", seconds_to_timecode(start_sec),
                "-e", seconds_to_timecode(end_sec),
            ]

        # длительность нужна для прогресс-бара
        if sections is not None:
            duration = sections[1] - sections[0]
        else:
            duration = 0  # для клипов неизвестно, узнаем из GraphQL инфо при get_info

        # Запускаем TWD через псевдо-терминал (PTY), чтобы IsOutputRedirected=False
        # и CLI слал `\r`-обновления прогресса, как в интерактивном терминале.
        proc, reader = await self._spawn_with_pty(self._wrap_with_proxy(cmd))

        stderr_buf: list[str] = []
        stdout_data = b""
        try:
            await self._parse_twd_progress_stream(
                reader, progress_callback, stderr_buf,
                expected_size=quality.size_bytes,
            )
            await proc.wait()
        finally:
            try:
                reader.feed_eof()
            except Exception:
                pass
        if proc.returncode != 0:
            full_err = "\n".join(stderr_buf) if stderr_buf else ""
            full_out = stdout_data.decode("utf-8", errors="ignore")
            logger.error(
                "TwitchDownloaderCLI cmd=%s rc=%s\n---STDERR---\n%s\n---STDOUT---\n%s",
                " ".join(cmd), proc.returncode, full_err[:2000], full_out[:2000],
            )
            # ищем первую строку похожую на Exception/Error message — она информативнее чем stack trace
            msg = ""
            for line in stderr_buf:
                if any(k in line for k in ("Exception", "Error:", "error:", "[ERROR]")):
                    msg = line
                    break
            if not msg and stderr_buf:
                msg = stderr_buf[0]
            raise RuntimeError(f"TwitchDownloaderCLI rc={proc.returncode}: {msg[:400]}")

        if not os.path.exists(output_path):
            raise RuntimeError("TwitchDownloaderCLI не создал выходной файл")

        # для прогрессивного сплита нужна длительность — берём из ffprobe
        if duration == 0:
            _, _, probed_dur = await self.probe_media(output_path)
            duration = probed_dur or 0

        paths, was_split = await self._split_if_needed(output_path, duration)
        elapsed = time.monotonic() - t_start
        try:
            total_mb = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
        except OSError:
            total_mb = 0
        logger.info(
            "[METRIC] twitch.download_twd %.2fs quality=%s size=%.1fMB parts=%d trim=%s clip=%s",
            elapsed, quality.label, total_mb, len(paths), sections is not None, is_clip,
        )

        return DownloadResult(
            file_paths=paths,
            media_type="video",
            title=f"Twitch {twitch_id}",
            duration=duration,
            format_key=quality.key,
            was_split=was_split,
            job_dir=job_dir,
        )

    async def _spawn_with_pty(
        self, cmd: list[str],
    ) -> tuple[asyncio.subprocess.Process, asyncio.StreamReader]:
        """Запускает subprocess через pty → child думает что в интерактивном терминале.
        Возвращает (process, reader) — stdout+stderr процесса сливаются в один reader.
        """
        master_fd, slave_fd = pty.openpty()
        # неблокирующее чтение через asyncio
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        finally:
            # slave_fd нужен только child'у — закрываем в родителе сразу после fork
            os.close(slave_fd)

        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        master_file = os.fdopen(master_fd, "rb", buffering=0)
        await loop.connect_read_pipe(lambda: protocol, master_file)
        return proc, reader

    async def _parse_twd_progress_stream(
        self,
        reader: asyncio.StreamReader,
        progress_callback: ProgressCallback,
        stderr_buf: list[str] | None = None,
        expected_size: int = 0,
    ) -> None:
        """Читает PTY-вывод TWD, парсит `[STATUS] - Downloading 45% [2/4]` и т.п.
        В PTY-режиме TWD обновляет строку через `\\r`, поэтому режем буфер по `\\r` и `\\n`.
        Если expected_size > 0 — вычисляем ориентировочные dl_mb/total_mb для отрисовки размера.
        """
        import re
        pct_re = re.compile(r"(\d{1,3})%")
        last_update = 0.0
        buffer = bytearray()

        def _flush(line: str) -> None:
            nonlocal last_update
            line = line.strip()
            if not line:
                return
            if stderr_buf is not None:
                stderr_buf.append(line)
                if len(stderr_buf) > 50:
                    del stderr_buf[:-50]
            m = pct_re.search(line)
            if not m or not progress_callback:
                return
            percent = int(m.group(1))
            now = time.time()
            if now - last_update < 2 and percent < 100:
                return
            last_update = now
            if expected_size > 0:
                total_mb = expected_size / 1024 / 1024
                dl_mb = total_mb * percent / 100
            else:
                total_mb = 0
                dl_mb = 0
            try:
                progress_callback(dl_mb, total_mb, percent)
            except Exception:
                pass

        while True:
            try:
                chunk = await reader.read(256)
            except Exception:
                return
            if not chunk:
                if buffer:
                    _flush(buffer.decode("utf-8", errors="ignore"))
                return
            buffer.extend(chunk)
            while True:
                idx = -1
                for c in (b"\r", b"\n"):
                    i = buffer.find(c)
                    if i >= 0 and (idx < 0 or i < idx):
                        idx = i
                if idx < 0:
                    break
                line_bytes = bytes(buffer[:idx])
                del buffer[:idx + 1]
                _flush(line_bytes.decode("utf-8", errors="ignore"))

    async def _run_ffmpeg_progress(
        self,
        cmd: list[str],
        duration: int,
        progress_callback: ProgressCallback,
    ) -> None:
        """Запускает ffmpeg, парсит `-progress pipe:1` → дёргает progress_callback(dl_mb, total_mb, percent)."""
        # ffmpeg SOCKS5 нативно не умеет — оборачиваем в proxychains4 если PROXY_URL задан
        proc = await asyncio.create_subprocess_exec(
            *self._wrap_with_proxy(cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stderr_bytes = bytearray()

        async def _read_stderr() -> None:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                stderr_bytes.extend(line)

        async def _read_progress() -> None:
            last_update = 0.0
            out_time_ms = 0
            total_size = 0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="ignore").strip()
                if "=" not in text:
                    continue
                k, _, v = text.partition("=")
                if k == "out_time_ms" and v.isdigit():
                    out_time_ms = int(v)
                elif k == "total_size" and v.isdigit():
                    total_size = int(v)
                elif k == "progress":
                    if progress_callback and duration > 0:
                        now = time.time()
                        if now - last_update < 2 and v != "end":
                            continue
                        last_update = now
                        elapsed_sec = out_time_ms / 1_000_000
                        percent = int(min(elapsed_sec / duration * 100, 100))
                        # примерная оценка полного размера по текущему прогрессу
                        if elapsed_sec > 0 and total_size > 0:
                            total_est = total_size * duration / elapsed_sec
                        else:
                            total_est = 0
                        dl_mb = total_size / 1024 / 1024
                        total_mb = total_est / 1024 / 1024
                        try:
                            progress_callback(dl_mb, total_mb, percent)
                        except Exception:
                            pass

        await asyncio.gather(_read_progress(), _read_stderr())
        await proc.wait()
        if proc.returncode != 0:
            err = bytes(stderr_bytes).decode("utf-8", errors="ignore")
            logger.error("ffmpeg failed (rc=%s): %s", proc.returncode, err[:500])
            raise RuntimeError(f"ffmpeg: {err[:200]}")

    async def download_audio(
        self,
        url: str,
        progress_callback: ProgressCallback = None,
        sections: tuple[int, int] | None = None,
    ) -> DownloadResult:
        """Скачивает только аудио (mp3)."""
        self._cleanup_old_files()
        t_start = time.monotonic()

        # уникальный subdir для каждой закачки
        job_dir = tempfile.mkdtemp(dir=self.download_dir)
        try:
            return await self._download_audio_impl(
                url, job_dir, progress_callback, sections, t_start,
            )
        except BaseException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    async def _download_audio_impl(
        self,
        url: str,
        job_dir: str,
        progress_callback: ProgressCallback,
        sections: tuple[int, int] | None,
        t_start: float,
    ) -> DownloadResult:
        """Скачивание аудио напрямую из audio_only HLS-потока Twitch без перекодирования."""
        return await self._download_audio_native(
            url, job_dir, progress_callback, sections, t_start,
        )

    async def _download_audio_native(
        self,
        url: str,
        job_dir: str,
        progress_callback: ProgressCallback,
        sections: tuple[int, int] | None,
        t_start: float,
    ) -> DownloadResult:
        """yt-dlp выбирает audio_only HLS-поток (128kbps AAC) → ffmpeg -c:a copy → m4a.
        Без libmp3lame, без перекодирования — только download+remux. Как у YouTube-бота.
        """
        # format=bestaudio заставляет yt-dlp вернуть URL audio_only плейлиста;
        # фолбэки на worst/best — если audio_only внезапно недоступен
        info_opts = {
            **self._base_opts(),
            "format": "bestaudio/worstaudio/worst",
            "skip_download": True,
        }
        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, self._extract_info, url, info_opts)
        except Exception as e:
            cat = classify_error(e)
            if cat == "live":
                raise LiveStreamError("live stream not supported") from e
            if cat in ("unavailable", "private"):
                raise
            self._fire_source_failed("yt-dlp", e)
            raise

        stream_url = info.get("url")
        if not stream_url:
            rf = info.get("requested_formats") or []
            if rf:
                stream_url = rf[0].get("url")
        if not stream_url:
            raise RuntimeError("Не удалось получить audio stream-URL")

        vod_duration = int(info.get("duration") or 0)
        if sections is not None:
            start_sec, end_sec = sections
            duration = end_sec - start_sec
            suffix = "_trim"
        else:
            start_sec = None
            end_sec = None
            duration = vod_duration
            suffix = ""

        output_path = os.path.join(job_dir, f"{info.get('id') or 'vod'}{suffix}.m4a")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if start_sec is not None:
            cmd += ["-ss", str(start_sec), "-to", str(end_sec)]
        cmd += [
            "-i", stream_url,
            "-vn",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            output_path,
        ]

        await self._run_ffmpeg_progress(cmd, duration, progress_callback)
        if not os.path.exists(output_path):
            raise RuntimeError("ffmpeg не создал аудиофайл")

        paths, was_split = await self._split_if_needed(output_path, duration, is_audio=True)
        elapsed = time.monotonic() - t_start
        try:
            size_mb = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
        except OSError:
            size_mb = 0
        logger.info(
            "[METRIC] twitch.download_audio %.2fs size=%.1fMB trim=%s",
            elapsed, size_mb, sections is not None,
        )
        return DownloadResult(
            file_paths=paths,
            media_type="audio",
            title=info.get("title", "Twitch audio"),
            duration=duration,
            format_key="audio",
            was_split=was_split,
            job_dir=job_dir,
        )

    # ====================== SPLIT ======================

    async def _split_if_needed(
        self, file_path: str, duration: int, is_audio: bool = False,
    ) -> tuple[list[str], bool]:
        """Проверяет размер файла. Если > SAFE_LIMIT — режет через ffmpeg на куски по ~1.9 ГБ.
        Возвращает (список_путей, был_ли_сплит).
        """
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return [file_path], False

        if size <= SAFE_LIMIT:
            return [file_path], False

        if duration <= 0:
            # без длительности посчитать segment_time нельзя — отдаём как есть,
            # верхний слой отбьёт FileTooLargeError если > 2ГБ
            if size > MAX_FILE_SIZE:
                os.remove(file_path)
                raise FileTooLargeError(f"{size / 1024 / 1024:.0f} МБ, duration неизвестна")
            return [file_path], False

        # расчёт segment_time так, чтобы кусок был ~SAFE_LIMIT
        ratio = SAFE_LIMIT / size
        # минимум 10 сек, максимум — длительность файла (edge case коротких видео)
        segment_time = max(10, min(int(duration * ratio), max(duration - 1, 10)))
        logger.info(
            "Splitting %s: size=%.1fMB dur=%ds segment=%ds",
            file_path, size / 1024 / 1024, duration, segment_time,
        )

        return await self._ffmpeg_split(file_path, segment_time, is_audio)

    async def _ffmpeg_split(
        self, file_path: str, segment_time: int, is_audio: bool,
    ) -> tuple[list[str], bool]:
        """Режет файл через ffmpeg -f segment, возвращает список кусков."""
        base, ext = os.path.splitext(file_path)
        out_template = f"{base}_part%03d{ext}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", file_path,
            "-c", "copy",
            "-map", "0",
            "-f", "segment",
            "-segment_time", str(segment_time),
            "-reset_timestamps", "1",
            out_template,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="ignore")
            logger.error("ffmpeg split failed: %s", err)
            # fallback — отдаём исходный файл
            size = os.path.getsize(file_path)
            if size > MAX_FILE_SIZE:
                os.remove(file_path)
                raise FileTooLargeError(f"split failed, file {size / 1024 / 1024:.0f} MB")
            return [file_path], False

        # собираем все part-файлы (glob быстрее, чем перебор range)
        parts = sorted(glob.glob(f"{glob.escape(base)}_part*{ext}"))
        if not parts:
            return [file_path], False

        # исходник больше не нужен
        self._remove_file(file_path)

        # sanity-check: если какой-то кусок > 2ГБ — FileTooLargeError
        for p in parts:
            if os.path.getsize(p) > MAX_FILE_SIZE:
                for pp in parts:
                    self._remove_file(pp)
                raise FileTooLargeError(f"part {p} > 2GB")

        return parts, True

    # ====================== helpers ======================

    def _extract_info(self, url: str, opts: dict) -> dict:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _download(self, url: str, opts: dict, progress_callback: ProgressCallback = None) -> dict:
        """Синхронный yt-dlp download с progress hook (вызывается из executor)."""
        import yt_dlp

        if progress_callback:
            last_update = {"time": 0.0}

            def _hook(d):
                if d.get("status") != "downloading":
                    return
                now = time.time()
                if now - last_update["time"] < 3:
                    return
                last_update["time"] = now
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0) or 0
                if total > 0:
                    percent = int(downloaded / total * 100)
                    dl_mb = downloaded / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    try:
                        progress_callback(dl_mb, total_mb, percent)
                    except Exception:
                        pass

            opts = {**opts, "progress_hooks": [_hook]}

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    def _find_downloaded_file(
        self, info: dict, expected_ext: str, job_dir: str,
    ) -> str | None:
        requested = info.get("requested_downloads") or []
        if requested:
            fp = requested[0].get("filepath")
            if fp and os.path.exists(fp):
                return fp
        video_id = str(info.get("id") or "")
        if os.path.isdir(job_dir):
            for fn in os.listdir(job_dir):
                if fn.endswith(f".{expected_ext}") and (not video_id or video_id in fn):
                    return os.path.join(job_dir, fn)
        return None

    def _cleanup_old_files(self, max_age_minutes: int = 120) -> None:
        """Чистим только файлы старше cutoff (по умолчанию 2 часа).
        Не трогаем файлы которые могут быть в работе у параллельных закачек —
        downloader пишет в уникальные subdir, а cutoff достаточно велик.
        """
        now = time.time()
        cutoff = now - max_age_minutes * 60
        try:
            for root, _dirs, files in os.walk(self.download_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getmtime(fp) < cutoff:
                            os.remove(fp)
                    except OSError:
                        pass
        except OSError:
            pass

    def cleanup(self, result: DownloadResult) -> None:
        """Удаляет весь рабочий каталог закачки целиком (все файлы, части сплита, .part и т.д.)."""
        if result.job_dir and os.path.isdir(result.job_dir):
            shutil.rmtree(result.job_dir, ignore_errors=True)
            return
        # fallback — если job_dir не задан, удаляем файлы поштучно
        for p in result.file_paths:
            self._remove_file(p)

    async def probe_media(self, path: str) -> tuple[int | None, int | None, int | None]:
        """ffprobe → (width, height, duration_sec). Надёжнее чем поля yt-dlp info."""
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=0",
            path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None, None, None
            w = h = d = None
            for line in stdout.decode("utf-8", errors="ignore").splitlines():
                k, _, v = line.partition("=")
                if k == "width" and v.isdigit():
                    w = int(v)
                elif k == "height" and v.isdigit():
                    h = int(v)
                elif k == "duration":
                    try:
                        d = int(float(v))
                    except ValueError:
                        pass
            return w, h, d
        except Exception:
            return None, None, None

    def cleanup_job_dir(self, job_dir: str) -> None:
        """Удаляет рабочий каталог закачки (для случаев когда result ещё не сформирован)."""
        if job_dir and os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)

    def _remove_file(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.warning("Не удалось удалить файл %s: %s", path, e)

    def _fire_source_failed(self, source: str, error: Exception) -> None:
        if self.on_source_failed is None:
            return
        try:
            self.on_source_failed(source, str(error))
        except Exception as e:
            logger.warning("on_source_failed callback упал: %s", e)


# семафор и доступ к нему для хендлера
def get_semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(5)
    return _download_semaphore


# глобальный экземпляр
downloader = TwitchDownloader()
