import logging
import sys
import os
import asyncio
import re
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import FSInputFile, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.media_group import MediaGroupBuilder
import aiohttp
from aiohttp import web
from deep_translator import GoogleTranslator
from langdetect import detect
import instaloader
import yt_dlp
import static_ffmpeg
import subprocess

# Активуємо FFmpeg
static_ffmpeg.add_paths()

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"
RENDER_URL = "https://tiktok-bot-z88j.onrender.com" 

# Пам'ять для кнопок
LINK_STORAGE = {}

# Дзеркала для YouTube (Cobalt + Piped)
COBALT_MIRRORS = [
    "https://api.cobalt.tools/api/json",
    "https://co.wuk.sh/api/json",
    "https://cobalt.pub/api/json",
    "https://api.succoon.com/api/json"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

if not BOT_TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN у змінних оточення!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
translator = GoogleTranslator(source='auto', target='uk')

# --- КЛАВІАТУРИ ---
def get_media_keyboard(url, content_type='video'):
    link_id = str(uuid.uuid4())[:8]
    LINK_STORAGE[link_id] = url
    buttons = []
    clean_btn = InlineKeyboardButton(text="🙈 Без підписів", callback_data=f"clean:{link_id}")
    if content_type == 'video':
        audio_btn = InlineKeyboardButton(text="🎵 + Аудіо", callback_data=f"audio:{link_id}")
        buttons.append([audio_btn, clean_btn])
    else:
        buttons.append([clean_btn])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def sanitize_filename(name):
    if not name: return "audio"
    name = re.sub(r'[\\/*?:"<>|]', "", str(name))
    name = name.replace('\n', ' ').strip()
    return name[:50]

def parse_message_data(text):
    if not text: return None, False, False, False
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False, False, False
    
    found_url = url_match.group(1)
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text)
    # Шукаємо (t) або (translate)
    toggle_trans = bool(re.search(r'\(t\)', cmd_text) or re.search(r'\(translate\)', cmd_text))
    
    return found_url, clean_mode, audio_mode, toggle_trans

async def download_content(url):
    if not url: return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200: return await response.read()
    except: return None

# 🔥 НОВА ЛОГІКА ПЕРЕКЛАДУ 🔥
async def translate_text_logic(text, toggle_trans=False):
    if not text or not text.strip(): return ""
    try:
        lang = detect(text)
        
        # 1. СТАНДАРТ (без команди (t)):
        # Англійська -> Залишається англійською.
        # Інша мова -> Перекладається на Українську.
        if not toggle_trans:
            if lang == 'en': return text
            else: return await asyncio.to_thread(translator.translate, text)
            
        # 2. З КОМАНДОЮ (t):
        # Англійська -> Перекладається на Українську.
        # Інша мова -> Залишається оригіналом.
        else:
            if lang == 'en': return await asyncio.to_thread(translator.translate, text)
            else: return text 
            
    except: pass
    return text

def format_caption(nickname, profile_url, title, original_url):
    caption = f"👤 <a href='{profile_url}'><b>{nickname}</b></a>\n\n"
    if title: caption += f"📝 {title}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    return caption[:1000] + "..." if len(caption) > 1024 else caption

def extract_audio_from_video(video_bytes):
    try:
        unique = str(uuid.uuid4())
        vid_path = f"temp_vid_{unique}.mp4"
        aud_path = f"temp_aud_{unique}.mp3"
        with open(vid_path, "wb") as f: f.write(video_bytes)
        subprocess.run(['ffmpeg', '-y', '-i', vid_path, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', aud_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(aud_path, "rb") as f: audio_bytes = f.read()
        os.remove(vid_path)
        os.remove(aud_path)
        return audio_bytes
    except: return None

# --- YOUTUBE HELPERS (Cobalt) ---
async def get_youtube_data(user_url):
    payload = {
        "url": user_url,
        "videoQuality": "720",
        "audioFormat": "mp3",
        "filenamePattern": "classic"
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        for mirror in COBALT_MIRRORS:
            try:
                async with session.post(mirror, json=payload, headers=headers, timeout=20) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('status') in ['stream', 'redirect']:
                            return data.get('url')
            except: continue
    return None

# --- TASKS ---
async def keep_alive_ping():
    logging.info("🚀 Ping service started!")
    await asyncio.sleep(10)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL) as response:
                    logging.info(f"🔔 Self-Ping status: {response.status}")
        except: pass
        await asyncio.sleep(120)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
    await site.start()

# ==========================================
# 🔥 ЛОГІКА ОБРОБКИ 🔥
# ==========================================

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, toggle_trans=False, is_button_click=False):
    if not is_button_click:
        status_msg = await message.reply("⏳ Обробляю...")
    else:
        status_msg = None

    try:
        # --- TIKTOK ---
        if "tiktok.com" in user_url:
            async with aiohttp.ClientSession() as session:
                async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as r:
                    data = (await r.json())['data']
            
            caption_text = None
            author_name = data['author']['nickname']
            title_text = data.get('title', '')
            
            if not clean_mode:
                trans = await translate_text_logic(title_text, toggle_trans)
                unique_id = data['author']['unique_id']
                caption_text = format_caption(author_name, f"https://www.tiktok.com/@{unique_id}", trans, user_url)

            music_file = None
            should_dl_audio = audio_mode or ('images' in data and data['images'])
            
            if should_dl_audio:
                mb = await download_content(data.get('music'))
                if mb:
                    m_author = data.get('music_info', {}).get('author', author_name)
                    m_title = data.get('music_info', {}).get('title', 'Audio')
                    fname = f"{sanitize_filename(m_author)} - {sanitize_filename(m_title)}.mp3"
                    music_file = BufferedInputFile(mb, filename=fname)

            if 'images' in data and data['images']:
                tasks = [download_content(u) for u in data['images']]
                imgs = await asyncio.gather(*tasks)
                mg = MediaGroupBuilder()
                for i, img in enumerate(imgs):
                    if img:
                        f = BufferedInputFile(img, filename=f"i{i}.jpg")
                        if i==0 and caption_text: mg.add_photo(f, caption=caption_text, parse_mode="HTML")
                        else: mg.add_photo(f)
                
                await message.answer_media_group(mg.build())
                
                if music_file and not clean_mode:
                    # Аудіо з кнопкою "Чисто"
                    await message.answer_audio(music_file, reply_markup=get_media_keyboard(user_url, 'photo'))
                elif not clean_mode:
                    await message.answer("Опції:", reply_markup=get_media_keyboard(user_url, 'photo'))

            else:
                vid_url = data.get('hdplay') or data.get('play')
                vb = await download_content(vid_url)
                if vb:
                    kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                    await message.answer_video(
                        BufferedInputFile(vb, filename="tiktok.mp4"), 
                        caption=caption_text, 
                        parse_mode="HTML", 
                        reply_markup=kb
                    )
            
            if audio_mode and not ('images' in data) and music_file: 
                await message.answer_audio(music_file, caption="🎵 Звук")

        # --- INSTAGRAM / THREADS / REDDIT (Використовуємо yt-dlp як найнадійніший метод) ---
        elif any(x in user_url for x in ["instagram.com", "threads", "reddit.com", "redd.it"]):
            
            # Налаштування yt-dlp
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'format': 'best', 
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            }

            info = None
            try:
                # Спробуємо yt-dlp (він чудово працює з Threads/Reddit і часто з Insta)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, user_url, download=False)
            except:
                # Якщо yt-dlp не впорався з Інстою, спробуємо Instaloader (резерв)
                if "instagram.com" in user_url:
                    try:
                        shortcode = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url).group(1)
                        L = instaloader.Instaloader(quiet=True)
                        L.context._user_agent = "Instagram 269.0.0.18.75 Android"
                        post = await asyncio.to_thread(instaloader.Post.from_shortcode, L.context, shortcode)
                        # Перетворюємо в формат схожий на yt-dlp
                        info = {
                            'uploader': post.owner_username,
                            'title': post.caption,
                            'url': post.video_url if post.is_video else post.url,
                            'ext': 'mp4' if post.is_video else 'jpg',
                            '_type': 'video' if post.is_video else 'image'
                        }
                        # Галереї тут складно обробляти універсально, тому поки що для простих постів
                        if post.typename == 'GraphSidecar':
                             # Якщо галерея - просто кидаємо помилку щоб юзер знав (поки що)
                             # Або реалізуємо просту логіку
                             pass 
                    except: pass

            if not info:
                raise Exception("Failed to extract data")

            # Обробка даних
            caption_text = None
            author_name = info.get('uploader') or info.get('uploader_id') or "User"
            raw_text = info.get('description') or info.get('title') or ""
            # Для Reddit/Threads часто title це і є текст
            
            if not clean_mode:
                trans = await translate_text_logic(raw_text, toggle_trans)
                
                profile_link = user_url
                if "instagram.com" in user_url: profile_link = f"https://instagram.com/{author_name}"
                elif "threads" in user_url: profile_link = f"https://www.threads.net/@{author_name}"
                elif "reddit" in user_url: profile_link = f"https://reddit.com/user/{author_name}"
                
                caption_text = format_caption(author_name, profile_link, trans, user_url)

            # Завантаження файлу
            media_url = info.get('url')
            content = await download_content(media_url)
            
            if not content: raise Exception("Download failed")

            is_video = info.get('ext') == 'mp4' or info.get('vcodec') != 'none'
            
            if is_video:
                f = BufferedInputFile(content, filename=f"video.mp4")
                if audio_mode and not clean_mode:
                    ab = await asyncio.to_thread(extract_audio_from_video, content)
                    if ab: await message.answer_audio(BufferedInputFile(ab, filename=f"{author_name}.mp3"), caption="🎵 Звук")
                else:
                    kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                    await message.answer_video(f, caption=caption_text, parse_mode="HTML", reply_markup=kb)
            else:
                f = BufferedInputFile(content, filename=f"image.jpg")
                await message.answer_photo(f, caption=caption_text, parse_mode="HTML")
                if not clean_mode: await message.answer("Опції:", reply_markup=get_media_keyboard(user_url, 'photo'))

        # --- TWITTER ---
        elif "twitter.com" in user_url or "x.com" in user_url:
            tw_id = re.search(r"/status/(\d+)", user_url).group(1)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.fxtwitter.com/status/{tw_id}") as r:
                    tweet = (await r.json()).get('tweet', {})
            
            caption_text = None
            text_content = tweet.get('text', '')
            author_name = tweet.get('author', {}).get('name', 'User')
            
            if not clean_mode:
                text = await translate_text_logic(text_content, toggle_trans)
                u = tweet.get('author', {})
                caption_text = format_caption(u.get('name', 'User'), f"https://x.com/{u.get('screen_name')}", text, user_url)

            audio_filename = f"{author_name} - {sanitize_filename(text_content)}.mp3"

            media = tweet.get('media', {}).get('all', [])
            has_video = any(m['type'] in ['video','gif'] for m in media)
            
            if has_video:
                vid = next(m for m in media if m['type'] in ['video','gif'])
                vb = await download_content(vid['url'])
                if vb:
                    if audio_mode and not clean_mode:
                         aud_bytes = await asyncio.to_thread(extract_audio_from_video, vb)
                         if aud_bytes:
                             await message.answer_audio(BufferedInputFile(aud_bytes, filename=audio_filename), caption="🎵 Звук з Twitter")
                    else:
                        kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                        await message.answer_video(BufferedInputFile(vb, filename="tw.mp4"), caption=caption_text, parse_mode="HTML", reply_markup=kb)
            else:
                tasks = [download_content(m['url']) for m in media]
                imgs = await asyncio.gather(*tasks)
                mg = MediaGroupBuilder()
                for i, img in enumerate(imgs):
                    if img:
                        f = BufferedInputFile(img, filename=f"t{i}.jpg")
                        if i==0 and caption_text: mg.add_photo(f, caption=caption_text, parse_mode="HTML")
                        else: mg.add_photo(f)
                await message.answer_media_group(mg.build())
                if not clean_mode: await message.answer("Опції:", reply_markup=get_media_keyboard(user_url, 'photo'))

        # --- YOUTUBE ---
        elif "youtube.com" in user_url or "youtu.be" in user_url:
            if not is_button_click: await status_msg.edit_text("📺 YouTube: Завантажую...")
            
            direct_url = await get_youtube_data(user_url)
            
            if not direct_url: 
                await status_msg.edit_text("❌ Всі сервери зайняті або відео недоступне.")
                return
            
            raw_path = f"video_{uuid.uuid4()}.mp4"
            with open(raw_path, 'wb') as f:
                f.write(await download_content(direct_url))
            
            # Аудіо
            if audio_mode:
                audio_path = f"audio_{uuid.uuid4()}.mp3"
                await asyncio.to_thread(extract_audio_from_video, open(raw_path, 'rb').read()) # Simplified reuse
                # Better reuse the function logic:
                subprocess.run(['ffmpeg', '-y', '-i', raw_path, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await message.answer_audio(FSInputFile(audio_path), caption="🎵 Звук з YouTube")
                if os.path.exists(audio_path): os.remove(audio_path)
            
            # Відео
            else:
                caption_text = None
                if not clean_mode:
                    caption_text = f"📺 <b>YouTube Video</b>\n\n🔗 <a href='{user_url}'>Оригінал</a>"
                
                kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                await message.answer_video(FSInputFile(raw_path), caption=caption_text, parse_mode="HTML", reply_markup=kb)

            if os.path.exists(raw_path): os.remove(raw_path)

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Processing error: {e}")
        if status_msg: await status_msg.edit_text("❌ Сталася помилка. Перевірте посилання.")

# ==========================
# 🎮 ОБРОБНИКИ
# ==========================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я качаю з TikTok, Instagram, Twitter, Threads, Reddit та YouTube.")

@dp.callback_query()
async def on_button_click(callback: CallbackQuery):
    try:
        action, link_id = callback.data.split(":")
        user_url = LINK_STORAGE.get(link_id)
        if not user_url:
            await callback.answer("Посилання застаріло.", show_alert=True)
            return
        await callback.answer("Виконую...")
        if action == "clean":
            await process_media_request(callback.message, user_url, clean_mode=True, is_button_click=True)
        elif action == "audio":
            await process_media_request(callback.message, user_url, audio_mode=True, clean_mode=False, is_button_click=True)
    except: pass

@dp.message(F.text.regexp(r'(https?://[^\s]+)') | F.caption.regexp(r'(https?://[^\s]+)'))
@dp.edited_message(F.text.regexp(r'(https?://[^\s]+)') | F.caption.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    content = message.text or message.caption
    user_url, clean, audio, toggle_trans = parse_message_data(content)
    await process_media_request(message, user_url, clean, audio, toggle_trans)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
