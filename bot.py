#!/usr/bin/env python3
"""
Новостной бот для @CambodlaNews и чата @Cambodia_Russ.

Что делает за один запуск:
  1. Собирает свежие новости (вчера + сегодня по времени Пномпеня) из Telegram-каналов
     (@khmertimesbreakingnews, @thephnompenhpost) и RSS камбоджийских СМИ.
  2. Убирает дубли и то, что уже публиковалось.
  3. Отбирает самые интересные (позитив + криминал, скам-центры, ДТП и т.д.) и пишет
     посты на русском: цепляющий заголовок, эмодзи, краткая суть, хэштеги.
     Делает это бесплатный Google Gemini; если он недоступен — запасной режим
     (отбор по ключевым словам + машинный перевод).
  4. Публикует в канал с фото; если в новости есть видео — добавляет ссылку на него.
  5. Пересылает самую интересную новость в чат и закрепляет её, снимая и удаляя
     прошлый закреп бота (ваши собственные закрепы не трогает).

Запуск:
  python bot.py                 # обычный запуск: собрать и опубликовать
  python bot.py --dry-run       # показать посты в терминале, ничего не отправлять
  python bot.py --count 2       # сколько новостей опубликовать (по умолчанию 3)
  python bot.py --no-pin        # не обновлять закреп в чате
  python bot.py --check         # проверить источники, Gemini и права бота
  python bot.py --set-description   # записать описание канала (био)
  python bot.py --wait-slot     # (для расписания) подготовить посты и дождаться 8:00/14:00/20:00
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------------------
# Настройки (можно менять через переменные окружения или файл .env рядом с bot.py)
# ----------------------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


TZ = ZoneInfo("Asia/Phnom_Penh")
BOT_TOKEN = env("BOT_TOKEN")
CHANNEL = env("CHANNEL", "@CambodlaNews")          # куда публикуем новости
CHAT = env("CHAT", "@Cambodia_Russ")                # где закрепляем лучшую новость
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODELS = [m.strip() for m in env(
    "GEMINI_MODELS",
    "gemini-3.6-flash,gemini-flash-latest,gemini-3.8-flash,gemini-flash-lite-latest,gemini-3.5-flash-lite",
).split(",") if m.strip()]
# Запасной бесплатный ИИ: GitHub Models. В GitHub Actions ключ не нужен (встроенный GITHUB_TOKEN).
GITHUB_TOKEN = env("GITHUB_TOKEN")
GH_MODELS = [m.strip() for m in env(
    "GH_MODELS", "openai/gpt-4.1-mini,openai/gpt-4o-mini,openai/gpt-4.1-nano").split(",") if m.strip()]
NEWS_PER_RUN = int(env("NEWS_PER_RUN", "3"))
SLOTS = [s.strip() for s in env("SLOTS", "08:00,14:00,20:00").split(",") if s.strip()]
# Чат: раз в день пересылать туда самую интересную новость.
CHAT_ENABLED = env("CHAT_ENABLED", env("PIN_ENABLED", "1")) == "1"
# В какие выпуски по расписанию отправлять новость в чат (по умолчанию 1 раз в день — утренний)
CHAT_SLOTS = [s.strip() for s in env("CHAT_SLOTS", env("PIN_SLOTS", "08:00")).split(",") if s.strip()]
CHAT_PIN = env("CHAT_PIN", "0") == "1"              # закреплять новость в чате (нужны права админа)
PIN_DELETE_OLD = env("PIN_DELETE_OLD", "1") == "1"  # при CHAT_PIN=1: удалять прошлую закреплённую новость бота
FOOTER = env("FOOTER", "")
STATE_FILE = BASE_DIR / env("STATE_FILE", "state.json")

# Описание канала (био). Лимит Telegram — 255 символов.
CHANNEL_DESCRIPTION = env(
    "CHANNEL_DESCRIPTION",
    "🇰🇭 Камбоджа без фильтров — главные новости на русском: Пномпень, Сиемреап, "
    "Сиануквиль, Кампот. Визы и законы, цены, криминал, скам-центры, ДТП, погода. "
    "Выпуски в 8:00, 14:00 и 20:00 🕗\n💬 Чат: @Cambodia_Russ",
)

# Источники. Неработающий источник просто пропускается (см. python bot.py --check).
SOURCES = [
    {"type": "telegram", "name": "Khmer Times", "channel": "khmertimesbreakingnews"},
    {"type": "telegram", "name": "Phnom Penh Post", "channel": "thephnompenhpost"},
    {"type": "rss", "name": "Khmer Times", "url": "https://www.khmertimeskh.com/feed/"},
    {"type": "rss", "name": "CamboJA News", "url": "https://cambojanews.com/feed/"},
]

MAX_CANDIDATES = 60
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def log(*args) -> None:
    stamp = dt.datetime.now(TZ).strftime("%H:%M:%S")
    print(f"[{stamp}]", *args, flush=True)


def http_get(url: str, timeout: int = 25, **kwargs) -> requests.Response:
    response = SESSION.get(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response


# ----------------------------------------------------------------------------------------
# Модель новости и утилиты
# ----------------------------------------------------------------------------------------
@dataclass
class Item:
    source: str
    title: str
    url: str = ""          # ссылка на статью
    tg_url: str = ""       # ссылка на пост в Telegram-источнике
    published: dt.datetime | None = None
    snippet: str = ""
    image: str = ""
    video: str = ""
    body: str = ""
    id: int = 0

    def key(self) -> str:
        return norm_url(self.url or self.tg_url)

    def link(self) -> str:
        return self.url or self.tg_url


_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|ref$|amp$|mc_)")
_STOP = set("the and for with from that this into over after amid says said will have has are was were "
            "its their new more than about cambodia cambodian kingdom khmer".split())
_NON_ARTICLE_HOSTS = ("t.me", "telegram.", "github.io", "facebook.com", "fb.com", "fb.watch", "instagram.com",
                      "tiktok.com", "x.com", "twitter.com", "linktr.ee", "youtube.com", "youtu.be", "whatsapp")
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "fb.watch", "facebook.com/watch", "/videos/", "vimeo.com", "tiktok.com")


def first(*candidates):
    """Первый найденный элемент. Нельзя писать `a or b`: пустой тег (<meta>, <img>) считается ложным."""
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def norm_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    p = urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)])
    return urlunparse(("https", host, p.path.rstrip("/"), "", query, ""))


def abs_url(url: str, base: str = "") -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url) if base else url


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    value = value.strip()
    parsed = None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except Exception:
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(TZ)


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    text = BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"The post .{0,300}? appeared first on .{0,100}?\.?$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def bg_url(style: str) -> str:
    match = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style or "")
    return abs_url(match.group(1)) if match else ""


def video_watch_url(url: str) -> str:
    url = abs_url(url)
    m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{6,})", url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return url


def is_video_link(url: str) -> bool:
    return any(h in url for h in _VIDEO_HOSTS)


def is_article_link(url: str) -> bool:
    if not url.startswith("http"):
        return False
    host = urlparse(url).netloc.lower()
    return not any(h in host for h in _NON_ARTICLE_HOSTS)


def shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos > limit * 0.5:
            return cut[: pos + 1]
    return cut.rsplit(" ", 1)[0] + "…"


# ----------------------------------------------------------------------------------------
# Сбор новостей
# ----------------------------------------------------------------------------------------
def fetch_telegram(src: dict, since: dt.datetime) -> list[Item]:
    """Публичная веб-версия канала t.me/s/<канал> — без аккаунта и API-ключей."""
    channel = src["channel"]
    items: list[Item] = []
    before = None
    for _ in range(int(src.get("pages", 5))):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        soup = BeautifulSoup(http_get(url).text, "html.parser")
        messages = soup.select("div.tgme_widget_message[data-post]")
        if not messages:
            break
        min_id, min_time = None, None
        for msg in messages:
            post = msg.get("data-post", "")
            try:
                msg_id = int(post.rsplit("/", 1)[1])
            except (IndexError, ValueError):
                continue
            min_id = msg_id if min_id is None else min(min_id, msg_id)
            time_el = first(msg.select_one("a.tgme_widget_message_date time[datetime]"),
                            msg.select_one("time[datetime]"))
            when = parse_date(time_el.get("datetime")) if time_el is not None else None
            if when and (min_time is None or when < min_time):
                min_time = when
            if when is None or when < since:
                continue
            item = parse_tg_message(msg, src, post, when)
            if item:
                items.append(item)
        if min_id is None or min_id <= 1 or (min_time and min_time < since):
            break
        before = min_id
    return items


def parse_tg_message(msg, src: dict, post: str, when: dt.datetime) -> Item | None:
    text_el = msg.select_one(".tgme_widget_message_text")
    lines: list[str] = []
    links: list[str] = []
    if text_el is not None:
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        links = [a.get("href", "") for a in text_el.select("a[href]")]
        raw = text_el.get_text("")
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
        lines = [ln for ln in lines if ln]

    preview = msg.select_one("a.tgme_widget_message_link_preview")
    preview_url = preview.get("href", "") if preview is not None else ""
    preview_title = preview_desc = preview_img = ""
    if preview is not None:
        t = preview.select_one(".link_preview_title")
        d = preview.select_one(".link_preview_description")
        i = preview.select_one(".link_preview_image, .link_preview_right_image")
        preview_title = t.get_text(" ", strip=True) if t is not None else ""
        preview_desc = d.get_text(" ", strip=True) if d is not None else ""
        preview_img = bg_url(i.get("style", "")) if i is not None else ""

    # убираем служебные строки ("Read more:", "Follow us:", голые ссылки)
    junk = re.compile(r"^(read more|follow us|subscribe|join us|more:|source:)|^https?://\S+$", re.I)
    content = [ln for ln in lines if not junk.search(ln)]

    title = content[0] if content else ""
    if len(title) < 15 and preview_title:
        title = preview_title
    if not title:
        return None
    snippet = " ".join(content[1:]) or preview_desc

    article = ""
    video = ""
    for link in ([preview_url] if preview_url else []) + links:
        if not article and is_article_link(link):
            article = link
        if not video and is_video_link(link):
            video = link

    photo_el = msg.select_one(".tgme_widget_message_photo_wrap")
    image = bg_url(photo_el.get("style", "")) if photo_el is not None else ""
    tg_url = f"https://t.me/{post}"
    if msg.select_one(".tgme_widget_message_video_player, video, .tgme_widget_message_roundvideo_player") is not None:
        video = video or tg_url  # видео смотрится прямо в посте источника
        if not image:
            thumb = msg.select_one(".tgme_widget_message_video_thumb")
            image = bg_url(thumb.get("style", "")) if thumb is not None else ""
    image = image or preview_img

    return Item(source=src["name"], title=shorten(title, 220), url=article, tg_url=tg_url, published=when,
                snippet=shorten(snippet, 700), image=image, video=video_watch_url(video) if video else "")


_NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}


def _txt(el, path: str) -> str:
    found = el.find(path, _NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def _first_img(fragment: str, base: str) -> str:
    if not fragment or "<img" not in fragment:
        return ""
    img = BeautifulSoup(fragment, "html.parser").find("img")
    if img is None:
        return ""
    return abs_url(img.get("data-src") or img.get("src") or "", base)


def _first_video(fragment: str) -> str:
    if not fragment:
        return ""
    m = re.search(r"""<iframe[^>]+src=["']([^"']*(?:youtube|youtu\.be|vimeo|facebook\.com/plugins/video)[^"']*)""",
                  fragment, re.I)
    return video_watch_url(m.group(1)) if m else ""


def fetch_rss(src: dict, since: dt.datetime) -> list[Item]:
    raw = http_get(src["url"]).content.lstrip(b"\xef\xbb\xbf \r\n\t")
    root = ET.fromstring(raw)
    items: list[Item] = []
    entries = list(root.iter("item"))
    is_atom = not entries
    if is_atom:
        entries = list(root.iter(f"{{{_NS['atom']}}}entry"))
    for e in entries:
        if is_atom:
            title = _txt(e, "atom:title")
            link_el = first(e.find("atom:link[@rel='alternate']", _NS), e.find("atom:link", _NS))
            link = link_el.get("href", "") if link_el is not None else ""
            published = parse_date(_txt(e, "atom:published") or _txt(e, "atom:updated"))
            desc_html = _txt(e, "atom:summary")
            content_html = _txt(e, "atom:content")
        else:
            title = _txt(e, "title")
            link = _txt(e, "link")
            published = parse_date(_txt(e, "pubDate") or _txt(e, "dc:date"))
            desc_html = _txt(e, "description")
            content_html = _txt(e, "content:encoded")
        if not title or not link:
            continue
        if published is None or published < since:
            continue

        image = ""
        for mc in e.findall("media:content", _NS) + e.findall("media:thumbnail", _NS):
            url = mc.get("url", "")
            if url and (mc.get("medium") in (None, "image") or re.search(r"\.(jpe?g|png|webp)", url, re.I)):
                image = url
                break
        if not image:
            enc = e.find("enclosure")
            if enc is not None and (enc.get("type") or "").startswith("image"):
                image = enc.get("url", "")
        image = image or _first_img(content_html, link) or _first_img(desc_html, link)

        snippet = html_to_text(desc_html) or html_to_text(content_html)
        items.append(Item(source=src["name"], title=html.unescape(title), url=link, published=published,
                          snippet=shorten(snippet, 700), image=abs_url(image, link),
                          video=_first_video(content_html) or _first_video(desc_html),
                          body=shorten(html_to_text(content_html), 3000)))
    return items


def fetch_source(src: dict, since: dt.datetime) -> list[Item]:
    if src["type"] == "telegram":
        return fetch_telegram(src, since)
    if src["type"] == "rss":
        return fetch_rss(src, since)
    raise ValueError(f"неизвестный тип источника: {src['type']}")


def _merge(into: Item, other: Item) -> None:
    for field_name in ("url", "tg_url", "image", "video", "body"):
        if not getattr(into, field_name) and getattr(other, field_name):
            setattr(into, field_name, getattr(other, field_name))
    if len(other.snippet) > len(into.snippet):
        into.snippet = other.snippet


def collect(since: dt.datetime) -> tuple[list[Item], list[tuple[str, int, str]]]:
    raw: list[Item] = []
    report = []
    for src in SOURCES:
        label = f"{src['name']} ({src['type']})"
        try:
            found = fetch_source(src, since)
            raw.extend(found)
            report.append((label, len(found), ""))
        except Exception as exc:  # источник недоступен — не страшно
            report.append((label, 0, f"{type(exc).__name__}: {str(exc)[:120]}"))

    oldest = dt.datetime(2000, 1, 1, tzinfo=TZ)
    raw.sort(key=lambda it: it.published or oldest, reverse=True)
    unique: list[Item] = []
    for item in raw:
        twin = next((u for u in unique if (item.key() and item.key() == u.key())
                     or (item.url and norm_url(item.url) == norm_url(u.url))
                     or similar(item.title, u.title)), None)
        if twin:
            _merge(twin, item)
        else:
            unique.append(item)
    return unique, report


def enrich(item: Item) -> None:
    """Открывает статью: берёт фото (og:image), видео и полный текст для точного пересказа."""
    if not item.url:
        return
    try:
        response = http_get(item.url, timeout=20)
    except Exception as exc:
        log(f"  статья недоступна ({type(exc).__name__}): {item.url}")
        return
    soup = BeautifulSoup(response.text, "html.parser")

    def meta(*names: str) -> str:
        for name in names:
            tag = first(soup.find("meta", attrs={"property": name}), soup.find("meta", attrs={"name": name}))
            if tag is not None and tag.get("content"):
                return tag["content"].strip()
        return ""

    og_image = meta("og:image:secure_url", "og:image", "twitter:image")
    if og_image and not re.search(r"logo|default|placeholder", og_image, re.I):
        item.image = item.image or abs_url(og_image, item.url)

    if not item.video:
        video = meta("og:video:secure_url", "og:video:url", "og:video")
        if not video:
            iframe = soup.find("iframe", src=re.compile(r"youtube|youtu\.be|vimeo|facebook\.com/plugins/video"))
            if iframe is not None:
                video = iframe.get("src", "")
        if not video:
            tag = soup.find("video")
            if tag is not None:
                source_tag = tag.find("source")
                video = tag.get("src") or (source_tag.get("src", "") if source_tag is not None else "")
        if video:
            item.video = video_watch_url(abs_url(video, item.url))

    if not item.published:
        item.published = parse_date(meta("article:published_time"))

    container = first(soup.select_one("article .entry-content, .entry-content, .td-post-content, .article-content, "
                                      ".post-content, .field--name-body, article"), soup.body, soup)
    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    text = " ".join(p for p in paragraphs if len(p) > 40)
    if len(text) > len(item.body):
        item.body = shorten(text, 3000)


# ----------------------------------------------------------------------------------------
# Отбор и написание постов: Gemini (бесплатно) + запасной режим без ИИ
# ----------------------------------------------------------------------------------------
class AIError(Exception):
    pass


_working_model: str | None = None
_provider = ""


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def gemini_json(prompt: str, temperature: float = 0.7) -> dict:
    global _working_model
    if not GEMINI_API_KEY:
        raise AIError("нет GEMINI_API_KEY")
    models = ([_working_model] if _working_model else []) + [m for m in GEMINI_MODELS if m != _working_model]
    errors = []
    for model in models:
        for attempt in range(2):
            try:
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": temperature,
                                               "responseMimeType": "application/json"}},
                    timeout=120,
                )
            except requests.RequestException as exc:
                errors.append(f"{model}: {type(exc).__name__}")
                break
            if response.status_code == 429 and attempt == 0:
                time.sleep(8)
                continue
            if response.status_code != 200:
                errors.append(f"{model}: HTTP {response.status_code} {response.text[:150]}")
                break
            try:
                data = response.json()
                parts = data["candidates"][0]["content"]["parts"]
                result = _parse_json("".join(p.get("text", "") for p in parts if not p.get("thought")))
                _working_model = f"Gemini {model}"
                return result
            except Exception as exc:
                errors.append(f"{model}: неверный ответ ({type(exc).__name__})")
                break
    raise AIError("; ".join(errors)[:600])


def github_json(prompt: str, temperature: float = 0.7) -> dict:
    """Запасной ИИ: GitHub Models (бесплатно ~150 запросов в день, в Actions без отдельного ключа)."""
    global _working_model
    if not GITHUB_TOKEN:
        raise AIError("нет GITHUB_TOKEN")
    errors = []
    for model in GH_MODELS:
        for attempt in range(2):
            try:
                response = requests.post(
                    "https://models.github.ai/inference/chat/completions",
                    headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json",
                             "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                    json={"model": model, "temperature": temperature,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=120,
                )
            except requests.RequestException as exc:
                errors.append(f"{model}: {type(exc).__name__}")
                break
            if response.status_code == 429 and attempt == 0:
                time.sleep(20)
                continue
            if response.status_code != 200:
                errors.append(f"{model}: HTTP {response.status_code} {response.text[:150]}")
                break
            try:
                result = _parse_json(response.json()["choices"][0]["message"]["content"])
                _working_model = f"GitHub Models {model}"
                return result
            except Exception as exc:
                errors.append(f"{model}: неверный ответ ({type(exc).__name__})")
                break
    raise AIError("; ".join(errors)[:600])


def ai_json(prompt: str, temperature: float = 0.7, compact_prompt: str | None = None) -> dict:
    """Сначала Gemini, при ошибке — GitHub Models. Если оба недоступны — AIError (дальше запасной режим без ИИ)."""
    errors = []
    try:
        return gemini_json(prompt, temperature)
    except AIError as exc:
        errors.append(f"Gemini: {exc}")
    try:
        return github_json(compact_prompt or prompt, temperature)
    except AIError as exc:
        errors.append(f"GitHub Models: {exc}")
    raise AIError(" | ".join(errors)[:900])


SELECT_PROMPT = """You are the chief editor of a Russian-language Telegram news channel about Cambodia (@CambodlaNews).
Audience: Russian-speaking expats, tourists, business people and investors in Cambodia.

From the candidates below choose up to {n} items for the next batch of posts and pick ONE most catchy item to pin in the community chat.

Rules:
- Only news about Cambodia or directly affecting people in Cambodia (border with Thailand/Vietnam, flights to/from Cambodia, visas, foreigners in Cambodia). Skip purely international news.
- The channel is NOT a government PR feed. Balance the batch: if available, include at least one "hard" story — crime, shootings, arrests, police raids, fraud, online scams and scam compounds, robberies, murders, drugs, accidents, fires, floods, deaths, corruption, deportations of foreigners, court verdicts.
- Also valuable: practical news (visas, new laws, fines, taxes, prices, fuel, traffic rules, flights, airports, holidays, weather, real estate, banks, exchange rate, events in Phnom Penh/Siem Reap/Sihanoukville).
- Avoid boring protocol news (courtesy calls, meetings, MoU signings, congratulation letters, "minister attends ceremony") unless it has a real practical impact.
- Never choose two items about the same story. Prefer items with has_image=true and fresher items.
- Do NOT choose stories that repeat these recently published ones:
{recent}

Return JSON only: {{"selected": [ids in publishing order, best first], "pin": id}}

Candidates (JSON):
{candidates}
"""

WRITE_PROMPT = """You write posts for a Russian-language Telegram news channel about Cambodia (@CambodlaNews).
For EACH item below write one post in Russian.

Fields:
- "emoji": 1-2 emoji that exactly match the topic. Examples: 🔫 shooting/guns, 🚔 or 👮 arrest/police raid, ⛓️ detention/prison,
  ⚖️ court/verdict, 🕵️ or 💻 scam centres/online fraud, 💸 money/fraud, 🦹 robbery/theft, 🔪 stabbing/murder, 💊 drugs,
  🚗💥 road accident, 🏍️ motorbike, 🔥 fire, 🌊 flood, 🌧️ weather/storm, ✈️ flights/airport, 🛂 visas/immigration,
  🚫 ban, 📜 new law/rule, 💰 economy/prices, 🏗️ construction, 🏝️ tourism/beaches, 🛕 temples/Angkor, 🎉 holiday/event,
  🇹🇭 Thailand, 🇻🇳 Vietnam, 🇨🇳 China, 🇷🇺 Russia, ⚠️ warning, ❗ urgent.
- "title": a hook headline in Russian, 45-90 characters: intriguing and emotional (clickbait style) but strictly truthful,
  no invented facts, no emoji, no quotes around it, no ALL CAPS.
- "text": 2-4 short sentences, 250-550 characters: who, what, where, when, what's next; if relevant, why it matters
  for foreigners in Cambodia. Use ONLY facts from the item. Natural, lively Russian. Russian spellings of places:
  Пномпень, Сиемреап, Сиануквиль, Кампот, Кеп, Баттамбанг, Пойпет, Кох Конг, Кампонгсом, Такео, Свайриенг.
  Keep money amounts as in the source ($ or риелей). Do not add the source name or links.
- "hashtags": 2-3 Russian hashtags, no spaces inside (e.g. "#криминал", "#Пномпень", "#визы", "#скам").

Return JSON only: {{"posts": [{{"id": <id>, "emoji": "...", "title": "...", "text": "...", "hashtags": ["#..."]}}]}}

Items (JSON):
{items}
"""


def _candidate_view(item: Item, snippet_len: int = 300) -> dict:
    return {
        "id": item.id,
        "source": item.source,
        "time": item.published.strftime("%d.%m %H:%M") if item.published else "",
        "title": item.title,
        "snippet": shorten(item.snippet or item.body, snippet_len),
        "has_image": bool(item.image),
        "has_video": bool(item.video),
    }


def ai_select(candidates: list[Item], n: int, recent_titles: list[str]) -> tuple[list[Item], Item | None]:
    recent = "\n".join(f"- {t}" for t in recent_titles[-40:]) or "- (nothing yet)"
    prompt = SELECT_PROMPT.format(
        n=n, recent=recent,
        candidates=json.dumps([_candidate_view(c) for c in candidates], ensure_ascii=False),
    )
    # у GitHub Models лимит ~8000 токенов на запрос — для него список короче
    compact = SELECT_PROMPT.format(
        n=n, recent="\n".join(f"- {t}" for t in recent_titles[-15:]) or "- (nothing yet)",
        candidates=json.dumps([_candidate_view(c, 150) for c in candidates[:35]], ensure_ascii=False),
    )
    result = ai_json(prompt, temperature=0.3, compact_prompt=compact)
    by_id = {c.id: c for c in candidates}
    chosen = []
    for raw_id in result.get("selected", []):
        try:
            item = by_id.get(int(raw_id))
        except (TypeError, ValueError):
            continue
        if item and item not in chosen:
            chosen.append(item)
    if not chosen:
        raise AIError("ИИ не выбрал ни одной новости")
    try:
        pin = by_id.get(int(result.get("pin")))
    except (TypeError, ValueError):
        pin = None
    return chosen[:n], (pin if pin in chosen else chosen[0])


def ai_write(items: list[Item]) -> dict[int, dict]:
    def build(limit: int) -> str:
        payload = [{"id": it.id, "source": it.source, "title": it.title,
                    "text": shorten(it.body or it.snippet, limit) or it.title} for it in items]
        return WRITE_PROMPT.format(items=json.dumps(payload, ensure_ascii=False))
    result = ai_json(build(2500), temperature=0.8, compact_prompt=build(1500))
    posts = {}
    for post in result.get("posts", []):
        try:
            posts[int(post["id"])] = post
        except (KeyError, TypeError, ValueError):
            continue
    return posts


# --- запасной режим без ИИ ---
_RULES = [  # (регулярка, эмодзи, хэштег, это "жёсткая" новость?)
    (r"\b(shoot\w*|shot|guns?|gunman|gunmen|firearms?|pistols?|opened fire)\b", "🔫", "#криминал", True),
    (r"\b(scam\w*|fraud\w*|swindl\w*|cybercrime|online gambling|compounds?)\b", "🕵️", "#скам", True),
    (r"\b(murder\w*|kill\w*|stabb?\w*|slain|body found|dead body)\b", "🔪", "#криминал", True),
    (r"\b(rob|robs|robbed|robber\w*|theft|thie\w+|stole|stolen|snatch\w*|burglar\w*)\b", "🦹", "#криминал", True),
    (r"\b(drugs?|meth\w*|narcotic\w*|heroin|cocaine|ketamine)\b", "💊", "#наркотики", True),
    (r"\b(arrest\w*|detain\w*|nabbed|apprehend\w*|raids?|raided|custody|charged)\b", "🚔", "#арест", True),
    (r"\b(courts?|sentenc\w*|verdict|jail\w*|prison\w*|convict\w*)\b", "⚖️", "#суд", True),
    (r"\b(deport\w*|immigration|visas?|e-?visa|passports?|e-?arrival)\b", "🛂", "#визы", False),
    (r"\b(crash\w*|collision|accidents?|overturn\w*)\b", "🚗💥", "#ДТП", True),
    (r"\b(blaze|caught fire|fire broke|gutted|engulf\w*|firefighters?|burn(ed|t)? down)\b", "🔥", "#пожар", True),
    (r"\b(floods?|flooding|storms?|rains?|rainfall|weather|lightning|drought|heatwave)\b", "🌧️", "#погода", False),
    (r"\b(flights?|airports?|airlines?|techo)\b", "✈️", "#авиа", False),
    (r"\b(tourism|tourists?|angkor|beach\w*|resorts?)\b", "🏝️", "#туризм", False),
    (r"\b(prices?|fuel|petrol|inflation|riel|dollars?|banks?|tax\w*|economy|exports?|investment)\b",
     "💰", "#экономика", False),
    (r"\b(thai|thailand)\b", "🇹🇭", "#Таиланд", False),
    (r"\b(laws?|bans?|banned|regulations?|decree|fines?|fined)\b", "📜", "#законы", False),
]
_CAMBODIA = re.compile(r"cambodia|khmer|phnom penh|siem reap|sihanouk|kampot|kep|battambang|poipet|koh kong|"
                       r"kingdom|hun manet|hun sen|takeo|kandal|preah|kampong|svay rieng|ratanakiri|mondulkiri", re.I)
_BORING = re.compile(r"courtesy call|\bmou\b|memorandum|congratulat|\battends?\b|ceremony|bilateral talks|\breceives\b",
                     re.I)


def _classify(item: Item) -> tuple[str, list[str], bool]:
    text = f"{item.title} {item.snippet}".lower()
    emoji, tags, hard = "📰", ["#Камбоджа"], False
    for pattern, e, tag, is_hard in _RULES:
        if re.search(pattern, text):
            if emoji == "📰":
                emoji = e
            if tag not in tags:
                tags.append(tag)
            hard = hard or is_hard
    if "phnom penh" in text:
        tags.append("#Пномпень")
    return emoji, tags[:3], hard


def fallback_select(candidates: list[Item], n: int) -> tuple[list[Item], Item | None]:
    now = dt.datetime.now(TZ)
    scored = []
    for item in candidates:
        text = f"{item.title} {item.snippet}"
        if not _CAMBODIA.search(text):
            continue
        _, tags, hard = _classify(item)
        hours = ((now - item.published).total_seconds() / 3600) if item.published else 48
        score = 10 - min(hours, 48) / 6 + (3 if hard else 0) + (1.5 if len(tags) > 1 else 0)
        score += 1 if item.image else 0
        score -= 4 if _BORING.search(text) else 0
        scored.append((score, hard, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = [it for _, _, it in scored[:n]]
    hard_items = [it for _, hard, it in scored if hard]
    if hard_items and not any(it in hard_items for it in chosen) and chosen:
        chosen[-1] = hard_items[0]
    return chosen, (chosen[0] if chosen else None)


def gtranslate(text: str) -> str:
    if not text:
        return ""
    try:
        r = SESSION.get("https://translate.googleapis.com/translate_a/single",
                        params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text[:4500]},
                        timeout=20)
        r.raise_for_status()
        return "".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip()
    except Exception as exc:
        log(f"  перевод не удался: {type(exc).__name__}")
        return text


def fallback_write(item: Item) -> dict:
    emoji, tags, _ = _classify(item)
    return {"emoji": emoji, "title": gtranslate(item.title),
            "text": gtranslate(shorten(item.body or item.snippet, 500)), "hashtags": tags}


# ----------------------------------------------------------------------------------------
# Telegram Bot API
# ----------------------------------------------------------------------------------------
class TgError(Exception):
    pass


def tg(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    if not BOT_TOKEN:
        raise TgError("нет BOT_TOKEN")
    data = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in (data or {}).items() if v is not None}
    for attempt in range(5):
        try:
            response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                                     data=data, files=files, timeout=90)
        except requests.RequestException as exc:  # обрыв связи — пробуем ещё раз
            if attempt == 4:
                raise TgError(f"нет связи с Telegram ({type(exc).__name__})")
            time.sleep(5 * (attempt + 1))
            continue
        try:
            payload = response.json()
        except ValueError:
            raise TgError(f"HTTP {response.status_code}")
        if payload.get("ok"):
            return payload["result"]
        retry = (payload.get("parameters") or {}).get("retry_after")
        if retry:
            time.sleep(int(retry) + 1)
            continue
        raise TgError(payload.get("description", "unknown error"))
    raise TgError("слишком много запросов (rate limit)")


def visible_len(text_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text_html)).encode("utf-16-le")) // 2


def build_caption(item: Item, post: dict, limit: int = 1024) -> str:
    emoji = (post.get("emoji") or "📰").strip()
    title = re.sub(r"\s+", " ", (post.get("title") or item.title)).strip().strip('"«»')
    body = re.sub(r"\s+", " ", post.get("text") or "").strip()
    tags = [t if t.startswith("#") else f"#{t}" for t in (post.get("hashtags") or [])]
    tags = [re.sub(r"\s+", "_", t) for t in tags][:3]

    def render(text: str) -> str:
        parts = [f"{emoji} <b>{html.escape(title)}</b>"]
        if text:
            parts += ["", html.escape(text)]
        extra = []
        if item.video:
            extra.append(f'🎥 <a href="{html.escape(item.video, quote=True)}">Смотреть видео</a>')
        if extra:
            parts += [""] + extra
        tail = " ".join(tags)
        if tail or FOOTER:
            parts += ["", (tail + ("\n" if tail and FOOTER else "") + FOOTER).strip()]
        return "\n".join(parts)

    caption = render(body)
    while visible_len(caption) > limit - 10 and len(body) > 60:
        body = shorten(body, len(body) - 60)
        caption = render(body)
    return caption


def download_image(url: str):
    if not url:
        return None
    try:
        response = SESSION.get(abs_url(url), timeout=30)
        response.raise_for_status()
    except Exception:
        return None
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    size = len(response.content)
    if not content_type.startswith("image/") or size < 4000 or size > 9_500_000:
        return None
    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(content_type, "jpg")
    return (f"photo.{ext}", response.content, content_type)


def send_post(item: Item, caption: str) -> dict:
    base = {"chat_id": CHANNEL, "parse_mode": "HTML"}
    photo = download_image(item.image)
    if photo:
        try:
            return tg("sendPhoto", {**base, "caption": caption}, files={"photo": photo})
        except TgError as exc:
            log(f"  фото файлом не ушло ({exc}), пробую по ссылке")
    if item.image:
        try:
            return tg("sendPhoto", {**base, "caption": caption, "photo": abs_url(item.image)})
        except TgError as exc:
            log(f"  фото по ссылке не ушло ({exc}), отправляю текстом")
    return tg("sendMessage", {**base, "text": caption, "link_preview_options": {"is_disabled": True}})


def share_to_chat(channel_message_id: int, state: dict) -> None:
    """Пересылает новость из канала в чат (с подписью «Переслано из…»). Закреп — только если CHAT_PIN=1."""
    try:
        new_msg = tg("forwardMessage", {"chat_id": CHAT, "from_chat_id": CHANNEL,
                                        "message_id": channel_message_id})
    except TgError as exc:
        log(f"  переслать не удалось ({exc}), копирую")
        new_msg = tg("copyMessage", {"chat_id": CHAT, "from_chat_id": CHANNEL, "message_id": channel_message_id})
    new_id = new_msg["message_id"]
    log(f"💬 Новость отправлена в {CHAT} (сообщение {new_id})")
    if not CHAT_PIN:
        return
    tg("pinChatMessage", {"chat_id": CHAT, "message_id": new_id, "disable_notification": True})
    old_id = state.get("pinned_message_id")
    if old_id and old_id != new_id:
        try:
            tg("unpinChatMessage", {"chat_id": CHAT, "message_id": old_id})
        except TgError as exc:
            log(f"  старый закреп не снят: {exc}")
        if PIN_DELETE_OLD:
            try:
                tg("deleteMessage", {"chat_id": CHAT, "message_id": old_id})
            except TgError as exc:
                log(f"  старый закреп не удалён: {exc}")
    state["pinned_message_id"] = new_id
    log(f"📌 Закреплено в {CHAT}")


# ----------------------------------------------------------------------------------------
# Состояние: что уже публиковали (хранится в state.json)
# ----------------------------------------------------------------------------------------
def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    state.setdefault("posted", {})
    state.setdefault("recent", [])
    state.setdefault("pinned_message_id", None)
    return state


def save_state(state: dict) -> None:
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=10)).isoformat()
    state["posted"] = {k: v for k, v in state["posted"].items() if v >= cutoff}
    state["recent"] = state["recent"][-80:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def already_posted(item: Item, state: dict) -> bool:
    keys = {norm_url(item.url), norm_url(item.tg_url)} - {""}
    if keys & set(state["posted"]):
        return True
    return any(similar(item.title, r.get("src", "")) for r in state["recent"])


def mark_posted(item: Item, post: dict, state: dict) -> None:
    now = dt.datetime.now(TZ).isoformat(timespec="minutes")
    for key in {norm_url(item.url), norm_url(item.tg_url)} - {""}:
        state["posted"][key] = now
    state["recent"].append({"src": item.title, "ru": post.get("title", ""), "at": now})


# ----------------------------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------------------------
def since_time() -> dt.datetime:
    now = dt.datetime.now(TZ)
    return (now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def nearest_slot(max_diff_min: int = 180) -> str | None:
    """Выпуск, к которому относится текущий запуск (ближайшее время из SLOTS в пределах 3 часов)."""
    now = dt.datetime.now(TZ)
    best, best_diff = None, None
    for slot in SLOTS:
        hour, minute = map(int, slot.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        diff = abs((target - now).total_seconds()) / 60
        if diff <= max_diff_min and (best_diff is None or diff < best_diff):
            best, best_diff = slot, diff
    return best


def wait_for_slot(max_wait_min: int = 50) -> None:
    now = dt.datetime.now(TZ)
    waits = []
    for slot in SLOTS:
        hour, minute = map(int, slot.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (target - now).total_seconds()
        if 0 < delta <= max_wait_min * 60:
            waits.append(delta)
    if waits:
        delay = min(waits)
        log(f"⏳ Посты готовы, жду времени публикации: {int(delay // 60)} мин {int(delay % 60)} с")
        time.sleep(delay)


def run(args) -> int:
    state = load_state()
    slot_key = None
    if args.wait_slot:
        slot = nearest_slot()
        if slot:
            slot_key = f"{dt.datetime.now(TZ):%Y-%m-%d} {slot}"
            if slot_key in state.get("slots_done", []):
                log(f"Выпуск {slot_key} уже опубликован другим запуском — пропускаю.")
                return 0
    since = since_time()
    log(f"Собираю новости с {since:%d.%m %H:%M} (время Пномпеня)…")
    items, report = collect(since)
    for label, count, error in report:
        log(f"  {label}: {count}" + (f"  ⚠️ {error}" if error else ""))

    fresh = [it for it in items if not already_posted(it, state)][:MAX_CANDIDATES]
    log(f"Новых кандидатов: {len(fresh)} (всего свежих {len(items)})")
    if not fresh:
        log("Нечего публиковать — новых новостей нет.")
        return 0
    for index, item in enumerate(fresh, 1):
        item.id = index

    count = max(1, args.count)
    recent = [r.get("src", "") for r in state["recent"]] + [r.get("ru", "") for r in state["recent"]]
    try:
        chosen, pin_item = ai_select(fresh, count, recent)
        log(f"ИИ ({_working_model}) отобрал {len(chosen)} новостей")
    except AIError as exc:
        log(f"⚠️ ИИ недоступен ({exc}). Запасной режим без ИИ.")
        chosen, pin_item = fallback_select(fresh, count)
    if not chosen:
        log("Ничего подходящего не нашлось.")
        return 0

    for item in chosen:
        enrich(item)
    try:
        written = ai_write(chosen)
    except AIError as exc:
        log(f"⚠️ ИИ не написал посты ({exc}). Запасной перевод.")
        written = {}
    posts = []
    for item in chosen:
        post = written.get(item.id) or fallback_write(item)
        posts.append((item, post, build_caption(item, post)))

    slot = nearest_slot() if args.wait_slot else None
    pin_now = CHAT_ENABLED and not args.no_pin and bool(CHAT) and (slot is None or slot in CHAT_SLOTS)

    if args.dry_run:
        print("\n" + "=" * 70)
        for item, post, caption in posts:
            plain = html.unescape(re.sub(r"<[^>]+>", "", caption))
            mark = f"  💬 (уйдёт в чат {CHAT})" if item is pin_item and pin_now else ""
            print(plain + mark)
            print(f"\n[фото] {item.image or '— нет —'}")
            print(f"[видео] {item.video or '— нет —'}")
            print(f"[длина подписи] {visible_len(caption)} / 1024")
            print("=" * 70)
        log("Это был предпросмотр (--dry-run): ничего не отправлено.")
        return 0

    if args.wait_slot:
        wait_for_slot()

    sent: dict[int, int] = {}
    failures = 0
    for item, post, caption in posts:
        try:
            message = send_post(item, caption)
            sent[item.id] = message["message_id"]
            mark_posted(item, post, state)
            save_state(state)
            log(f"✅ Опубликовано: {post.get('title') or item.title}")
            time.sleep(3)
        except TgError as exc:
            failures += 1
            log(f"❌ Не опубликовано ({exc}): {item.title}")

    if sent and pin_now:
        pin_id = sent.get(pin_item.id) if pin_item else None
        try:
            share_to_chat(pin_id or next(iter(sent.values())), state)
        except TgError as exc:
            log(f"⚠️ В чат не отправлено: {exc}")
    state["last_run"] = dt.datetime.now(TZ).isoformat(timespec="minutes")
    if slot_key and sent:
        state["slots_done"] = (state.get("slots_done", []) + [slot_key])[-30:]
    save_state(state)
    return 1 if failures and not sent else 0


def check() -> int:
    since = since_time()
    print(f"Источники (новости с {since:%d.%m %H:%M}):")
    for src in SOURCES:
        label = f"{src['name']} ({src['type']})"
        try:
            found = fetch_source(src, since)
            sample = found[0].title[:70] if found else "—"
            photos = sum(1 for it in found if it.image)
            print(f"  ✅ {label}: {len(found)} новостей, с фото {photos}. Пример: {sample}")
        except Exception as exc:
            print(f"  ❌ {label}: {type(exc).__name__}: {str(exc)[:120]}")

    print("\nGemini:")
    try:
        gemini_json('Return JSON {"ok": true}', temperature=0)
        print(f"  ✅ работает, модель {_working_model}")
    except AIError as exc:
        print(f"  ❌ {exc}")

    print("\nGitHub Models (запасной ИИ):")
    try:
        github_json('Return JSON {"ok": true}', temperature=0)
        print(f"  ✅ работает, {_working_model}")
    except AIError as exc:
        hint = "  (на Mac это нормально: работает только в GitHub Actions)" if not GITHUB_TOKEN else ""
        print(f"  ⚠️ {exc}{hint}")

    print("\nTelegram:")
    try:
        me = tg("getMe")
        print(f"  ✅ бот @{me['username']}")
        chat_needs = ("can_pin_messages", "can_delete_messages") if CHAT_PIN else ()
        for chat, needs in ((CHANNEL, ("can_post_messages",)), (CHAT, chat_needs)):
            try:
                member = tg("getChatMember", {"chat_id": chat, "user_id": me["id"]})
                status = member.get("status")
                rights = ", ".join(f"{r}={'да' if member.get(r) else 'нет'}" for r in needs)
                ok = (status in ("administrator", "creator") and all(member.get(r) for r in needs)) or \
                     (not needs and status in ("member", "administrator", "creator", "restricted"))
                print(f"  {'✅' if ok else '⚠️'} {chat}: {status}; {rights}")
            except TgError as exc:
                print(f"  ❌ {chat}: {exc}  → добавьте бота администратором")
    except TgError as exc:
        print(f"  ❌ {exc}")
    return 0


def set_description() -> int:
    print(f"Описание ({len(CHANNEL_DESCRIPTION)}/255 символов):\n{CHANNEL_DESCRIPTION}\n")
    try:
        tg("setChatDescription", {"chat_id": CHANNEL, "description": CHANNEL_DESCRIPTION})
        print(f"✅ Описание канала {CHANNEL} обновлено")
        return 0
    except TgError as exc:
        print(f"❌ {exc}\nНужны права администратора «Изменение профиля канала» или вставьте текст вручную.")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Новостной бот @CambodlaNews")
    parser.add_argument("--dry-run", action="store_true", help="показать посты, ничего не отправлять")
    parser.add_argument("--count", type=int, default=NEWS_PER_RUN, help="сколько новостей опубликовать")
    parser.add_argument("--no-pin", "--no-chat", dest="no_pin", action="store_true",
                        help="не отправлять новость в чат")
    parser.add_argument("--wait-slot", action="store_true", help="дождаться ближайшего времени публикации")
    parser.add_argument("--check", action="store_true", help="проверить источники, Gemini и права бота")
    parser.add_argument("--set-description", action="store_true", help="записать описание канала")
    args = parser.parse_args()
    if args.check:
        return check()
    if args.set_description:
        return set_description()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
