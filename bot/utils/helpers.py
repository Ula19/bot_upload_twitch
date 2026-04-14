"""Вспомогательные функции — проверка URL и т.д."""
import re


_TWITCH_VOD_RE = re.compile(
    r"^https?://(?:www\.|m\.)?twitch\.tv/videos/(\d+)",
    re.IGNORECASE,
)
_TWITCH_CLIP_LONG_RE = re.compile(
    r"^https?://(?:www\.|m\.)?twitch\.tv/[^/]+/clip/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_TWITCH_CLIP_SHORT_RE = re.compile(
    r"^https?://clips\.twitch\.tv/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
# старый формат VOD: twitch.tv/<channel>/v/<id>
_TWITCH_VOD_LEGACY_RE = re.compile(
    r"^https?://(?:www\.|m\.)?twitch\.tv/[^/]+/v/(\d+)",
    re.IGNORECASE,
)


def is_twitch_url(text: str) -> bool:
    """Проверяет, является ли строка ссылкой Twitch (VOD/clip/highlight)."""
    if not text:
        return False
    text = text.strip()
    return bool(
        _TWITCH_VOD_RE.match(text)
        or _TWITCH_CLIP_LONG_RE.match(text)
        or _TWITCH_CLIP_SHORT_RE.match(text)
        or _TWITCH_VOD_LEGACY_RE.match(text)
    )


def extract_twitch_id(url: str) -> str | None:
    """Извлекает идентификатор VOD или slug клипа из URL."""
    if not url:
        return None
    url = url.strip()
    for rx in (_TWITCH_VOD_RE, _TWITCH_CLIP_LONG_RE, _TWITCH_CLIP_SHORT_RE, _TWITCH_VOD_LEGACY_RE):
        m = rx.match(url)
        if m:
            return m.group(1)
    return None


def clean_twitch_url(url: str) -> str:
    """Удаляет query/fragment, оставляет чистый путь."""
    if not url:
        return url
    url = url.strip()
    for ch in ("?", "#"):
        pos = url.find(ch)
        if pos != -1:
            url = url[:pos]
    return url


# === таймкоды для обрезки ===

_TC_RE = re.compile(r"^\s*(\d{1,2}):([0-5]?\d)(?::([0-5]?\d))?\s*$")


def parse_timecode(tc: str) -> int | None:
    """Парсит HH:MM:SS или MM:SS в секунды. None если не распознано."""
    if not tc:
        return None
    m = _TC_RE.match(tc)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:
        # MM:SS
        return int(a) * 60 + int(b)
    # HH:MM:SS
    return int(a) * 3600 + int(b) * 60 + int(c)


def parse_timecodes_pair(text: str) -> tuple[int, int] | None:
    """Парсит строку «HH:MM:SS HH:MM:SS» или «MM:SS MM:SS» → (start, end) в секундах.
    Возвращает None при ошибке.
    """
    if not text:
        return None
    parts = text.replace("\n", " ").replace("\t", " ").split()
    if len(parts) < 2:
        return None
    start = parse_timecode(parts[0])
    end = parse_timecode(parts[1])
    if start is None or end is None:
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def seconds_to_timecode(sec: int) -> str:
    """Секунды → HH:MM:SS"""
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"
