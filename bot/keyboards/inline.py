"""Inline-клавиатуры — меню, подписка, формат, качество"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings
from bot.emojis import E_ID
from bot.i18n import t


def get_start_keyboard(user_id: int, lang: str = "ru") -> InlineKeyboardMarkup:
    """Главное меню бота"""
    buttons = [
        [InlineKeyboardButton(
            text=t("btn.download", lang),
            callback_data="download_video",
            style="primary",
            icon_custom_emoji_id=E_ID["download"],
        )],
        [
            InlineKeyboardButton(
                text=t("btn.profile", lang),
                callback_data="my_profile",
                style="success",
                icon_custom_emoji_id=E_ID["profile"],
            ),
            InlineKeyboardButton(
                text=t("btn.help", lang),
                callback_data="my_help",
                style="success",
                icon_custom_emoji_id=E_ID["info"],
            ),
        ],
        [InlineKeyboardButton(
            text=t("btn.language", lang),
            callback_data="change_language",
            style="success",
            icon_custom_emoji_id=E_ID["language"],
        )],
    ]

    if user_id in settings.admin_id_list:
        buttons.append([InlineKeyboardButton(
            text=t("btn.admin_panel", lang),
            callback_data="admin_panel",
            style="danger",
            icon_custom_emoji_id=E_ID["lock"],
        )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t("btn.back", lang),
            callback_data="back_to_menu",
            style="success",
            icon_custom_emoji_id=E_ID["back"],
        )],
    ])


def get_format_keyboard(lang: str = "ru", with_trim: bool = True) -> InlineKeyboardMarkup:
    """Выбор формата: видео или аудио + опциональная кнопка обрезки."""
    rows = [[
        InlineKeyboardButton(
            text=t("btn.format_video", lang),
            callback_data="fmt_video",
            style="primary",
            icon_custom_emoji_id=E_ID["video"],
        ),
        InlineKeyboardButton(
            text=t("btn.format_audio", lang),
            callback_data="fmt_audio",
            style="primary",
            icon_custom_emoji_id=E_ID["download"],
        ),
    ]]
    if with_trim:
        rows.append([InlineKeyboardButton(
            text=t("twitch_trim_button", lang),
            callback_data="twitch_trim",
            style="success",
            icon_custom_emoji_id=E_ID["edit"],
        )])
    rows.append([InlineKeyboardButton(
        text=t("btn.back", lang),
        callback_data="back_to_menu",
        style="danger",
        icon_custom_emoji_id=E_ID["back"],
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_size_label(size_bytes: int) -> str:
    """Форматирует байты в красивый размер: 850 MB / 3.2 GB."""
    if size_bytes <= 0:
        return "—"
    mb = size_bytes / 1024 / 1024
    if mb < 1024:
        return f"{int(mb)} MB"
    gb = mb / 1024
    return f"{gb:.1f} GB"


def get_quality_keyboard(
    qualities: list | None = None, lang: str = "ru",
) -> InlineKeyboardMarkup:
    """Клавиатура качеств на базе списка QualityOption.
    Формат кнопки: «720p60 • 850 MB» или «1080p60 • 3.2 GB • будет разбито»
    callback_data: quality_<index> — index в списке qualities.
    """
    rows = []
    if qualities:
        split_label = t("twitch_quality_will_split", lang)
        for idx, q in enumerate(qualities):
            if q.size_bytes > 0:
                text = f"{q.label} • {_format_size_label(q.size_bytes)}"
            else:
                text = q.label
            if q.will_split:
                text += f" • {split_label}"
            rows.append([InlineKeyboardButton(
                text=text,
                callback_data=f"quality_{idx}",
                style="primary",
                icon_custom_emoji_id=E_ID["camera"],
            )])
    else:
        # дефолт если пусто
        rows.append([InlineKeyboardButton(
            text="720p",
            callback_data="quality_0",
            style="primary",
            icon_custom_emoji_id=E_ID["camera"],
        )])

    rows.append([InlineKeyboardButton(
        text=t("btn.back", lang),
        callback_data="back_to_menu",
        style="danger",
        icon_custom_emoji_id=E_ID["back"],
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_subscription_keyboard(
    channels: list[dict], lang: str = "ru"
) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=f"{ch['title']}",
            url=ch["invite_link"],
            style="primary",
            icon_custom_emoji_id=E_ID["megaphone"],
        )])
    buttons.append([InlineKeyboardButton(
        text=t("btn.check_sub", lang),
        callback_data="check_subscription",
        style="success",
        icon_custom_emoji_id=E_ID["check"],
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Русский",
                callback_data="set_lang_ru",
                style="primary",
                icon_custom_emoji_id=E_ID["flag_ru"],
            ),
            InlineKeyboardButton(
                text="O'zbek",
                callback_data="set_lang_uz",
                style="primary",
                icon_custom_emoji_id=E_ID["flag_uz"],
            ),
            InlineKeyboardButton(
                text="English",
                callback_data="set_lang_en",
                style="primary",
                icon_custom_emoji_id=E_ID["flag_gb"],
            ),
        ],
    ])
