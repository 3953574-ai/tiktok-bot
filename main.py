import logging
import sys
import os
import asyncio
import re
import uuid
import glob
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
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

# Кеш
STORAGE = {}

# Дзеркала Cobalt (Розширений список)
COBALT_MIRRORS = [
    "https://api.cobalt.tools/api/json",
    "https://co.wuk.sh/api/json",
    "https://cobalt.pub/api/json",
    "https://api.succoon.com/api/json",
    "https://cobalt.zip/api/json",
    "https://cobalt.xy24.eu/api/json"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

if not BOT_TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
translator = GoogleTranslator(source='auto', target='uk')

# --- КЛАВІАТУРИ ---

def get_video_keyboard(data_id, current_mode='trans'):
    buttons = [
        [
            InlineKeyboardButton(text="🎵 + Аудіо", callback_data=f"vid_audio:{data_id}"),
            InlineKeyboardButton(text="🎬 Відео", callback_data=f"vid_clean:{data_id}")
        ]
    ]
    data = STORAGE.get(data_id)
    if data and data.get('has_diff'):
        if current_mode == 'trans':
            btn = InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"vid_lang:orig:{data_id}")
        else:
            btn = InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"vid_lang:trans:{data_id}")
        buttons.append([btn])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_photo_keyboard(data_id, current_mode='trans'):
    buttons = [
        [InlineKeyboardButton(text="🖼️ Тільки фото", callback_data=f"pho_clean:{data_id}")]
    ]
    data = STORAGE.get(data_id)
    if data and data.get('has_diff'):
        if current_mode == 'trans':
            btn = InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"pho_resend:orig:{data_id}")
        else:
            btn = InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"pho_resend:trans:{data_id}")
        buttons.append([btn])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def sanitize_filename(name):
    if not name: return "audio"
    name = re.sub(r'[\\/*?:"<>|]', "", str(name))
    name = name.replace('\n', ' ').strip()
    return name[:50]

def parse_message_data(text):
    if not text: return None, False, False
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False, False
    
    found_url = url_match.group(1)
    if "threads.com" in found_url: found_url = found_url.replace("threads.com", "threads.net")
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text)
    
    return found_url, clean_mode, audio_mode

async def download_content(url):
    if not url: return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200: return await response.read()
    except: return None

async def prepare_texts(text):
    if not text: return "", "", False
    try:
        lang = detect(text)
        if lang != 'uk':
            trans = await asyncio.to_thread(translator.translate, text)
            return text, trans, True
        return text, text, False
    except:
        return text, text, False

def format_caption(author_name, author_url, text, original_url):
    caption = f"👤 <a href='{author_url}'><b>{author_name}</b></a>\n\n"
    if text: caption += f"📝 {text}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    return caption[:1024]

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

# --- ЗАВАНТАЖУВАЧІ ---

async def get_cobalt_data(user_url, is_youtube=False):
    payload = {"url": user_url}
    if is_youtube:
        payload.update({"videoQuality":"720","youtubeVideoCodec":"h264","audioFormat":"mp3","filenamePattern":"classic"})
    
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        for mirror in COBALT_MIRRORS:
            try:
                async with session.post(mirror, json=payload, headers=headers, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'error' in data: continue
                        if data.get('status') in ['stream', 'redirect', 'picker']: return data
            except: continue
    return None

async def download_instagram_fallback(user_url):
    """Запасний варіант через yt-dlp, якщо Cobalt не працює"""
    if not os.path.exists("downloads"): os.makedirs("downloads")
    
    # Емулюємо iPhone
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        'outtmpl': f'downloads/%(id)s.%(ext)s',
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, user_url, download=True)
            
            file_id = info['id']
            files = glob.glob(f"downloads/{file_id}*")
            if not files: return None, None, None
            
            file_path = files[0]
            with open(file_path, 'rb') as f:
                content = f.read()
            
            os.remove(file_path)
            
            is_video = info.get('ext') == 'mp4' or info.get('vcodec') != 'none'
            # yt-dlp для інсти зазвичай качає тільки 1 файл, галереї він погано підтримує
            # Тому повертаємо як список з 1 елемента
            return [(content, 'video' if is_video else 'photo')], info.get('uploader'), info.get('description')
    except Exception as e:
        logging.error(f"Fallback yt-dlp failed: {e}")
        return None, None, None

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
# 🔥 MAIN LOGIC 🔥
# ==========================================

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, is_button_click=False, force_lang='trans'):
    
    if not clean_mode and not audio_mode and not is_button_click and message.from_user.id != bot.id:
        status_msg = await message.reply("⏳ Обробляю...")
    else:
        status_msg = None

    try:
        video_bytes = None
        photo_bytes = None
        gallery_data = [] 
        audio_bytes = None
        
        author_name = "User"
        author_link = user_url
        raw_desc = ""
        audio_name = "audio.mp3"
        
        # --- TIKTOK ---
        if "tiktok.com" in user_url:
            async with aiohttp.ClientSession() as session:
                async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as r:
                    data = (await r.json())['data']
            
            author_name = data['author']['nickname']
            unique_id = data['author']['unique_id']
            author_link = f"https://www.tiktok.com/@{unique_id}"
            raw_desc = data.get('title', '')
            
            m_author = data.get('music_info', {}).get('author', author_name)
            m_title = data.get('music_info', {}).get('title', 'Audio')
            audio_name = f"{sanitize_filename(m_author)} - {sanitize_filename(m_title)}.mp3"
            
            mb = await download_content(data.get('music'))
            if mb: audio_bytes = mb

            if 'images' in data and data['images']:
                tasks = [download_content(u) for u in data['images']]
                imgs = await asyncio.gather(*tasks)
                for img in imgs:
                    if img: gallery_data.append((img, 'photo'))
            else:
                video_bytes = await download_content(data.get('hdplay') or data.get('play'))

        # --- TWITTER ---
        elif "twitter.com" in user_url or "x.com" in user_url:
            match = re.search(r"/status/(\d+)", user_url)
            if not match: raise Exception("No ID")
            tw_id = match.group(1)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.vxtwitter.com/Twitter/status/{tw_id}") as r:
                    if r.status != 200: raise Exception("Twitter API Error")
                    tweet = await r.json()

            author_name = tweet.get('user_name', 'User')
            screen_name = tweet.get('user_screen_name', 'user')
            author_link = f"https://x.com/{screen_name}"
            raw_desc = tweet.get('text', '')
            audio_name = f"{author_name} - twitter.mp3"

            media_list = tweet.get('media_extended', [])
            if not media_list and 'media_url' in tweet: media_list = [{'type': 'image', 'url': tweet['media_url']}]

            has_video_tw = any(m['type'] in ['video','gif'] for m in media_list)
            if has_video_tw:
                vid = next(m for m in media_list if m['type'] in ['video','gif'])
                video_bytes = await download_content(vid['url'])
            else:
                tasks = [download_content(m['url']) for m in media_list]
                imgs = await asyncio.gather(*tasks)
                for img in imgs:
                    if img: gallery_data.append((img, 'photo'))

        # --- INSTAGRAM / THREADS / REDDIT / YOUTUBE ---
        elif any(x in user_url for x in ["instagram.com", "threads", "reddit.com", "redd.it", "youtube.com", "youtu.be"]):
            
            is_yt = "youtube.com" in user_url or "youtu.be" in user_url
            
            # 1. Метадані через Instaloader (тільки для Інсти)
            if "instagram.com" in user_url:
                try:
                    shortcode = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url).group(1)
                    def get_meta():
                        L = instaloader.Instaloader(quiet=True)
                        L.context._user_agent = "Instagram 269.0.0.18.75 Android"
                        return instaloader.Post.from_shortcode(L.context, shortcode)
                    post = await asyncio.to_thread(get_meta)
                    author_name = post.owner_username
                    author_link = f"https://instagram.com/{author_name}"
                    raw_desc = (post.caption or "").split('\n')[0]
                    audio_name = f"{author_name}.mp3"
                except: pass

            # 2. Спроба скачати через Cobalt (Пріоритет)
            c_data = await get_cobalt_data(user_url, is_youtube=is_yt)
            
            # 3. Якщо Cobalt не впорався -> Fallback (yt-dlp)
            if not c_data and "instagram.com" in user_url:
                logging.warning("Cobalt failed for Insta. Switching to yt-dlp fallback...")
                gallery_data, fb_author, fb_desc = await download_instagram_fallback(user_url)
                if gallery_data:
                    if fb_author: author_name = fb_author
                    if fb_desc: raw_desc = fb_desc
                    # Розбираємо результат
                    if len(gallery_data) == 1:
                        content, ctype = gallery_data[0]
                        if ctype == 'video': video_bytes = content
                        else: photo_bytes = content
                        gallery_data = [] # Очищаємо, бо вже розібрали
                    # Якщо більше - залишаємо в gallery_data
                else:
                    raise Exception("All methods failed")
            
            elif not c_data:
                raise Exception("API Error")
            
            # Якщо Cobalt спрацював
            elif c_data:
                if author_name == "User" and not is_yt:
                    if "reddit" in user_url: author_name = "Reddit"
                    elif "threads" in user_url: author_name = "Threads"
                    elif "instagram" in user_url: author_name = "Instagram"
                    elif "youtube" in user_url: author_name = "YouTube"
                    
                    audio_name = "audio.mp3"

                if c_data.get('status') == 'picker':
                    tasks = []
                    types_list = []
                    for item in c_data['picker']:
                        if item.get('type') == 'audio':
                            audio_bytes = await download_content(item['url'])
                            continue
                        tasks.append(download_content(item['url']))
                        types_list.append('video' if item.get('type') == 'video' else 'photo')
                    
                    files = await asyncio.gather(*tasks)
                    for i, f in enumerate(files):
                        if f: gallery_data.append((f, types_list[i]))

                else:
                    url = c_data.get('url')
                    content = await download_content(url)
                    is_vid = ".mp4" in url or "video" in c_data.get('filename', '')
                    if is_vid: video_bytes = content
                    else: photo_bytes = content

        # --- ВІДПРАВКА ---
        
        orig_text, trans_text, has_diff = await prepare_texts(raw_desc)
        
        # 1. CLEAN MODE
        if clean_mode:
            if video_bytes: await message.answer_video(BufferedInputFile(video_bytes, filename="video.mp4"))
            elif photo_bytes: await message.answer_photo(BufferedInputFile(photo_bytes, filename="photo.jpg"))
            elif gallery_data:
                chunks = [gallery_data[i:i + 10] for i in range(0, len(gallery_data), 10)]
                for chunk in chunks:
                    mg = MediaGroupBuilder()
                    for content, ctype in chunk:
                        if ctype == 'video': mg.add_video(BufferedInputFile(content, filename="vid.mp4"))
                        else: mg.add_photo(BufferedInputFile(content, filename="img.jpg"))
                    await message.answer_media_group(mg.build())
            
            if status_msg: await status_msg.delete()
            return

        # 2. AUDIO MODE
        if audio_mode:
            if audio_bytes:
                await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))
            elif video_bytes:
                ab = await asyncio.to_thread(extract_audio_from_video, video_bytes)
                if ab: await message.answer_audio(BufferedInputFile(ab, filename=audio_name))
            if status_msg: await status_msg.delete()
            return

        # 3. STANDARD MODE
        text_to_show = trans_text if force_lang == 'trans' else orig_text
        caption = format_caption(author_name, author_link, text_to_show, user_url)
        
        data_id = str(uuid.uuid4())[:8]
        STORAGE[data_id] = {
            'orig_text': orig_text,
            'trans_text': trans_text,
            'has_diff': has_diff,
            'author_name': author_name,
            'author_link': author_link,
            'user_url': user_url,
            'video_bytes': video_bytes, 
            'audio_name': audio_name,
            'file_id': None
        }

        # --- ВІДЕО ---
        if video_bytes:
            sent = await message.answer_video(
                BufferedInputFile(video_bytes, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML"
            )
            STORAGE[data_id]['file_id'] = sent.video.file_id
            await bot.edit_message_reply_markup(
                chat_id=sent.chat.id, message_id=sent.message_id,
                reply_markup=get_video_keyboard(data_id, current_mode=force_lang)
            )

        # --- ОДНЕ ФОТО ---
        elif photo_bytes:
            sent = await message.answer_photo(
                BufferedInputFile(photo_bytes, filename="photo.jpg"),
                caption=caption,
                parse_mode="HTML"
            )
            kb = get_photo_keyboard(data_id, current_mode=force_lang)
            await bot.edit_message_reply_markup(
                chat_id=sent.chat.id, message_id=sent.message_id,
                reply_markup=kb
            )

        # --- ГАЛЕРЕЯ ---
        elif gallery_data:
            chunks = [gallery_data[i:i + 10] for i in range(0, len(gallery_data), 10)]
            
            for i, chunk in enumerate(chunks):
                mg = MediaGroupBuilder()
                for j, (content, ctype) in enumerate(chunk):
                    cap = caption if (i == 0 and j == 0) else None
                    if ctype == 'video': mg.add_video(BufferedInputFile(content, filename="v.mp4"), caption=cap, parse_mode="HTML")
                    else: mg.add_photo(BufferedInputFile(content, filename="p.jpg"), caption=cap, parse_mode="HTML")
                
                await message.answer_media_group(mg.build())
            
            kb = get_photo_keyboard(data_id, current_mode=force_lang)
            
            if audio_bytes:
                await message.answer_audio(
                    BufferedInputFile(audio_bytes, filename=audio_name),
                    reply_markup=kb
                )
            else:
                await message.answer("⠀", reply_markup=kb)

        if (photo_bytes) and audio_bytes:
            await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if status_msg: await status_msg.edit_text("❌ Помилка завантаження (сервіс блокує).")

# --- HANDLERS ---

@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    try:
        parts = callback.data.split(":")
        action = parts[0]
        
        if action == "vid_clean":
            data_id = parts[1]
            data = STORAGE.get(data_id)
            if data and data.get('file_id'):
                await callback.message.answer_video(data['file_id']) 
            else:
                await callback.answer("Файл втрачено", show_alert=True)
            await callback.answer()

        elif action == "vid_audio":
            data_id = parts[1]
            data = STORAGE.get(data_id)
            if data:
                await callback.answer("Витягую аудіо...")
                await process_media_request(callback.message, data['user_url'], audio_mode=True, is_button_click=True)
            else:
                await callback.answer("Застаріло", show_alert=True)

        elif action == "vid_lang":
            target_lang = parts[1] 
            data_id = parts[2]
            data = STORAGE.get(data_id)
            if data:
                text = data['trans_text'] if target_lang == 'trans' else data['orig_text']
                new_cap = format_caption(data['author_name'], data['author_link'], text, data['user_url'])
                try:
                    await bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                        caption=new_cap, parse_mode="HTML",
                        reply_markup=get_video_keyboard(data_id, current_mode=target_lang)
                    )
                except: pass
            await callback.answer()

        elif action == "pho_clean":
            data_id = parts[1]
            data = STORAGE.get(data_id)
            if data:
                await process_media_request(callback.message, data['user_url'], clean_mode=True, is_button_click=True)
            else:
                await callback.answer("Застаріло", show_alert=True)
            await callback.message.delete() 

        elif action == "pho_resend":
            target_lang = parts[1]
            data_id = parts[2]
            data = STORAGE.get(data_id)
            if data:
                await callback.message.delete() 
                await process_media_request(callback.message, data['user_url'], force_lang=target_lang, is_button_click=True)
            else:
                await callback.answer("Застаріло", show_alert=True)

    except Exception as e:
        logging.error(f"CB Error: {e}")
        await callback.answer()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Кидай посилання.")

@dp.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    user_url, clean, audio = parse_message_data(message.text)
    await process_media_request(message, user_url, clean, audio, force_lang='trans')

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
