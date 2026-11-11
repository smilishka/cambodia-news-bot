#!/usr/bin/env python3
"""
Разовое оформление канала @CambodlaNews через бота:
  1. имя бота → «КПР Новости | Камбоджа»
  2. пост №3 → вступительный пост (с новой картинкой)
  3. в каждом посте эмодзи в заголовке → по смыслу новости
  4. из каждого поста убираются строки «Источник: …»
  5. аватар канала → avatar.jpg

Запуск (из папки бота):
  .venv/bin/python makeover.py --dry-run     # показать, что изменится, ничего не меняя
  .venv/bin/python makeover.py               # применить
  .venv/bin/python makeover.py --restore     # вернуть тексты постов из резервной копии
  .venv/bin/python makeover.py --hashtags    # добавить хэштеги к постам, где их нет
Перед изменениями все исходные тексты сохраняются в makeover_backup.json.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

import bot

BASE = Path(__file__).resolve().parent
CHANNEL_USERNAME = bot.CHANNEL.lstrip("@")
BOT_NAME = "КПР Новости | Камбоджа"
INTRO_POST_ID = 3
AVATAR = BASE / "avatar.jpg"
BACKUP = BASE / "makeover_backup.json"
PROGRESS = BASE / "makeover_progress.json"   # что уже сделано — повторный запуск продолжит с места остановки

INTRO_CAPTION = """🇰🇭🔥 <b>Камбоджа, о которой не расскажут в турагентстве</b>

Всё, что происходит в Королевстве, — <b>быстро, на русском и без фильтров</b> 👇

🚨 Криминал, облавы и скам-центры
🛂 Визы, законы и новые правила для иностранцев
💸 Цены, курс, бизнес и недвижимость
✈️ Рейсы, аэропорт Течо и путешествия
🌧 Погода, ДТП и всё, что важно знать прямо сейчас

🗞 3 выпуска в день: <b>8:00 · 14:00 · 20:00</b>
📍 Пномпень · Сиемреап · Сиануквиль · Кампот

🔔 Включи уведомления — узнавай первым!
💬 Обсуждаем в чате: @Cambodia_Russ
📲 @CambodlaNews"""

EMOJI_PROMPT = """You pick emoji for headlines of a Russian-language Telegram news channel about Cambodia.
For EACH item choose 1-2 emoji that exactly match the topic of the news. Examples:
🔫 shooting, 🚔 or 👮 police/arrest/raid, ⛓️ detention/prison, ⚖️ court, 🕵️ or 💻 scam centres/online fraud,
💸 fraud/money, 🦹 robbery/theft, 🔪 murder/stabbing, 💊 drugs, 🚗💥 road accident, 🏍️ motorbike, 🔥 fire,
🌊 flood, 🌧️ weather, 💣 landmines, ✈️ flights/airport, 🛂 visas/immigration/deportation, 📜 law/rule, 🚫 ban,
💰 economy/prices/trade, 🏦 banks/currency, ⛽ fuel, 🏗️ construction/infrastructure, 🏝️ tourism/beaches,
🛕 Angkor/temples, 🏥 health/hospital, 🎓 education, 🌿 ecology, 🗳️ elections, 🏛️ government/politics,
🤝 diplomacy/cooperation, 🎉 holidays/events, 🌾 agriculture, 🇹🇭 Thailand, 🇻🇳 Vietnam, 🇨🇳 China, 🇯🇵 Japan,
🇺🇸 USA, 🇷🇺 Russia, ⚠️ warning, ❗ urgent. Use the 🔥 emoji ONLY for news about an actual fire.
Return JSON only: {{"items": [{{"id": <id>, "emoji": "..."}}]}}

Items (JSON):
{items}
"""

_FALLBACK = [
    (r"стрельб|выстрел|оружи|пистолет", "🔫"), (r"мошен|скам|афер|обман", "🕵️"),
    (r"убий|зарез|нож|тело ", "🔪"), (r"огра|краж|вор", "🦹"), (r"нарко|метамф|героин", "💊"),
    (r"арест|задерж|облав|рейд|полици", "🚔"), (r"суд|приговор|тюрьм", "⚖️"), (r"ДТП|авари|столкнов", "🚗💥"),
    (r"пожар|сгорел|возгоран", "🔥"), (r"мин[аыну]\b|минн|разминир", "💣"), (r"паводк|наводн|затоп", "🌊"),
    (r"погод|дожд|шторм|жара", "🌧️"), (r"виз[аыу]|депорт|иммиграц|границ", "🛂"),
    (r"рейс|аэропорт|авиа|Течо", "✈️"), (r"Ангкор|храм|Апсар|APSARA", "🛕"), (r"туризм|турист|пляж", "🏝️"),
    (r"топлив|бензин|дизел", "⛽"), (r"банк|риел|Bakong|валют|курс", "🏦"),
    (r"цен[аыу]|экономик|экспорт|импорт|торгов|инвест|ВВП", "💰"), (r"больниц|здоров|ВИЧ|медиц|недоедан", "🏥"),
    (r"образован|школ|студент|обучен", "🎓"), (r"эколог|пластик|лес|вод[аоы]ём|сток", "🌿"),
    (r"выбор|избират", "🗳️"), (r"закон|правил|запрет|штраф", "📜"), (r"Таиланд|тайск", "🇹🇭"),
    (r"Вьетнам", "🇻🇳"), (r"Китай|КНР", "🇨🇳"), (r"Япони", "🇯🇵"), (r"строит|дорог|мост|инфраструкт", "🏗️"),
    (r"Хун Манет|премьер|министр|правительств", "🏛️"),
]


TAGS_PROMPT = """You add hashtags to posts of a Russian-language Telegram news channel about Cambodia.
For EACH item give 2-3 Russian hashtags that describe the topic (and the city/province if one is clearly named).
Prefer this shared vocabulary so tags group well: #криминал #скам #наркотики #ДТП #происшествия #суд #визы
#законы #экономика #цены #бизнес #недвижимость #туризм #транспорт #авиа #политика #дипломатия #выборы #здоровье
#образование #экология #погода #культура #Ангкор #общество #безопасность #мины #граница
#Пномпень #Сиемреап #Сиануквиль #Кампот #Баттамбанг #Пойпет #Таиланд #Вьетнам #Китай #Япония #США #Россия.
Hashtags: start with #, no spaces inside (use _ if needed), Russian words (city names in Russian).
Return JSON only: {{"items": [{{"id": <id>, "hashtags": ["#...", "#..."]}}]}}

Items (JSON):
{items}
"""

_TAG_FALLBACK = [
    (r"арест|задерж|облав|полици|убий|огра|краж", "#криминал"), (r"мошен|скам|афер", "#скам"),
    (r"нарко", "#наркотики"), (r"ДТП|авари", "#ДТП"), (r"виз[аыу]|депорт|иммиграц", "#визы"),
    (r"цен[аыу]|экономик|экспорт|торгов|инвест|банк|риел", "#экономика"), (r"туризм|турист|Ангкор", "#туризм"),
    (r"выбор", "#выборы"), (r"министр|премьер|Хун Манет|правительств", "#политика"),
    (r"больниц|здоров|ВИЧ|медиц", "#здоровье"), (r"эколог|пластик|лес", "#экология"),
    (r"погод|дожд|паводк|наводн", "#погода"), (r"Пномпен", "#Пномпень"), (r"Сиемреап", "#Сиемреап"),
    (r"Сиануквил", "#Сиануквиль"), (r"Таиланд", "#Таиланд"),
]


def fallback_tags(text: str) -> list[str]:
    tags = [tag for pattern, tag in _TAG_FALLBACK if re.search(pattern, text, re.I)]
    return (list(dict.fromkeys(tags)) or ["#новости"])[:3] + (["#Камбоджа"] if len(tags) < 2 else [])


_HAS_TAGS = re.compile(r"(^|\s)#[A-Za-zА-Яа-яЁё_]")


def add_tags(text_html: str, tags: list[str]) -> str:
    line = " ".join(tags)
    lines = text_html.rstrip().split("\n")
    if lines and "@CambodlaNews" in lines[-1]:        # хэштеги — перед подписью канала
        return "\n".join(lines[:-1]).rstrip() + "\n\n" + line + "\n" + lines[-1]
    return text_html.rstrip() + "\n\n" + line


def run_hashtags(dry_run: bool) -> int:
    bot.log("Читаю все посты канала…")
    posts = [p for p in load_posts() if p["id"] != INTRO_POST_ID]
    progress = load_progress()
    done = set(progress.setdefault("tags_done", []))
    todo = [p for p in posts if p["id"] not in done and not _HAS_TAGS.search(p["plain"])]
    bot.log(f"Постов без хэштегов: {len(todo)}")
    tags: dict[int, list[str]] = {}
    for start in range(0, len(todo), 40):
        chunk = todo[start:start + 40]
        payload = [{"id": p["id"], "text": re.sub(r"\s+", " ", p["plain"])[:300]} for p in chunk]
        try:
            data = bot.ai_json(TAGS_PROMPT.format(items=json.dumps(payload, ensure_ascii=False)), temperature=0.2)
            for item in data.get("items", []):
                clean = [re.sub(r"\s+", "_", str(t).strip()) for t in item.get("hashtags", [])]
                clean = [t if t.startswith("#") else f"#{t}" for t in clean if len(t) > 1][:3]
                if clean:
                    tags[int(item["id"])] = clean
            bot.log(f"  хэштеги подобраны для постов {chunk[0]['id']}–{chunk[-1]['id']} ({bot._working_model})")
        except Exception as exc:
            bot.log(f"  ИИ недоступен ({str(exc)[:80]}), подбираю по словам")
        for p in chunk:
            tags.setdefault(p["id"], fallback_tags(p["plain"]))

    if dry_run:
        for p in todo[:8]:
            print(f"пост {p['id']}: {' '.join(tags[p['id']])}  ←  {p['plain'][:70]!r}")
        bot.log("Это был предпросмотр: ничего не изменено.")
        return 0

    ok = failed = 0
    for n, p in enumerate(todo, 1):
        new_html = add_tags(p["html"], tags[p["id"]])
        if p["media"] and bot.visible_len(new_html) > 1024:
            bot.log(f"  пост {p['id']}: подпись станет длиннее 1024 символов — пропускаю")
            continue
        status = edit_post(p, new_html)
        if status in ("ok", "same") or "can't be edited" in status:
            progress["tags_done"].append(p["id"])
            save_progress(progress)
            ok += status != "same" and not status.startswith("error")
        else:
            failed += 1
            bot.log(f"  ⚠️ пост {p['id']}: {status}")
        if n % 25 == 0:
            bot.log(f"  … {n}/{len(todo)}")
        time.sleep(2.5)
    bot.log(f"Готово: хэштеги добавлены к {ok} постам, ошибок {failed}")
    return 0


def fallback_emoji(text: str) -> str:
    for pattern, emoji in _FALLBACK:
        if re.search(pattern, text, re.I):
            return emoji
    return "📰"


# ---------- чтение канала из публичной веб-версии ----------
_TAGS = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u", "s": "s", "strike": "s",
         "del": "s", "code": "code", "pre": "pre"}


def to_tg_html(node) -> str:
    out = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(html.escape(str(child), quote=False))
            continue
        name = child.name
        classes = child.get("class") or []
        if name == "br":
            out.append("\n")
        elif "emoji" in classes or name == "tg-emoji":
            out.append(html.escape(child.get_text(), quote=False))
        elif name == "a":
            text, href = child.get_text(), child.get("href", "")
            if text.startswith(("#", "@")) or not href.startswith("http"):
                out.append(html.escape(text, quote=False))
            else:
                out.append(f'<a href="{html.escape(href, quote=True)}">{to_tg_html(child)}</a>')
        elif "tgme_widget_message_spoiler" in " ".join(classes):
            out.append(f"<tg-spoiler>{to_tg_html(child)}</tg-spoiler>")
        elif name == "blockquote":
            out.append(f"<blockquote>{to_tg_html(child)}</blockquote>")
        elif name in _TAGS:
            tag = _TAGS[name]
            out.append(f"<{tag}>{to_tg_html(child)}</{tag}>")
        else:
            out.append(to_tg_html(child))
    return "".join(out)


def load_posts() -> list[dict]:
    posts, before = {}, None
    while True:
        url = f"https://t.me/s/{CHANNEL_USERNAME}" + (f"?before={before}" if before else "")
        soup = BeautifulSoup(bot.http_get(url).text, "html.parser")
        widgets = soup.select("div.tgme_widget_message[data-post]")
        if not widgets:
            break
        ids = []
        for w in widgets:
            try:
                msg_id = int(w["data-post"].rsplit("/", 1)[1])
            except (KeyError, ValueError, IndexError):
                continue
            ids.append(msg_id)
            text_el = w.select_one(".tgme_widget_message_text")
            if text_el is None or msg_id in posts or msg_id == 1 or "service_message" in (w.get("class") or []):
                continue
            media = w.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player, "
                                 ".tgme_widget_message_document, .tgme_widget_message_roundvideo_player, "
                                 ".tgme_widget_message_grouped_wrap") is not None
            posts[msg_id] = {"id": msg_id, "media": media, "html": to_tg_html(text_el).strip(),
                             "plain": text_el.get_text("\n").strip()}
        if not ids or min(ids) <= 1:
            break
        before = min(ids)
        time.sleep(0.5)
    return [posts[k] for k in sorted(posts)]


# ---------- преобразование текста ----------
_LEAD = re.compile(r"^((?:<[^>]+>)*)([^\w<«\"„“(\[]+)")


def strip_sources(text_html: str) -> str:
    lines = [ln for ln in text_html.split("\n")
             if "источник" not in re.sub(r"<[^>]+>", "", ln).lower()]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return re.sub(r"(<(b|i|u|s)>)\s*(</\2>)", "", out)   # пустые теги после удаления


def set_lead_emoji(text_html: str, emoji: str) -> str:
    m = _LEAD.match(text_html)
    if m and any(ord(c) > 0x2000 for c in m.group(2)):
        return f"{m.group(1)}{emoji} {text_html[m.end():].lstrip()}"
    return f"{emoji} {text_html}"


def pick_emojis(posts: list[dict]) -> dict[int, str]:
    result: dict[int, str] = {}
    for start in range(0, len(posts), 40):
        chunk = posts[start:start + 40]
        payload = [{"id": p["id"], "text": re.sub(r"\s+", " ", p["plain"])[:260]} for p in chunk]
        try:
            data = bot.ai_json(EMOJI_PROMPT.format(items=json.dumps(payload, ensure_ascii=False)), temperature=0.2)
            for item in data.get("items", []):
                emoji = str(item.get("emoji", "")).strip()
                if emoji and not re.search(r"[\w<>]", emoji):
                    result[int(item["id"])] = emoji[:12]
            bot.log(f"  эмодзи подобраны для постов {chunk[0]['id']}–{chunk[-1]['id']} ({bot._working_model})")
        except Exception as exc:
            bot.log(f"  ИИ недоступен для {chunk[0]['id']}–{chunk[-1]['id']} ({str(exc)[:80]}), подбираю по словам")
        for p in chunk:
            result.setdefault(p["id"], fallback_emoji(p["plain"]))
    return result


def load_progress() -> dict:
    try:
        data = json.loads(PROGRESS.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    data.setdefault("done", [])
    data.setdefault("emojis", {})
    return data


def save_progress(progress: dict) -> None:
    PROGRESS.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")


def edit_post(post: dict, new_html: str) -> str:
    data = {"chat_id": bot.CHANNEL, "message_id": post["id"], "parse_mode": "HTML"}
    try:
        if post["media"]:
            bot.tg("editMessageCaption", {**data, "caption": new_html})
        else:
            bot.tg("editMessageText", {**data, "text": new_html})
        return "ok"
    except bot.TgError as exc:
        if "not modified" in str(exc):
            return "same"
        return f"error: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--hashtags", action="store_true")
    args = parser.parse_args()

    if args.hashtags:
        return run_hashtags(args.dry_run)

    if args.restore:
        saved = json.loads(BACKUP.read_text(encoding="utf-8"))
        for n, p in enumerate(saved, 1):
            if p["id"] != INTRO_POST_ID:
                status = edit_post(p, p["html"])
                if status.startswith("error"):
                    bot.log(f"  ⚠️ пост {p['id']}: {status}")
                time.sleep(2.5)
            if n % 25 == 0:
                bot.log(f"  … {n}/{len(saved)}")
        bot.log("Тексты постов возвращены из резервной копии.")
        return 0

    bot.log("Читаю все посты канала…")
    posts = load_posts()
    bot.log(f"Постов с текстом: {len(posts)}")
    if not args.dry_run and not BACKUP.exists():
        BACKUP.write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
        bot.log(f"Резервная копия: {BACKUP.name}")

    progress = load_progress()
    done_ids = set(progress["done"])
    news = [p for p in posts if p["id"] != INTRO_POST_ID and p["id"] not in done_ids]
    if done_ids:
        bot.log(f"Уже обработано раньше: {len(done_ids)} — продолжаю с места остановки")
    cached = {int(k): v for k, v in progress["emojis"].items()}
    need = [p for p in news if p["id"] not in cached]
    if need:
        bot.log("Подбираю эмодзи…")
        cached.update(pick_emojis(need))
        progress["emojis"] = {str(k): v for k, v in cached.items()}
        if not args.dry_run:
            save_progress(progress)
    emojis = cached

    changes = []
    for p in news:
        new_html = set_lead_emoji(strip_sources(p["html"]), emojis[p["id"]])
        if new_html != p["html"] and new_html.strip():
            changes.append((p, new_html))
    bot.log(f"Постов к изменению: {len(changes)}")

    if args.dry_run:
        for p, new_html in changes[:6] + changes[-3:]:
            print(f"\n--- пост {p['id']} ---\nБЫЛО:  {p['plain'][:160]!r}\nСТАЛО: {new_html[:200]!r}")
        print(f"\nИмя бота → {BOT_NAME}\nПост {INTRO_POST_ID} → вступительный пост:\n{INTRO_CAPTION}")
        print(f"\nАватар: {'есть' if AVATAR.exists() else 'НЕТ ФАЙЛА'} ({AVATAR.name})")
        bot.log("Это был предпросмотр: ничего не изменено.")
        return 0

    if not progress.get("setup_done"):
        try:
            bot.tg("setMyName", {"name": BOT_NAME})
            bot.log(f"✅ Имя бота: {BOT_NAME}")
        except bot.TgError as exc:
            bot.log(f"❌ Имя бота: {exc}")

    if AVATAR.exists() and not progress.get("setup_done"):
        try:
            bot.tg("setChatPhoto", {"chat_id": bot.CHANNEL},
                   files={"photo": ("avatar.jpg", AVATAR.read_bytes(), "image/jpeg")})
            bot.log("✅ Аватар канала обновлён")
        except bot.TgError as exc:
            bot.log(f"❌ Аватар: {exc}")
        try:
            media = {"type": "photo", "media": "attach://cover", "caption": INTRO_CAPTION, "parse_mode": "HTML"}
            bot.tg("editMessageMedia", {"chat_id": bot.CHANNEL, "message_id": INTRO_POST_ID, "media": media},
                   files={"cover": ("cover.jpg", AVATAR.read_bytes(), "image/jpeg")})
            bot.log(f"✅ Пост {INTRO_POST_ID} стал вступительным")
        except bot.TgError as exc:
            bot.log(f"❌ Пост {INTRO_POST_ID}: {exc}")
    progress["setup_done"] = True
    save_progress(progress)
    try:
        bot.tg("pinChatMessage", {"chat_id": bot.CHANNEL, "message_id": INTRO_POST_ID, "disable_notification": True})
        bot.log(f"📌 Пост {INTRO_POST_ID} закреплён")
    except bot.TgError as exc:
        bot.log(f"❌ Закреп поста {INTRO_POST_ID}: {exc}")

    done = same = failed = 0
    for n, (p, new_html) in enumerate(changes, 1):
        status = edit_post(p, new_html)
        if status in ("ok", "same") or "can't be edited" in status:
            progress["done"].append(p["id"])
            save_progress(progress)
        if status == "ok":
            done += 1
        elif status == "same":
            same += 1
        else:
            failed += 1
            bot.log(f"  ⚠️ пост {p['id']}: {status}")
        if n % 25 == 0:
            bot.log(f"  … {n}/{len(changes)}")
        time.sleep(2.5)
    bot.log(f"Готово: изменено {done}, без изменений {same}, ошибок {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
