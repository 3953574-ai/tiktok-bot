import asyncio
import logging
import os
import re
import sys
import uuid
import random
import subprocess
from typing import List, Tuple, Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.utils.media_group import MediaGroupBuilder

import aiohttp
from aiohttp import web

from deep_translator import GoogleTranslator
from langdetect import detect
import instaloader
import static_ffmpeg

from pathlib import Path

# ---------------------------
#  БАЗОВА КОНФІГУРАЦІЯ
# ---------------------------

# Спочатку пробуємо взяти з змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Якщо не знайшли – пробуємо прочитати з .env поруч із файлом bot.py
if not BOT_TOKEN:
    env_path = Path(__file__).with_name(".env")
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("BOT_TOKEN="):
                BOT_TOKEN = line.split("=", 1)[1].strip()
                break
    except FileNotFoundError:
        pass

if not BOT_TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN в оточенні або у файлі .env")

API_HOST = "0.0.0.0"
API_PORT = int(os.getenv("PORT", 20000))

static_ffmpeg.add_paths()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

translator = GoogleTranslator(source="auto", target="uk")

STORAGE = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ---------- Інстанс Instaloader (один, не створюємо щоразу) ----------
INSTA_LOADER = instaloader.Instaloader(quiet=True)
INSTA_LOADER.context._user_agent = "Instagram 269.0.0.18.75 Android"


# ---------------------------
#  КЛАВІАТУРИ
# ---------------------------

def get_video_keyboard(data_id: str, current_lang: str = "orig") -> InlineKeyboardMarkup:
    """
    Для відео-постів (TikTok / X / Instagram video):
    – 🎵 Аудіо
    – 🎬 Відео
    – 🇺🇦 Переклад / 🌐 Оригінал (тільки якщо текст не українською)
    """
    btn_audio = InlineKeyboardButton(
        text="🎵 Аудіо", callback_data=f"vid_audio:{data_id}"
    )
    btn_video = InlineKeyboardButton(
        text="🎬 Відео", callback_data=f"vid_clean:{data_id}"
    )

    buttons = [[btn_audio, btn_video]]

    data = STORAGE.get(data_id)
    has_diff = bool(data and data.get("has_diff"))

    # Якщо мова вже українська – кнопки перекладу не показуємо
    if has_diff:
        if current_lang == "orig":
            lang_btn = InlineKeyboardButton(
                text="🇺🇦 Переклад", callback_data=f"vid_lang:trans:{data_id}"
            )
        else:
            lang_btn = InlineKeyboardButton(
                text="🌐 Оригінал", callback_data=f"vid_lang:orig:{data_id}"
            )
        buttons.append([lang_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_photo_keyboard(data_id: str, current_lang: str = "orig") -> InlineKeyboardMarkup:
    """
    Для фото/галереї (Instagram, X, TikTok-фото):
    – 🖼️ Тільки медіа
    – 🇺🇦 Переклад / 🌐 Оригінал (тільки якщо текст не українською)
    """
    btn_clean = InlineKeyboardButton(
        text="🖼️ Тільки медіа", callback_data=f"pho_clean:{data_id}"
    )

    buttons = [[btn_clean]]

    data = STORAGE.get(data_id)
    has_diff = bool(data and data.get("has_diff"))

    if has_diff:
        if current_lang == "orig":
            lang_btn = InlineKeyboardButton(
                text="🇺🇦 Переклад", callback_data=f"pho_lang:trans:{data_id}"
            )
        else:
            lang_btn = InlineKeyboardButton(
                text="🌐 Оригінал", callback_data=f"pho_lang:orig:{data_id}"
            )
        buttons.append([lang_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------
#  ДОПОМІЖНІ ФУНКЦІЇ
# ---------------------------

def sanitize_filename(name: str) -> str:
    if not name:
        return "audio"
    name = re.sub(r'[\\/*?:"<>|]', "", str(name))
    name = name.replace("\n", " ").strip()
    return name[:50]


def parse_message_data(text: Optional[str]):
    """
    Повертає: url, clean_mode, audio_mode
    """
    if not text:
        return None, False, False
    url_match = re.search(r"(https?://[^\s]+)", text)
    if not url_match:
        return None, False, False

    found_url = url_match.group(1)

    cmd_text = text.replace(found_url, "").lower()

    clean_mode = (
        "-" in cmd_text or "!" in cmd_text or "clear" in cmd_text or "чисто" in cmd_text
    )
    audio_mode = ("!a" in cmd_text) or ("audio" in cmd_text) or ("аудіо" in cmd_text)

    return found_url, clean_mode, audio_mode


async def download_content(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logging.warning(f"download_content error: {e}")
    return None


async def prepare_texts(text: str):
    """
    Повертає: (orig_text, trans_text, has_diff)
    За замовчуванням показуємо ОРИГІНАЛ.
    has_diff = True, якщо мова не українська.
    """
    if not text:
        return "", "", False
    try:
        lang = detect(text)
        if lang != "uk":
            trans = await asyncio.to_thread(translator.translate, text)
            return text, trans, True
        # вже українська
        return text, text, False
    except Exception as e:
        logging.warning(f"prepare_texts error: {e}")
        return text, text, False


def format_caption(author_name: str, author_url: str, text: str, original_url: str):
    caption = f"👤 <a href='{author_url}'><b>{author_name}</b></a>\n\n"
    if text:
        caption += f"📝 {text}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    return caption[:1024]


def extract_audio_from_video_bytes(video_bytes: bytes) -> Optional[bytes]:
    try:
        unique = str(uuid.uuid4())
        vid_path = f"temp_vid_{unique}.mp4"
        aud_path = f"temp_aud_{unique}.mp3"
        with open(vid_path, "wb") as f:
            f.write(video_bytes)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                vid_path,
                "-vn",
                "-acodec",
                "libmp3lame",
                "-q:a",
                "2",
                aud_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(aud_path, "rb") as f:
            audio_bytes = f.read()
        os.remove(vid_path)
        os.remove(aud_path)
        return audio_bytes
    except Exception as e:
        logging.warning(f"extract_audio_from_video_bytes error: {e}")
        return None


async def get_instagram_post(user_url: str):
    """
    Повертає instaloader.Post або None
    """
    try:
        m = re.search(r"/(p|reel|reels)/([A-Za-z0-9_\-]+)", user_url)
        if not m:
            return None
        shortcode = m.group(2)

        def _load():
            return instaloader.Post.from_shortcode(INSTA_LOADER.context, shortcode)

        post = await asyncio.to_thread(_load)
        return post
    except Exception as e:
        logging.warning(f"get_instagram_post error: {e}")
        return None


def chunk_list(lst: List, size: int) -> List[List]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


async def resend_photo_post(message: types.Message, data_id: str, target_lang: str):
    """Повторно відправляє фото/галерею з описом потрібною мовою,
    використовуючи вже збережені байти у STORAGE.
    """
    data = STORAGE.get(data_id)
    if not data:
        return

    text = data["orig_text"] if target_lang == "orig" else data["trans_text"]
    caption = format_caption(
        data["author_name"],
        data["author_link"],
        text,
        data["user_url"],
    )

    photo_bytes = data.get("photo_bytes")
    gallery_data = data.get("gallery_data") or []

    # Якщо одиночне фото
    if photo_bytes and not gallery_data:
        await message.answer_photo(
            BufferedInputFile(photo_bytes, filename="photo.jpg"),
            caption=caption,
            parse_mode="HTML",
        )
        return

    # Якщо галерея / змішане медіа
    if gallery_data:
        global_index = 0
        for chunk in chunk_list(gallery_data, 10):
            mg = MediaGroupBuilder()
            first_in_chunk = True
            for content, ctype in chunk:
                cap = caption if (global_index == 0 and first_in_chunk) else None
                if ctype == "video":
                    mg.add_video(
                        BufferedInputFile(content, filename="media.mp4"),
                        caption=cap,
                        parse_mode="HTML" if cap else None,
                    )
                else:
                    mg.add_photo(
                        BufferedInputFile(content, filename="photo.jpg"),
                        caption=cap,
                        parse_mode="HTML" if cap else None,
                    )
                first_in_chunk = False
                global_index += 1
            await message.answer_media_group(mg.build())


# -------------------------------------------------
# ФУНКЦІЯ РОЗГОРТАННЯ
# -------------------------------------------------
async def resolve_redirect(url: str) -> str:
    """
    Розгортає короткі посилання vm.tiktok.com у повні.
    Використовує User-Agent Facebook, щоб отримати прямий редірект.
    """
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            # Імітуємо бота Facebook (найкраще працює для отримання редіректів)
            headers = {
                "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            }
            async with aiohttp.ClientSession() as session:
                # allow_redirects=True - aiohttp сам пройде по редіректах
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    return str(resp.url)
        except Exception:
            pass
    return url


# -------------------------------------------------
# PER-SOURCE HANDLERS
# -------------------------------------------------

async def handle_tiktok(user_url: str):
    """
    Повертає:
    author_name, author_link, raw_desc, audio_name, audio_bytes, video_bytes, photo_bytes, gallery_data
    """
    # 1. Розгортаємо посилання
    full_url = await resolve_redirect(user_url)
    
    # Очищуємо URL від зайвих параметрів
    full_url = full_url.split("?")[0]

    api_url = "https://www.tikwm.com/api/"

    async with aiohttp.ClientSession() as session:
        # Перша спроба
        async with session.post(api_url, data={"url": full_url, "hd": 1}) as r:
            data = await r.json()
            
            if "data" not in data:
                error_msg = data.get("msg", "Unknown error")
                
                # Якщо ліміт або помилка парсингу - пауза і повтор
                if "Url parsing is failed" in error_msg or "Free Api Limit" in error_msg:
                     logging.warning("First attempt failed, waiting 1.1s and retrying...")
                     await asyncio.sleep(1.1)  # ПАУЗА 1.1 сек
                     
                     # Друга спроба
                     async with session.post(api_url, data={"url": full_url, "hd": 1}) as r2:
                         data = await r2.json()
                         if "data" not in data:
                             raise Exception(f"TikWM Error: {data.get('msg')}")
                else:
                    raise Exception(f"TikWM Error: {error_msg}")

            data = data["data"]

    author_name = data["author"]["nickname"]
    unique_id = data["author"]["unique_id"]
    author_link = f"https://www.tiktok.com/@{unique_id}"
    raw_desc = data.get("title", "")

    m_author = data.get("music_info", {}).get("author", author_name)
    m_title = data.get("music_info", {}).get("title", "Audio")
    audio_name = f"{sanitize_filename(m_author)} - {sanitize_filename(m_title)}.mp3"

    audio_bytes = None
    mb = await download_content(data.get("music"))
    if mb:
        audio_bytes = mb

    video_bytes = None
    photo_bytes = None
    gallery_data: List[Tuple[bytes, str]] = []

    if "images" in data and data["images"]:
        tasks = [download_content(u) for u in data["images"]]
        imgs = await asyncio.gather(*tasks)
        for img in imgs:
            if img:
                gallery_data.append((img, "photo"))
    else:
        video_bytes = await download_content(data.get("hdplay") or data.get("play"))

    return (
        author_name,
        author_link,
        raw_desc,
        audio_name,
        audio_bytes,
        video_bytes,
        photo_bytes,
        gallery_data,
    )


async def handle_twitter(user_url: str):
    """
    X / Twitter через vxtwitter.
    """
    # ВИПРАВЛЕНО: Один слеш для regex
    m = re.search(r"/status/(\d+)", user_url)
    if not m:
        raise Exception("Не знайдено ID твіта")

    tw_id = m.group(1)
    api_url = f"https://api.vxtwitter.com/Twitter/status/{tw_id}"

    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as r:
            if r.status != 200:
                raise Exception(f"Twitter API error, status={r.status}")
            tweet = await r.json()

    author_name = tweet.get("user_name", "User")
    screen_name = tweet.get("user_screen_name", "user")
    author_link = f"https://x.com/{screen_name}"
    raw_desc = tweet.get("text", "")

    audio_name = f"{sanitize_filename(author_name)} - twitter.mp3"
    audio_bytes = None

    video_bytes = None
    photo_bytes = None
    gallery_data: List[Tuple[bytes, str]] = []

    media_list = tweet.get("media_extended", [])
    if not media_list and "media_url" in tweet:
        media_list = [{"type": "image", "url": tweet["media_url"]}]

    has_video = any(m["type"] in ["video", "gif"] for m in media_list)
    if has_video:
        vid = next(m for m in media_list if m["type"] in ["video", "gif"])
        video_bytes = await download_content(vid["url"])
    else:
        tasks = [download_content(m["url"]) for m in media_list]
        imgs = await asyncio.gather(*tasks)
        for img in imgs:
            if img:
                gallery_data.append((img, "photo"))

    return (
        author_name,
        author_link,
        raw_desc,
        audio_name,
        audio_bytes,
        video_bytes,
        photo_bytes,
        gallery_data,
    )


async def handle_instagram(user_url: str):
    """
    Instagram:
    – одиночне відео
    – одиночне фото
    – sidecar: фото/відео-галерея
    """
    post = await get_instagram_post(user_url)
    if not post:
        raise Exception("Не вдалося отримати пост Instagram")

    author_name = post.owner_username or "Instagram"
    author_link = f"https://instagram.com/{author_name}"
    raw_desc = (post.caption or "").split("\n")[0]
    audio_name = f"{sanitize_filename(author_name)}.mp3"

    audio_bytes = None
    video_bytes = None
    photo_bytes = None
    gallery_data: List[Tuple[bytes, str]] = []

    # Sidecar (галерея)
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())

        async def dl(node):
            url = node.video_url if node.is_video else node.display_url
            return await download_content(url), "video" if node.is_video else "photo"

        tasks = [dl(n) for n in nodes]
        results = await asyncio.gather(*tasks)
        for content, ctype in results:
            if content:
                gallery_data.append((content, ctype))

    else:
        # Одиночне відео або фото
        if post.is_video:
            url = post.video_url
            video_bytes = await download_content(url)
        else:
            url = post.url
            photo_bytes = await download_content(url)

    return (
        author_name,
        author_link,
        raw_desc,
        audio_name,
        audio_bytes,
        video_bytes,
        photo_bytes,
        gallery_data,
    )


# ==========================================
#  MAIN LOGIC
# ==========================================

async def process_media_request(
    message: types.Message,
    user_url: str,
    clean_mode: bool = False,
    audio_mode: bool = False,
    is_button_click: bool = False,
    force_lang: str = "orig",  # 'orig' або 'trans'
):
    if not user_url:
        return

    status_msg = None
    if not clean_mode and not audio_mode and not is_button_click:
        status_msg = await message.reply("⏳ Обробляю...")

    try:
        author_name = "User"
        author_link = user_url
        raw_desc = ""
        audio_name = "audio.mp3"

        audio_bytes = None
        video_bytes = None
        photo_bytes = None
        gallery_data: List[Tuple[bytes, str]] = []

        # TikTok
        if "tiktok.com" in user_url:
            (
                author_name,
                author_link,
                raw_desc,
                audio_name,
                audio_bytes,
                video_bytes,
                photo_bytes,
                gallery_data,
            ) = await handle_tiktok(user_url)

        # Twitter / X
        elif "twitter.com" in user_url or "x.com" in user_url:
            (
                author_name,
                author_link,
                raw_desc,
                audio_name,
                audio_bytes,
                video_bytes,
                photo_bytes,
                gallery_data,
            ) = await handle_twitter(user_url)

        # Instagram
        elif "instagram.com" in user_url:
            (
                author_name,
                author_link,
                raw_desc,
                audio_name,
                audio_bytes,
                video_bytes,
                photo_bytes,
                gallery_data,
            ) = await handle_instagram(user_url)
        else:
            raise Exception("Непідтримуване посилання")

        # AUDIO ONLY (кнопка / режим)
        if audio_mode:
            if audio_bytes:
                await message.answer_audio(
                    BufferedInputFile(audio_bytes, filename=audio_name)
                )
            elif video_bytes:
                extracted = await asyncio.to_thread(
                    extract_audio_from_video_bytes, video_bytes
                )
                if extracted:
                    await message.answer_audio(
                        BufferedInputFile(extracted, filename=audio_name)
                    )
                else:
                    await message.answer("Не вдалося отримати аудіо 😔")
            else:
                await message.answer("Не вдалося отримати аудіо 😔")

            if status_msg:
                await status_msg.delete()
            return

        # Тексти / переклад
        orig_text, trans_text, has_diff = await prepare_texts(raw_desc)
        text_to_show = orig_text if force_lang == "orig" else trans_text
        caption = format_caption(author_name, author_link, text_to_show, user_url)

        # CLEAN MODE — тільки медіа без описів/кнопок
        if clean_mode:
            if video_bytes:
                await message.answer_video(
                    BufferedInputFile(video_bytes, filename="video.mp4")
                )
            elif photo_bytes:
                await message.answer_photo(
                    BufferedInputFile(photo_bytes, filename="photo.jpg")
                )
            elif gallery_data:
                for chunk in chunk_list(gallery_data, 10):
                    mg = MediaGroupBuilder()
                    for content, ctype in chunk:
                        if ctype == "video":
                            mg.add_video(
                                BufferedInputFile(content, filename="media.mp4")
                            )
                        else:
                            mg.add_photo(
                                BufferedInputFile(content, filename="photo.jpg")
                            )
                    await message.answer_media_group(mg.build())
            if status_msg:
                await status_msg.delete()
            return

        # СТАНДАРТНИЙ РЕЖИМ
        data_id = str(uuid.uuid4())[:8]
        STORAGE[data_id] = {
            "user_url": user_url,
            "orig_text": orig_text,
            "trans_text": trans_text,
            "has_diff": has_diff,
            "author_name": author_name,
            "author_link": author_link,
            "audio_name": audio_name,
            "kind": None,
            "video_file_id": None,
            "current_lang": force_lang,
        }

        # ---------- ВІДЕО-ПОСТ ----------
        if video_bytes and not gallery_data:
            STORAGE[data_id]["kind"] = "video"
            sent = await message.answer_video(
                BufferedInputFile(video_bytes, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_video_keyboard(data_id, current_lang=force_lang),
            )
            STORAGE[data_id]["video_file_id"] = sent.video.file_id

        # ---------- ФОТО / ГАЛЕРЕЯ ----------
        else:
            STORAGE[data_id]["kind"] = "photo"
            STORAGE[data_id]["photo_bytes"] = photo_bytes
            STORAGE[data_id]["gallery_data"] = gallery_data

            # 1) Шлемо саме медіа з описом (оригінал/вибрана мова)
            if photo_bytes and not gallery_data:
                await message.answer_photo(
                    BufferedInputFile(photo_bytes, filename="photo.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                # галерея / змішане медіа
                for_idx = 0
                for chunk in chunk_list(gallery_data, 10):
                    mg = MediaGroupBuilder()
                    first_in_chunk = True
                    for content, ctype in chunk:
                        cap = caption if for_idx == 0 and first_in_chunk else None
                        if ctype == "video":
                            mg.add_video(
                                BufferedInputFile(content, filename="media.mp4"),
                                caption=cap,
                                parse_mode="HTML" if cap else None,
                            )
                        else:
                            mg.add_photo(
                                BufferedInputFile(content, filename="photo.jpg"),
                                caption=cap,
                                parse_mode="HTML" if cap else None,
                            )
                        first_in_chunk = False
                        for_idx += 1
                    await message.answer_media_group(mg.build())

            # 2) Окреме повідомлення «Опції» з кнопками
            opts_msg = await message.answer(
                "Опції:",
                reply_markup=get_photo_keyboard(data_id, current_lang=force_lang),
            )
            STORAGE[data_id]["opts_msg_id"] = opts_msg.message_id

            # 3) Якщо в пості є аудіо (TikTok / інші) — кидаємо окремо
            if audio_bytes:
                try:
                    await message.answer_audio(
                        BufferedInputFile(audio_bytes, filename=audio_name)
                    )
                except Exception as e:
                    logging.warning(f"send audio after photo error: {e}")

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        logging.exception(f"process_media_request error: {e}")
        if status_msg:
            try:
                await status_msg.edit_text("❌ Помилка завантаження.")
            except Exception:
                pass


# ==========================================
#  CALLBACKS
# ==========================================

@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    try:
        parts = callback.data.split(":")
        action = parts[0]

        # ---------- ВІДЕО ----------
        if action == "vid_clean":
            data_id = parts[1]
            data = STORAGE.get(data_id)
            if data and data.get("video_file_id"):
                await callback.message.answer_video(data["video_file_id"])
            else:
                await callback.answer("Файл втрачено", show_alert=True)
            await callback.answer()

        elif action == "vid_audio":
            data_id = parts[1]
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return
            await callback.answer("Витягую аудіо...")
            await process_media_request(
                callback.message,
                data["user_url"],
                audio_mode=True,
                is_button_click=True,
            )

        elif action == "vid_lang":
            target_lang = parts[1]  # orig / trans
            data_id = parts[2]
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return

            text = (
                data["orig_text"]
                if target_lang == "orig"
                else data["trans_text"]
            )
            new_cap = format_caption(
                data["author_name"],
                data["author_link"],
                text,
                data["user_url"],
            )
            try:
                await bot.edit_message_caption(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    caption=new_cap,
                    parse_mode="HTML",
                    reply_markup=get_video_keyboard(
                        data_id, current_lang=target_lang
                    ),
                )
                data["current_lang"] = target_lang
            except Exception as e:
                logging.warning(f"vid_lang edit caption error: {e}")
            await callback.answer()

        # ---------- ФОТО / ГАЛЕРЕЯ ----------
        elif action == "pho_clean":
            data_id = parts[1]
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return

            await process_media_request(
                callback.message,
                data["user_url"],
                clean_mode=True,
                is_button_click=True,
                force_lang=data.get("current_lang", "orig"),
            )
            await callback.answer()

        elif action == "pho_lang":
            target_lang = parts[1]  # orig / trans
            data_id = parts[2]
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return

            # 1) Повторно шлемо повний пост (фото/галерею) з потрібною мовою опису
            await resend_photo_post(callback.message, data_id, target_lang)

            # 2) Оновлюємо клавіатуру під «Опції:»
            data["current_lang"] = target_lang
            try:
                await bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    reply_markup=get_photo_keyboard(
                        data_id, current_lang=target_lang
                    ),
                )
            except Exception:
                pass

            await callback.answer()

    except Exception as e:
        logging.exception(f"Callback error: {e}")


# ==========================================
#  ХЕНДЛЕРИ МЕСЕДЖІВ
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Кидай посилання на TikTok / Instagram / X (Twitter).")


@dp.message(F.text.regexp(r"(https?://[^\s]+)"))
async def handle_link(message: types.Message):
    user_url, clean, audio = parse_message_data(message.text or "")
    await process_media_request(
        message,
        user_url,
        clean_mode=clean,
        audio_mode=audio,
        force_lang="orig",  # за замовчуванням показуємо оригінал
    )


# ==========================================
#  WEB-SERVER (для Render / VPS health-check)
# ==========================================

async def start_web_server():
    app = web.Application()

    async def handle_root(request):
        return web.Response(text="Bot is alive!")

    app.router.add_get("/", handle_root)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    logging.info(f"Web server started on {API_HOST}:{API_PORT}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
