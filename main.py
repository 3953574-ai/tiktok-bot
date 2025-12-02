import asyncio
import logging
import os
import re
import sys
import uuid
import random
import subprocess
from typing import List, Tuple, Optional, Dict, Any

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
from langdetect import detect, DetectorFactory
import instaloader
import static_ffmpeg
import yt_dlp  # Використовуємо як резерв

from pathlib import Path

# ---------------------------
#  БАЗОВА КОНФІГУРАЦІЯ
# ---------------------------

# Фікс для стабільності langdetect
DetectorFactory.seed = 0

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    env_path = Path(__file__).with_name(".env")
    try:
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("BOT_TOKEN="):
                    BOT_TOKEN = line.split("=", 1)[1].strip()
                    break
    except Exception:
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

# ---------- Інстанс Instaloader ----------
INSTA_LOADER = instaloader.Instaloader(quiet=True)
INSTA_LOADER.context._user_agent = "Instagram 269.0.0.18.75 Android"


# ---------------------------
#  КЛАВІАТУРИ
# ---------------------------

def get_video_keyboard(data_id: str, current_lang: str = "orig") -> InlineKeyboardMarkup:
    btn_audio = InlineKeyboardButton(text="🎵 Аудіо", callback_data=f"vid_audio:{data_id}")
    btn_video = InlineKeyboardButton(text="🎬 Відео", callback_data=f"vid_clean:{data_id}")
    buttons = [[btn_audio, btn_video]]

    data = STORAGE.get(data_id)
    has_diff = bool(data and data.get("has_diff"))

    if has_diff:
        if current_lang == "orig":
            lang_btn = InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"vid_lang:trans:{data_id}")
        else:
            lang_btn = InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"vid_lang:orig:{data_id}")
        buttons.append([lang_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_photo_keyboard(data_id: str, current_lang: str = "orig") -> InlineKeyboardMarkup:
    btn_clean = InlineKeyboardButton(text="🖼️ Тільки медіа", callback_data=f"pho_clean:{data_id}")
    buttons = [[btn_clean]]

    data = STORAGE.get(data_id)
    has_diff = bool(data and data.get("has_diff"))

    if has_diff:
        if current_lang == "orig":
            lang_btn = InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"pho_lang:trans:{data_id}")
        else:
            lang_btn = InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"pho_lang:orig:{data_id}")
        buttons.append([lang_btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------
#  ДОПОМІЖНІ ФУНКЦІЇ
# ---------------------------

def sanitize_filename(name: str) -> str:
    if not name: return "audio"
    name = re.sub(r'[\\/*?:"<>|]', "", str(name))
    name = name.replace("\n", " ").strip()
    return name[:50]


def parse_message_data(text: Optional[str]):
    if not text: return None, False, False
    url_match = re.search(r"(https?://[^\s]+)", text)
    if not url_match: return None, False, False

    found_url = url_match.group(1)
    found_url = found_url.rstrip(").,") # Очистка від зайвих знаків
    cmd_text = text.replace(found_url, "").lower()

    clean_mode = ("-" in cmd_text or "!" in cmd_text or "clear" in cmd_text or "чисто" in cmd_text)
    audio_mode = ("!a" in cmd_text or "audio" in cmd_text or "аудіо" in cmd_text)

    return found_url, clean_mode, audio_mode


async def download_content(url: str) -> Optional[bytes]:
    if not url: return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logging.warning(f"download_content error: {e}")
    return None


async def prepare_texts(text: str):
    if not text: return "", "", False
    try:
        lang = detect(text)
        if lang != "uk":
            trans = await asyncio.to_thread(translator.translate, text)
            return text, trans, True
        return text, text, False
    except Exception as e:
        logging.warning(f"prepare_texts error: {e}")
        return text, text, False


def format_caption(author_name: str, author_url: str, text: str, original_url: str):
    caption = f"👤 <a href='{author_url}'><b>{author_name}</b></a>\n\n"
    if text: caption += f"📝 {text}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    return caption[:1024]


def extract_audio_from_video_bytes(video_bytes: bytes) -> Optional[bytes]:
    try:
        unique = str(uuid.uuid4())
        vid_path = f"temp_vid_{unique}.mp4"
        aud_path = f"temp_aud_{unique}.mp3"
        with open(vid_path, "wb") as f: f.write(video_bytes)
        subprocess.run(["ffmpeg", "-y", "-i", vid_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", aud_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(aud_path, "rb") as f: audio_bytes = f.read()
        os.remove(vid_path)
        os.remove(aud_path)
        return audio_bytes
    except Exception as e:
        logging.warning(f"extract_audio_from_video_bytes error: {e}")
        return None


def chunk_list(lst: List, size: int) -> List[List]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


async def resend_photo_post(message: types.Message, data_id: str, target_lang: str):
    data = STORAGE.get(data_id)
    if not data: return

    text = data["orig_text"] if target_lang == "orig" else data["trans_text"]
    caption = format_caption(data["author_name"], data["author_link"], text, data["user_url"])
    photo_bytes = data.get("photo_bytes")
    gallery_data = data.get("gallery_data") or []

    if photo_bytes and not gallery_data:
        await message.answer_photo(BufferedInputFile(photo_bytes, filename="photo.jpg"), caption=caption, parse_mode="HTML")
        return

    if gallery_data:
        global_index = 0
        for chunk in chunk_list(gallery_data, 10):
            mg = MediaGroupBuilder()
            first_in_chunk = True
            for content, ctype in chunk:
                cap = caption if (global_index == 0 and first_in_chunk) else None
                if ctype == "video":
                    mg.add_video(BufferedInputFile(content, filename="media.mp4"), caption=cap, parse_mode="HTML" if cap else None)
                else:
                    mg.add_photo(BufferedInputFile(content, filename="photo.jpg"), caption=cap, parse_mode="HTML" if cap else None)
                first_in_chunk = False
                global_index += 1
            await message.answer_media_group(mg.build())


# -------------------------------------------------
# YT-DLP HELPER (Для резервного завантаження)
# -------------------------------------------------
def ytdlp_download(url: str) -> Dict[str, Any]:
    unique_id = str(uuid.uuid4())
    temp_template = f"temp_ytdlp_{unique_id}.%(ext)s"
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': temp_template,
        'quiet': True,
        'no_warnings': True,
        # 'cookies': 'cookies.txt', # Можна додати, якщо буде файл cookies.txt
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            # Fallback search
            d = os.path.dirname(filename) or "."
            prefix = f"temp_ytdlp_{unique_id}"
            candidates = [f for f in os.listdir(d) if f.startswith(prefix)]
            if candidates: filename = candidates[0]
            else: raise Exception("YT-DLP file not found")
        
        with open(filename, "rb") as f:
            video_bytes = f.read()
        try: os.remove(filename)
        except: pass
        return info, video_bytes


async def resolve_redirect(url: str) -> str:
    """ Розгортає короткі посилання vm.tiktok.com """
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            headers = {"User-Agent": "facebookexternalhit/1.1"} # Прикидаємось ботом FB
            async with aiohttp.ClientSession() as session:
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
    TikWM -> Якщо помилка -> YT-DLP
    """
    try:
        # 1. Спроба TikWM API
        full_url = await resolve_redirect(user_url)
        full_url = full_url.split("?")[0] # Чистимо сміття

        # Якщо редірект кинув на Explore (ознака видаленого/приватного відео або блоку)
        if "/explore" in full_url or full_url.rstrip("/") == "https://www.tiktok.com":
             raise Exception("Redirected to Explore (Video not accessible via generic link)")

        api_url = "https://www.tikwm.com/api/"
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, data={"url": full_url, "hd": 1}) as r:
                data = await r.json()
                
                # Обробка помилок API
                if "data" not in data:
                    # Якщо ліміт або помилка - даємо паузу
                    if "Free Api Limit" in data.get("msg", ""):
                        await asyncio.sleep(1.2)
                        async with session.post(api_url, data={"url": full_url, "hd": 1}) as r2:
                            data = await r2.json()
                    
                    if "data" not in data:
                        raise Exception(f"TikWM Error: {data.get('msg')}")

                data = data["data"]

        # Успіх TikWM
        author_name = data["author"]["nickname"]
        unique_id = data["author"]["unique_id"]
        author_link = f"https://www.tiktok.com/@{unique_id}"
        raw_desc = data.get("title", "")
        m_author = data.get("music_info", {}).get("author", author_name)
        m_title = data.get("music_info", {}).get("title", "Audio")
        audio_name = f"{sanitize_filename(m_author)} - {sanitize_filename(m_title)}.mp3"

        audio_bytes = None
        mb = await download_content(data.get("music"))
        if mb: audio_bytes = mb

        video_bytes = None
        photo_bytes = None
        gallery_data = []

        if "images" in data and data["images"]:
            tasks = [download_content(u) for u in data["images"]]
            imgs = await asyncio.gather(*tasks)
            for img in imgs:
                if img: gallery_data.append((img, "photo"))
        else:
            video_bytes = await download_content(data.get("hdplay") or data.get("play"))

        return (author_name, author_link, raw_desc, audio_name, audio_bytes, video_bytes, photo_bytes, gallery_data)

    except Exception as e:
        logging.warning(f"Primary TikTok method failed: {e}. Switching to Reserve (YT-DLP)...")
        
        # 2. РЕЗЕРВНИЙ МЕТОД: YT-DLP
        # Він впорається там, де API не змогло розпізнати посилання або дало помилку
        try:
            info, vid_bytes = await asyncio.to_thread(ytdlp_download, user_url) # Передаємо оригінальний лінк
            
            author_name = info.get("uploader") or "TikTok User"
            author_link = info.get("uploader_url") or user_url
            raw_desc = info.get("description") or info.get("title") or ""
            audio_name = f"{sanitize_filename(author_name)} - tiktok.mp3"
            
            return (author_name, author_link, raw_desc, audio_name, None, vid_bytes, None, [])
        except Exception as yt_e:
            # Якщо і резерв не зміг - тоді вже точно все
            raise Exception(f"Failed all methods. API: {e} | YT-DLP: {yt_e}")


async def handle_twitter(user_url: str):
    """
    VXTwitter -> YT-DLP
    """
    # FIX: Correct Regex
    m = re.search(r"/status/(\d+)", user_url)
    if not m: raise Exception("Не знайдено ID твіта")
    tw_id = m.group(1)
    
    # 1. VX/FX
    for service_domain in ["api.vxtwitter.com", "api.fxtwitter.com"]:
        try:
            api_url = f"https://{service_domain}/Twitter/status/{tw_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as r:
                    if r.status != 200: continue
                    tweet = await r.json()

            author_name = tweet.get("user_name", "User")
            screen_name = tweet.get("user_screen_name", "user")
            author_link = f"https://x.com/{screen_name}"
            raw_desc = tweet.get("text", "")
            audio_name = f"{sanitize_filename(author_name)} - twitter.mp3"

            video_bytes = None
            photo_bytes = None
            gallery_data = []
            media_list = tweet.get("media_extended") or []
            if not media_list and "media_url" in tweet:
                media_list = [{"type": "image", "url": tweet["media_url"]}]

            has_video = any(m.get("type") in ["video", "gif"] for m in media_list)
            if has_video:
                vid = next((m for m in media_list if m.get("type") in ["video", "gif"]), None)
                if vid and vid.get("url"): video_bytes = await download_content(vid["url"])
            else:
                tasks = [download_content(m["url"]) for m in media_list if m.get("url")]
                imgs = await asyncio.gather(*tasks)
                for img in imgs:
                    if img: gallery_data.append((img, "photo"))

            if video_bytes or gallery_data:
                return (author_name, author_link, raw_desc, audio_name, None, video_bytes, photo_bytes, gallery_data)
        except Exception:
            continue

    # 2. YT-DLP Reserve
    try:
        info, vid_bytes = await asyncio.to_thread(ytdlp_download, user_url)
        author_name = info.get("uploader") or "Twitter User"
        author_link = user_url
        raw_desc = info.get("description") or info.get("title") or ""
        audio_name = f"{sanitize_filename(author_name)} - twitter.mp3"
        return (author_name, author_link, raw_desc, audio_name, None, vid_bytes, None, [])
    except Exception as e:
        raise Exception(f"Twitter download failed: {e}")


async def get_instagram_post(user_url: str):
    try:
        m = re.search(r"/(p|reel|reels)/([A-Za-z0-9_\-]+)", user_url)
        if not m: return None
        shortcode = m.group(2)
        def _load(): return instaloader.Post.from_shortcode(INSTA_LOADER.context, shortcode)
        post = await asyncio.to_thread(_load)
        return post
    except Exception as e:
        logging.warning(f"get_instagram_post error: {e}")
        return None # Return None to trigger fallback


async def handle_instagram(user_url: str):
    """
    Instaloader -> YT-DLP (при помилках 401/403)
    """
    # 1. Instaloader (Original Logic)
    try:
        post = await get_instagram_post(user_url)
        if post:
            author_name = post.owner_username or "Instagram"
            author_link = f"https://instagram.com/{author_name}"
            raw_desc = (post.caption or "").split("\n")[0]
            audio_name = f"{sanitize_filename(author_name)}.mp3"

            audio_bytes = None
            video_bytes = None
            photo_bytes = None
            gallery_data = []

            if post.typename == "GraphSidecar":
                nodes = list(post.get_sidecar_nodes())
                async def dl(node):
                    url = node.video_url if node.is_video else node.display_url
                    return await download_content(url), "video" if node.is_video else "photo"
                tasks = [dl(n) for n in nodes]
                results = await asyncio.gather(*tasks)
                for content, ctype in results:
                    if content: gallery_data.append((content, ctype))
            else:
                if post.is_video:
                    url = post.video_url
                    video_bytes = await download_content(url)
                else:
                    url = post.url
                    photo_bytes = await download_content(url)

            return (author_name, author_link, raw_desc, audio_name, audio_bytes, video_bytes, photo_bytes, gallery_data)
        else:
            raise Exception("Instaloader returned None") # Trigger fallback

    except Exception as e:
        logging.warning(f"Instaloader failed ({e}). Switching to Reserve (YT-DLP)...")
        
        # 2. РЕЗЕРВНИЙ МЕТОД: YT-DLP
        # Рятує, коли Instaloader заблокований (403/401)
        try:
            info, vid_bytes = await asyncio.to_thread(ytdlp_download, user_url)
            author_name = info.get("uploader") or "Instagram User"
            author_link = f"https://instagram.com/{author_name}"
            raw_desc = info.get("description") or info.get("title") or ""
            audio_name = f"{sanitize_filename(author_name)}.mp3"
            
            return (author_name, author_link, raw_desc, audio_name, None, vid_bytes, None, [])
        except Exception as yt_e:
            raise Exception(f"Instagram Failed. Main: {e} | Reserve: {yt_e}")


# ==========================================
#  MAIN LOGIC
# ==========================================

async def process_media_request(message: types.Message, user_url: str, clean_mode: bool = False, audio_mode: bool = False, is_button_click: bool = False, force_lang: str = "orig"):
    if not user_url: return

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
        gallery_data = []

        if "tiktok.com" in user_url:
            (author_name, author_link, raw_desc, audio_name, audio_bytes, video_bytes, photo_bytes, gallery_data) = await handle_tiktok(user_url)
        elif "twitter.com" in user_url or "x.com" in user_url:
            (author_name, author_link, raw_desc, audio_name, audio_bytes, video_bytes, photo_bytes, gallery_data) = await handle_twitter(user_url)
        elif "instagram.com" in user_url:
            (author_name, author_link, raw_desc, audio_name, audio_bytes, video_bytes, photo_bytes, gallery_data) = await handle_instagram(user_url)
        else:
            raise Exception("Непідтримуване посилання")

        # --- Audio Mode ---
        if audio_mode:
            if audio_bytes:
                await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))
            elif video_bytes:
                extracted = await asyncio.to_thread(extract_audio_from_video_bytes, video_bytes)
                if extracted: await message.answer_audio(BufferedInputFile(extracted, filename=audio_name))
                else: await message.answer("Не вдалося отримати аудіо 😔")
            else: await message.answer("Не вдалося отримати аудіо 😔")
            if status_msg: await status_msg.delete()
            return

        # --- Text Prep ---
        orig_text, trans_text, has_diff = await prepare_texts(raw_desc)
        text_to_show = orig_text if force_lang == "orig" else trans_text
        caption = format_caption(author_name, author_link, text_to_show, user_url)

        # --- Clean Mode ---
        if clean_mode:
            if video_bytes:
                await message.answer_video(BufferedInputFile(video_bytes, filename="video.mp4"))
            elif photo_bytes:
                await message.answer_photo(BufferedInputFile(photo_bytes, filename="photo.jpg"))
            elif gallery_data:
                for chunk in chunk_list(gallery_data, 10):
                    mg = MediaGroupBuilder()
                    for content, ctype in chunk:
                        if ctype == "video": mg.add_video(BufferedInputFile(content, filename="media.mp4"))
                        else: mg.add_photo(BufferedInputFile(content, filename="photo.jpg"))
                    await message.answer_media_group(mg.build())
            if status_msg: await status_msg.delete()
            return

        # --- Standard Mode ---
        data_id = str(uuid.uuid4())[:8]
        STORAGE[data_id] = {
            "user_url": user_url, "orig_text": orig_text, "trans_text": trans_text, "has_diff": has_diff,
            "author_name": author_name, "author_link": author_link, "audio_name": audio_name,
            "kind": None, "video_file_id": None, "current_lang": force_lang,
            "photo_bytes": photo_bytes, "gallery_data": gallery_data
        }

        if video_bytes and not gallery_data:
            STORAGE[data_id]["kind"] = "video"
            sent = await message.answer_video(
                BufferedInputFile(video_bytes, filename="video.mp4"),
                caption=caption, parse_mode="HTML",
                reply_markup=get_video_keyboard(data_id, current_lang=force_lang),
            )
            STORAGE[data_id]["video_file_id"] = sent.video.file_id
        else:
            STORAGE[data_id]["kind"] = "photo"
            if photo_bytes and not gallery_data:
                await message.answer_photo(BufferedInputFile(photo_bytes, filename="photo.jpg"), caption=caption, parse_mode="HTML")
            else:
                for_idx = 0
                for chunk in chunk_list(gallery_data, 10):
                    mg = MediaGroupBuilder()
                    first_in_chunk = True
                    for content, ctype in chunk:
                        cap = caption if for_idx == 0 and first_in_chunk else None
                        if ctype == "video": mg.add_video(BufferedInputFile(content, filename="media.mp4"), caption=cap, parse_mode="HTML" if cap else None)
                        else: mg.add_photo(BufferedInputFile(content, filename="photo.jpg"), caption=cap, parse_mode="HTML" if cap else None)
                        first_in_chunk = False
                        for_idx += 1
                    await message.answer_media_group(mg.build())
            
            opts_msg = await message.answer("Опції:", reply_markup=get_photo_keyboard(data_id, current_lang=force_lang))
            STORAGE[data_id]["opts_msg_id"] = opts_msg.message_id
            
            if audio_bytes:
                try: await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))
                except Exception: pass

        if status_msg:
            try: await status_msg.delete()
            except Exception: pass

    except Exception as e:
        logging.exception(f"process_media_request error: {e}")
        if status_msg:
            try: await status_msg.edit_text(f"❌ Помилка: {str(e)[:100]}")
            except Exception: pass


# ==========================================
#  CALLBACKS / SERVER
# ==========================================

@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    try:
        parts = callback.data.split(":")
        action = parts[0]
        data_id = parts[1] if len(parts) > 1 else None
        
        if not data_id:
             await callback.answer("Error")
             return

        if action == "vid_clean":
            data = STORAGE.get(data_id)
            if data and data.get("video_file_id"):
                await callback.message.answer_video(data["video_file_id"])
            else:
                await callback.answer("Файл втрачено", show_alert=True)
            await callback.answer()

        elif action == "vid_audio":
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return
            await callback.answer("Витягую аудіо...")
            await process_media_request(callback.message, data["user_url"], audio_mode=True, is_button_click=True)

        elif action == "vid_lang":
            target_lang = parts[2]
            data = STORAGE.get(data_id)
            if not data or not data.get("has_diff"):
                await callback.answer()
                return

            text = data["orig_text"] if target_lang == "orig" else data["trans_text"]
            new_cap = format_caption(data["author_name"], data["author_link"], text, data["user_url"])
            try:
                await bot.edit_message_caption(
                    chat_id=callback.message.chat.id, message_id=callback.message.message_id,
                    caption=new_cap, parse_mode="HTML",
                    reply_markup=get_video_keyboard(data_id, current_lang=target_lang),
                )
                data["current_lang"] = target_lang
            except Exception: pass
            await callback.answer()

        elif action == "pho_clean":
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return
            await process_media_request(callback.message, data["user_url"], clean_mode=True, is_button_click=True, force_lang=data.get("current_lang", "orig"))
            await callback.answer()

        elif action == "pho_lang":
            target_lang = parts[2]
            data = STORAGE.get(data_id)
            if not data or not data.get("has_diff"):
                await callback.answer()
                return
            await resend_photo_post(callback.message, data_id, target_lang)
            data["current_lang"] = target_lang
            try:
                await bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id, message_id=callback.message.message_id,
                    reply_markup=get_photo_keyboard(data_id, current_lang=target_lang),
                )
            except Exception: pass
            await callback.answer()

    except Exception as e:
        logging.exception(f"Callback error: {e}")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Кидай посилання на TikTok / Instagram / X (Twitter).")


@dp.message(F.text.regexp(r"(https?://[^\s]+)"))
async def handle_link(message: types.Message):
    user_url, clean, audio = parse_message_data(message.text or "")
    await process_media_request(message, user_url, clean_mode=clean, audio_mode=audio, force_lang="orig")


async def start_web_server():
    app = web.Application()
    async def handle_root(request): return web.Response(text="Bot is alive!")
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
