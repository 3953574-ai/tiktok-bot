import logging
import sys
import os
import asyncio
import re
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto, InputMediaVideo
from aiogram.utils.media_group import MediaGroupBuilder
import aiohttp
from aiohttp import web
from deep_translator import GoogleTranslator
from langdetect import detect
import instaloader
import static_ffmpeg
import subprocess

# Активуємо FFmpeg
static_ffmpeg.add_paths()

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"
RENDER_URL = "https://tiktok-bot-z88j.onrender.com" 

# Кеш тільки для метаданих
STORAGE = {}

COBALT_MIRRORS = [
    "https://co.wuk.sh/api/json",
    "https://api.cobalt.tools/api/json",
    "https://cobalt.pub/api/json",
    "https://api.succoon.com/api/json",
    "https://cobalt.zip/api/json"
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

def get_video_keyboard(data_id, current_lang='orig'):
    buttons = [
        [
            InlineKeyboardButton(text="🎵 Аудіо", callback_data=f"vid_audio:{data_id}"),
            InlineKeyboardButton(text="🙈 Без підписів", callback_data=f"vid_clean:{data_id}")
        ]
    ]
    data = STORAGE.get(data_id)
    # Кнопка перекладу тільки якщо є різниця в тексті
    if data and data.get('orig_text') and data.get('orig_text') != data.get('trans_text'):
        btn_text = "🇺🇦 Переклад" if current_lang == 'orig' else "🌐 Оригінал"
        next_lang = 'uk' if current_lang == 'orig' else 'orig'
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"vid_lang:{next_lang}:{data_id}")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_photo_keyboard(user_url, current_lang='orig'):
    # Для фото зберігаємо лінк для перезаливу
    link_id = str(uuid.uuid4())[:8]
    STORAGE[link_id] = {'user_url': user_url}
    
    buttons = [
        [InlineKeyboardButton(text="🙈 Тільки фото", callback_data=f"pho_clean:{link_id}")]
    ]
    # Кнопка перекладу (через перезалив)
    if current_lang == 'orig':
        buttons.append([InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"pho_resend:uk:{link_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"pho_resend:orig:{link_id}")])

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
    if "threads.com" in found_url: found_url = found_url.replace("threads.com", "threads.net")
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text)
    toggle_trans = bool(re.search(r'\b(t|translate)\b', cmd_text))
    
    return found_url, clean_mode, audio_mode, toggle_trans

async def download_content(url):
    if not url: return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200: return await response.read()
    except: return None

async def prepare_text_data(text, toggle_trans):
    if not text: return "", ""
    try:
        lang = detect(text)
        if not toggle_trans:
            trans = text if lang == 'en' else await asyncio.to_thread(translator.translate, text)
        else:
            trans = await asyncio.to_thread(translator.translate, text) if lang == 'en' else text
        return text, trans
    except:
        return text, text

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

# --- COBALT API ---
async def get_cobalt_data(user_url):
    payload = {"url": user_url}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        for mirror in COBALT_MIRRORS:
            try:
                async with session.post(mirror, json=payload, headers=headers, timeout=20) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('status') in ['stream', 'redirect', 'picker']: return data
            except: continue
    return None

# --- PING ---
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
# 🔥 ГОЛОВНА ЛОГІКА 🔥
# ==========================================

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, toggle_trans=False, force_lang='orig', is_button_click=False):
    
    # Не показуємо "Обробляю" при натисканні кнопок або при автоматичному репості бота
    status_msg = None
    if not clean_mode and not audio_mode and not is_button_click and message.from_user.id != bot.id:
        status_msg = await message.reply("⏳ Обробляю...")

    try:
        video_bytes = None
        photo_bytes = None
        gallery_bytes = [] 
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
                gallery_bytes = await asyncio.gather(*tasks)
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
            if not media_list and 'media_url' in tweet:
                media_list = [{'type': 'image', 'url': tweet['media_url']}]

            has_video_tw = any(m['type'] in ['video','gif'] for m in media_list)
            
            if has_video_tw:
                vid = next(m for m in media_list if m['type'] in ['video','gif'])
                video_bytes = await download_content(vid['url'])
            else:
                tasks = [download_content(m['url']) for m in media_list]
                gallery_bytes = await asyncio.gather(*tasks)
                if len(gallery_bytes) == 1:
                    photo_bytes = gallery_bytes[0]
                    gallery_bytes = []

        # --- INSTAGRAM ---
        elif "instagram.com" in user_url:
            success = False
            # 1. Instaloader
            try:
                shortcode = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url).group(1)
                def get_insta():
                    L = instaloader.Instaloader(quiet=True)
                    L.context._user_agent = "Instagram 269.0.0.18.75 Android"
                    return instaloader.Post.from_shortcode(L.context, shortcode)
                post = await asyncio.to_thread(get_insta)
                author_name = post.owner_username
                author_link = f"https://instagram.com/{author_name}"
                raw_desc = (post.caption or "").split('\n')[0]
                audio_name = f"{author_name}.mp3"

                if post.typename == 'GraphSidecar':
                    tasks = []
                    for node in post.get_sidecar_nodes():
                        url = node.video_url if node.is_video else node.display_url
                        tasks.append(download_content(url))
                    gallery_bytes = await asyncio.gather(*tasks)
                else:
                    url = post.video_url if post.is_video else post.url
                    content = await download_content(url)
                    if post.is_video: video_bytes = content
                    else: photo_bytes = content
                success = True
            except: pass
            
            # 2. Cobalt Fallback
            if not success:
                c_data = await get_cobalt_data(user_url)
                if not c_data: raise Exception("API Error")
                author_name = "Instagram User"
                if c_data.get('status') == 'picker':
                    tasks = [download_content(i['url']) for i in c_data['picker']]
                    gallery_bytes = await asyncio.gather(*tasks)
                else:
                    url = c_data.get('url')
                    content = await download_content(url)
                    is_vid = ".mp4" in url or "video" in c_data.get('filename', '')
                    if is_vid: video_bytes = content
                    else: photo_bytes = content

        # --- ВІДПРАВКА ---
        
        orig_text, trans_text = await prepare_text_data(raw_desc, toggle_trans)
        
        # 1. CLEAN MODE
        if clean_mode:
            if video_bytes: await message.answer_video(BufferedInputFile(video_bytes, filename="video.mp4"))
            elif photo_bytes: await message.answer_photo(BufferedInputFile(photo_bytes, filename="photo.jpg"))
            elif gallery_bytes:
                mg = MediaGroupBuilder()
                for i, b in enumerate(gallery_bytes): mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
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
        text_to_show = trans_text if force_lang == 'uk' else orig_text
        caption = format_caption(author_name, author_link, text_to_show, user_url)
        
        # --- ВІДЕО ---
        if video_bytes:
            data_id = str(uuid.uuid4())[:8]
            STORAGE[data_id] = {
                'orig_text': orig_text,
                'trans_text': trans_text,
                'author_name': author_name,
                'author_link': author_link,
                'user_url': user_url,
                'video_bytes': video_bytes, 
                'audio_name': audio_name
            }
            await message.answer_video(
                BufferedInputFile(video_bytes, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_video_keyboard(data_id, force_lang)
            )

        # --- ОДНЕ ФОТО ---
        elif photo_bytes:
            await message.answer_photo(
                BufferedInputFile(photo_bytes, filename="photo.jpg"),
                caption=caption,
                parse_mode="HTML"
            )
            kb = get_photo_keyboard(user_url, force_lang)
            await message.answer("Опції:", reply_markup=kb)

        # --- ГАЛЕРЕЯ ---
        elif gallery_bytes:
            mg = MediaGroupBuilder()
            for i, b in enumerate(gallery_bytes):
                cap = caption if i == 0 else None
                mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"), caption=cap, parse_mode="HTML")
            await message.answer_media_group(mg.build())
            
            kb = get_photo_keyboard(user_url, force_lang)
            await message.answer("Опції:", reply_markup=kb)

        # Авто-аудіо (для фото/галерей)
        if (photo_bytes or gallery_bytes) and audio_bytes:
            await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if status_msg: await status_msg.edit_text("❌ Помилка завантаження.")

# --- ОБРОБКА КНОПОК ---
@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    try:
        action, data_id = callback.data.split(":")[:2]
        
        # 1. ВІДЕО КНОПКИ
        if action.startswith("vid_"):
            data = STORAGE.get(data_id)
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return
            
            if action == "vid_clean":
                await callback.message.reply_video(BufferedInputFile(data['video_bytes'], filename="video.mp4"))
                await callback.answer()
                
            elif action == "vid_audio":
                await callback.answer("Витягую аудіо...")
                # Тут важливо передати is_button_click=True
                await process_media_request(callback.message, data['user_url'], audio_mode=True, is_button_click=True)
                
            elif action == "vid_lang":
                target_lang = callback.data.split(":")[2]
                text = data['trans_text'] if target_lang == 'uk' else data['orig_text']
                new_cap = format_caption(data['author_name'], data['author_link'], text, data['user_url'])
                try:
                    await bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                        caption=new_cap, parse_mode="HTML",
                        reply_markup=get_video_keyboard(data_id, target_lang)
                    )
                except: pass
                await callback.answer()

        # 2. ФОТО КНОПКИ
        elif action.startswith("pho_"):
            data = STORAGE.get(data_id) # Тут лежить тільки {'user_url': ...}
            if not data:
                await callback.answer("Застаріло", show_alert=True)
                return
            
            if action == "pho_clean":
                await process_media_request(callback.message, data['user_url'], clean_mode=True, is_button_click=True)
                await callback.message.delete() # Видаляємо меню
                await callback.answer()
                
            elif action.startswith("pho_resend"):
                target_lang = callback.data.split(":")[2]
                await callback.message.delete()
                # Перезаливаємо з новою мовою
                await process_media_request(callback.message, data['user_url'], force_lang=target_lang, is_button_click=True)
                await callback.answer()

    except Exception as e:
        logging.error(f"Callback Error: {e}")
        await callback.answer("Помилка", show_alert=True)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Кидай посилання на TikTok, Instagram або Twitter.")

@dp.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    user_url, clean, audio, toggle_trans = parse_message_data(message.text)
    await process_media_request(message, user_url, clean, audio, toggle_trans)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
