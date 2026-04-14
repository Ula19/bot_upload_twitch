"""Хэндлер скачивания с Twitch — VOD, клипы, хайлайты.
Флоу: ссылка → инфо → выбор формата (видео/аудио/обрезка) →
выбор качества → скачивание → (сплит если нужно) → отправка.
"""
import asyncio
import logging
import os
import time

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.config import settings
from bot.database import async_session
from bot.database.crud import (
    get_cached_twitch_download,
    get_or_create_user,
    get_user_language,
    save_twitch_download,
)
from bot.emojis import E
from bot.i18n import t
from bot.keyboards.inline import (
    get_back_keyboard,
    get_format_keyboard,
    get_quality_keyboard,
)
from bot.services.twitch import (
    SAFE_LIMIT,
    FileTooLargeError,
    LiveStreamError,
    QualityOption,
    classify_error,
    downloader,
    get_semaphore,
)
from bot.utils.helpers import (
    clean_twitch_url,
    extract_twitch_id,
    is_twitch_url,
    parse_timecodes_pair,
    seconds_to_timecode,
)

logger = logging.getLogger(__name__)
router = Router()

# троттлинг прогресса живёт в yt-dlp хуке (3 сек), здесь не дублируем


class TwitchStates(StatesGroup):
    waiting_format = State()
    waiting_quality = State()
    waiting_timecodes = State()


# ====================== URL handler ======================

@router.message(StateFilter(None), F.text)
async def handle_twitch_link(message: Message, state: FSMContext) -> None:
    """Главный входной хендлер — принимаем текст, проверяем что это Twitch URL."""
    text = (message.text or "").strip()

    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)

    if not is_twitch_url(text):
        await message.answer(t("download.not_twitch", lang), parse_mode="HTML")
        return

    url = clean_twitch_url(text)
    twitch_id = extract_twitch_id(url) or ""

    status_msg = None
    try:
        status_msg = await message.answer(t("download.fetching_info", lang))
        info = await downloader.get_info(url)
    except LiveStreamError:
        target = status_msg or message
        await _send_or_edit(target, t("twitch_live_not_supported", lang))
        return
    except Exception as e:
        logger.warning("get_info failed for %s: %s", url, e)
        target = status_msg or message
        await _send_or_edit(target, _error_text(e, lang))
        return

    # отфильтровываем качества которые даже после сплита были бы неудобны? — ничего не фильтруем,
    # помечаем «будет разбито» и предупреждаем юзера в клавиатуре
    duration_str = _format_duration(info.duration)

    await state.set_state(TwitchStates.waiting_format)
    # сохраняем в FSM: список качеств как dict'ы (QualityOption не сериализуется MemoryStorage напрямую, но можно)
    await state.update_data(
        url=url,
        twitch_id=info.twitch_id or twitch_id,
        media_type=info.media_type,
        title=info.title,
        duration=info.duration,
        uploader=info.uploader or "—",
        qualities=[_quality_to_dict(q) for q in info.qualities],
    )

    target = status_msg or message
    await _send_or_edit(
        target,
        t("twitch_choose_format", lang,
          title=info.title,
          duration=duration_str,
          uploader=info.uploader or "—"),
        reply_markup=get_format_keyboard(lang, with_trim=info.duration > 0),
    )


# ====================== Выбор формата ======================

@router.callback_query(F.data == "fmt_video", TwitchStates.waiting_format)
async def choose_video_format(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)

    data = await state.get_data()
    qualities = [_quality_from_dict(q) for q in (data.get("qualities") or [])]

    await state.set_state(TwitchStates.waiting_quality)

    await callback.message.edit_text(
        t("twitch_choose_quality", lang),
        reply_markup=get_quality_keyboard(qualities, lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "fmt_audio", TwitchStates.waiting_format)
async def choose_audio_format(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)

    data = await state.get_data()
    url = data.get("url")
    sections = data.get("sections")  # если пришли из trim-флоу
    await state.clear()

    await callback.answer()

    if not url:
        await callback.message.answer(f"{E['cross']} Ссылка не найдена, отправь заново")
        return

    await _process_download(
        callback.message, url,
        format_key="audio",
        quality=None,
        user=callback.from_user,
        lang=lang,
        media_type=data.get("media_type", "vod"),
        twitch_id=data.get("twitch_id", ""),
        sections=tuple(sections) if sections else None,
        title=data.get("title", ""),
    )


# ====================== Обрезка ======================

@router.callback_query(F.data == "twitch_trim", TwitchStates.waiting_format)
async def ask_timecodes(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)

    data = await state.get_data()
    duration = int(data.get("duration") or 0)

    await state.set_state(TwitchStates.waiting_timecodes)

    await callback.message.edit_text(
        t("twitch_trim_prompt", lang, duration=_format_duration(duration)),
        reply_markup=get_back_keyboard(lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(TwitchStates.waiting_timecodes, F.text)
async def receive_timecodes(message: Message, state: FSMContext) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)

    data = await state.get_data()
    duration = int(data.get("duration") or 0)

    pair = parse_timecodes_pair(message.text or "")
    if pair is None or (duration and pair[1] > duration):
        await message.answer(t("twitch_trim_invalid", lang), parse_mode="HTML")
        return

    start_sec, end_sec = pair
    qualities = [_quality_from_dict(q) for q in (data.get("qualities") or [])]
    # у trim-фрагмента другой ожидаемый размер — масштабируем пропорционально
    if duration > 0:
        ratio = (end_sec - start_sec) / duration
        for q in qualities:
            q.size_bytes = int(q.size_bytes * ratio)
            q.will_split = q.size_bytes > SAFE_LIMIT

    await state.update_data(sections=[start_sec, end_sec])
    await state.set_state(TwitchStates.waiting_quality)

    await message.answer(
        t("twitch_choose_quality", lang),
        reply_markup=get_quality_keyboard(qualities, lang),
        parse_mode="HTML",
    )


# ====================== Выбор качества ======================

@router.callback_query(F.data.startswith("quality_"), TwitchStates.waiting_quality)
async def choose_quality(callback: CallbackQuery, state: FSMContext) -> None:
    # отвечаем сразу, чтобы Telegram не показывал "loading" и не словил таймаут
    await callback.answer()

    # сразу убираем клавиатуру качеств — повторный клик невозможен
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    idx_str = callback.data.replace("quality_", "")
    try:
        idx = int(idx_str)
    except ValueError:
        return

    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)

    data = await state.get_data()
    qualities = [_quality_from_dict(q) for q in (data.get("qualities") or [])]
    url = data.get("url")
    sections = data.get("sections")

    await state.clear()

    if not url or idx < 0 or idx >= len(qualities):
        await callback.message.answer(f"{E['cross']} Ссылка не найдена, отправь заново")
        return

    quality = qualities[idx]

    # запускаем загрузку фоном, чтобы не блокировать обработку других апдейтов в очереди
    asyncio.create_task(_process_download(
        callback.message, url,
        format_key=quality.key,
        quality=quality,
        user=callback.from_user,
        lang=lang,
        media_type=data.get("media_type", "vod"),
        twitch_id=data.get("twitch_id", ""),
        sections=tuple(sections) if sections else None,
        title=data.get("title", ""),
    ))


# ====================== Основной download-флоу ======================

async def _process_download(
    message: Message,
    url: str,
    format_key: str,
    quality: QualityOption | None,
    user,
    lang: str,
    media_type: str = "vod",
    twitch_id: str = "",
    sections: tuple[int, int] | None = None,
    title: str = "",
) -> None:
    """Качает и отправляет файл. Для sections=None — проверяет кэш."""

    # обеспечиваем запись юзера
    async with async_session() as session:
        await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )

        # кэш только для «полного» файла без обрезки
        if sections is None:
            cached = await get_cached_twitch_download(session, url, format_key)
            if cached:
                logger.info("Кэш найден: %s [%s]", url, format_key)
                try:
                    await _send_cached(message, cached.file_id, cached.tg_media_type)
                    return
                except Exception as e:
                    logger.warning("Кэш устарел, скачиваем заново: %s", e)

    semaphore = get_semaphore()
    async with semaphore:
        status_msg = await _safe_edit_or_answer(message, t("twitch_downloading", lang))

        loop = asyncio.get_event_loop()

        def on_progress(dl_mb: float, total_mb: float, percent: int) -> None:
            # хук вызывается из executor-треда yt-dlp; троттлинг (3 сек) уже сделан внутри _download
            text = _progress_bar(percent, dl_mb, total_mb)
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_safe_edit(status_msg, text))
                )
            except Exception:
                pass

        result = None
        try:
            if format_key == "audio":
                result = await downloader.download_audio(url, on_progress, sections=sections)
            else:
                if quality is None:
                    raise RuntimeError("quality not provided")
                result = await downloader.download_video(
                    url, quality, on_progress, sections=sections,
                )

            # реальное название видео мы знаем из get_info (хранилось в FSM),
            # downloader мог вернуть плейсхолдер вида "Twitch 123456" — перезапишем
            if title and result is not None:
                result.title = title

            # отправка
            try:
                await _safe_edit(status_msg, t("twitch_uploading", lang))
            except Exception:
                pass

            first_file_id = await _send_result(
                message, result, lang, sections=sections,
            )

            # кэшируем только одиночные файлы без обрезки
            if (
                first_file_id
                and sections is None
                and not result.was_split
            ):
                async with async_session() as session:
                    await save_twitch_download(
                        session=session,
                        twitch_url=url,
                        twitch_id=twitch_id,
                        format_key=format_key,
                        file_id=first_file_id,
                        media_type=media_type,
                        tg_media_type=result.media_type,
                        ttl_days=settings.cache_ttl_days,
                    )
                    user_obj = await get_or_create_user(
                        session=session,
                        telegram_id=user.id,
                        username=user.username,
                        full_name=user.full_name,
                    )
                    user_obj.download_count += 1
                    await session.commit()
            else:
                async with async_session() as session:
                    user_obj = await get_or_create_user(
                        session=session,
                        telegram_id=user.id,
                        username=user.username,
                        full_name=user.full_name,
                    )
                    user_obj.download_count += 1
                    await session.commit()

            try:
                await status_msg.delete()
            except Exception:
                pass

        except LiveStreamError:
            await _safe_edit(status_msg, t("twitch_live_not_supported", lang))
        except FileTooLargeError:
            await _safe_edit(status_msg, t("twitch_error_too_large", lang))
        except Exception as e:
            logger.error("Ошибка скачивания %s: %s", url, e)
            await _safe_edit(status_msg, _error_text(e, lang))
        finally:
            if result is not None:
                downloader.cleanup(result)


# ====================== sending ======================

async def _send_result(
    message: Message,
    result,
    lang: str,
    sections: tuple[int, int] | None = None,
) -> str | None:
    """Отправляет все части файла. Возвращает file_id первой части (для кэша).
    Если файл разбит — отправляет каждую часть с подписью «Часть N/M».
    """
    total = len(result.file_paths)
    first_file_id: str | None = None

    # базовая подпись
    promo = t("download.promo", lang, bot_username=settings.bot_username)
    base_caption = result.title
    if sections:
        trim_cap = t(
            "twitch_trimmed_caption", lang,
            start=seconds_to_timecode(sections[0]),
            end=seconds_to_timecode(sections[1]),
        )
        base_caption = f"{base_caption}\n{trim_cap}"

    for idx, path in enumerate(result.file_paths, start=1):
        file = FSInputFile(path)

        if total > 1:
            part_caption = t(
                "twitch_part_caption", lang,
                part=idx, total=total, title=result.title,
            )
            caption = f"{part_caption}{promo}"
        else:
            caption = f"{base_caption}{promo}"

        t_upload = time.monotonic()
        try:
            size_mb = os.path.getsize(path) / 1024 / 1024
        except OSError:
            size_mb = 0

        # ffprobe реальных метаданных файла — надёжнее чем поля yt-dlp info
        probed_w = probed_h = probed_d = None
        if result.media_type == "video":
            probed_w, probed_h, probed_d = await downloader.probe_media(path)

        try:
            if result.media_type == "video":
                sent = await message.answer_video(
                    video=file,
                    caption=caption,
                    duration=probed_d or (int(result.duration) if result.duration else None),
                    width=probed_w or result.width,
                    height=probed_h or result.height,
                    supports_streaming=True,
                )
                if first_file_id is None:
                    first_file_id = sent.video.file_id
            else:
                sent = await message.answer_audio(
                    audio=file,
                    caption=caption,
                    duration=int(result.duration) if (result.duration and total == 1) else None,
                    title=result.title,
                )
                if first_file_id is None:
                    first_file_id = sent.audio.file_id
        except Exception as e:
            logger.error("Не удалось отправить файл %s: %s", path, e)
            raise

        elapsed = time.monotonic() - t_upload
        speed = size_mb / elapsed if elapsed > 0 else 0
        logger.info(
            "[METRIC] twitch.upload %.2fs part=%d/%d size=%.1fMB speed=%.1fMB/s",
            elapsed, idx, total, size_mb, speed,
        )

    return first_file_id


async def _send_cached(message: Message, file_id: str, tg_media_type: str) -> None:
    if tg_media_type == "video":
        await message.answer_video(video=file_id)
    elif tg_media_type == "audio":
        await message.answer_audio(audio=file_id)
    else:
        await message.answer_document(document=file_id)


# ====================== helpers ======================

def _quality_to_dict(q: QualityOption) -> dict:
    return {
        "height": q.height,
        "fps": q.fps,
        "format_id": q.format_id,
        "size_bytes": q.size_bytes,
        "will_split": q.will_split,
        "label": q.label,
    }


def _quality_from_dict(d: dict) -> QualityOption:
    return QualityOption(
        height=int(d.get("height") or 0),
        fps=int(d.get("fps") or 30),
        format_id=str(d.get("format_id") or ""),
        size_bytes=int(d.get("size_bytes") or 0),
        will_split=bool(d.get("will_split")),
        label=str(d.get("label") or f"{d.get('height')}p"),
    )


def _progress_bar(percent: int, dl_mb: float, total_mb: float) -> str:
    filled = int(percent / 100 * 12)
    bar = "▰" * filled + "▱" * (12 - filled)
    text = f"{E['clock']} Скачиваю...\n{bar} {percent}%"
    # размер может быть 0 если downloader его не отдаёт (TwitchDownloaderCLI)
    if total_mb > 0:
        text += f"\n{dl_mb:.0f} МБ из {total_mb:.0f} МБ"
    elif dl_mb > 0:
        text += f"\n{dl_mb:.0f} МБ"
    return text


def _format_duration(seconds: int) -> str:
    if not seconds:
        return "—"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _error_text(exc: Exception, lang: str) -> str:
    cat = classify_error(exc)
    if cat == "live":
        return t("twitch_live_not_supported", lang)
    if cat == "private":
        return t("twitch_error_private", lang)
    if cat == "unavailable":
        return t("twitch_error_unavailable", lang)
    if cat == "network":
        return t("twitch_error_network", lang)
    return t("twitch_error_unknown", lang)


async def _safe_edit(msg: Message, text: str, **kwargs) -> None:
    try:
        await msg.edit_text(text, parse_mode="HTML", **kwargs)
    except Exception:
        pass


async def _send_or_edit(msg: Message, text: str, reply_markup=None) -> None:
    """Пробуем edit, если не выходит — answer."""
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        try:
            await msg.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass


async def _safe_edit_or_answer(message: Message, text: str) -> Message:
    """Пробует edit_text, при неудаче — answer. Возвращает актуальный Message."""
    try:
        return await message.edit_text(text, parse_mode="HTML")
    except Exception:
        return await message.answer(text, parse_mode="HTML")


# ====================== fallback alerts ======================

_bot_ref = None
_FALLBACK_ALERT_THROTTLE = 600
_last_fallback_alert: dict[str, float] = {}
_SILENT_CATEGORIES = {"unavailable", "private", "live"}


def setup_fallback_alerts(bot) -> None:
    """Подключает callback алертов админу. Вызывается из main.py."""
    global _bot_ref
    _bot_ref = bot
    downloader.on_source_failed = _on_source_failed
    logger.info("Twitch: алерты о падении источников подключены")


def _on_source_failed(source: str, error: str) -> None:
    if _bot_ref is None:
        return
    try:
        asyncio.create_task(_send_fallback_alert(source, error))
    except RuntimeError:
        pass


async def _send_fallback_alert(source: str, error: str) -> None:
    category = classify_error(error)
    if category in _SILENT_CATEGORIES:
        return

    now = time.time()
    key = f"{source}:{category}"
    last = _last_fallback_alert.get(key, 0)
    if now - last < _FALLBACK_ALERT_THROTTLE:
        return
    _last_fallback_alert[key] = now

    short = error[:300] + "..." if len(error) > 300 else error
    text = (
        f"{E['warning']} <b>Twitch: источник упал</b>\n\n"
        f"<b>Источник:</b> {source}\n"
        f"<b>Категория:</b> {category}\n"
        f"<b>Ошибка:</b> <code>{short}</code>"
    )

    for admin_id in settings.admin_id_list:
        try:
            await _bot_ref.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)
