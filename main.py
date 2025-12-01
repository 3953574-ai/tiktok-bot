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

# Кеш для відео (щоб перекладати на льоту)
# key = "chat_id:message_id" -> value = {orig_text, trans_text, author, link, ...}
VIDEO_CACHE = {}

# Пам'ять для посилань (для фото-перезаливу)
LINK_STORAGE = {}

# Дзеркала Cobalt (Тільки для Instagram Fallback)
COBALT_MIRRORS = [
    "https://co.wuk.sh/api/json",
    "https://api.cobalt.tools/api/json",
    "https://cobalt.pub/api/json",
    "https://api.succoon.com/api/json"
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

def get_video_keyboard(cache_key, current_lang='orig'):
    """Кнопки для ВІДЕО (Переклад на льоту)"""
    buttons = [
        [
            InlineKeyboardButton(text="🎵 Аудіо", callback_data=f"vid_audio:{cache_key}"), # Передаємо ключ кешу
            InlineKeyboardButton(text="🎬 Відео", callback_data=f"vid_clean:{cache_key}")
        ]
    ]
    
    # Перевіряємо, чи є сенс у кнопці перекладу
    data = VIDEO_CACHE.get(cache_key)
    if data and data.get('orig_text') != data.get('trans_text'):
        # Якщо зараз оригінал -> показуємо "Перекласти", і навпаки
        btn_text = "🇺🇦 Переклад" if current_lang == 'orig' else "🌐 Оригінал"
        next_lang = 'uk' if current_lang == 'orig' else 'orig'
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"vid_lang:{next_lang}:{cache_key}")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_photo_keyboard(url, current_lang='orig'):
    """Кнопки для ФОТО (Перезалив)"""
    link_id = str(uuid.uuid4())[:8]
    LINK_STORAGE[link_id] = url
    
    buttons = [
        [InlineKeyboardButton(text="🖼 Тільки фото", callback_data=f"pho_clean:{link_id}")]
    ]
    
    # Кнопка перекладу (через resend)
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
    if not text: return None, False, False
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False, False
    
    found_url = url_match.group(1)
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

async def prepare_text_data(text):
    if not text: return "", ""
    try:
        lang = detect(text)
        if lang != 'uk':
            trans = await asyncio.to_thread(translator.translate, text)
            return text, trans # (Оригінал, Переклад)
        return text, text
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

# --- COBALT API (Instagram Backup) ---
async def get_cobalt_data(user_url):
    payload = {"url": user_url}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        for mirror in COBALT_MIRRORS:
            try:
                async with session.post(mirror, json=payload, headers=headers, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('status') in ['stream', 'redirect', 'picker']: return data
            except: continue
    return None

# --- WEB & PING ---
async def keep_alive_ping():
    logging.info("🚀 Ping service started!")
    await asyncio.sleep(10)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL) as response: pass
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

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, force_lang='orig'):
    # force_lang='orig' (за замовчуванням) або 'uk' (якщо натиснули кнопку перекладу фото)
    
    status_msg = None
    # Показуємо статус тільки якщо це не автоматичний перезалив
    if not clean_mode and not audio_mode and message.from_user.id != bot.id:
         status_msg = await message.reply("⏳ ...")

    try:
        final_video_bytes = None
        final_photo_bytes = None
        final_gallery = [] 
        final_audio_bytes = None
        
        author_name = "User"
        author_link = user_url
        raw_desc = ""
        is_video = False
        audio_filename = "audio.mp3"

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
            audio_filename = f"{sanitize_filename(m_author)} - {sanitize_filename(m_title)}.mp3"
            
            mb = await download_content(data.get('music'))
            if mb: final_audio_bytes = mb

            if 'images' in data and data['images']:
                tasks = [download_content(u) for u in data['images']]
                final_gallery = await asyncio.gather(*tasks)
            else:
                is_video = True
                final_video_bytes = await download_content(data.get('hdplay') or data.get('play'))

        # --- TWITTER (Повернув старий робочий метод) ---
        elif "twitter.com" in user_url or "x.com" in user_url:
            match = re.search(r"/status/(\d+)", user_url)
            if not match: raise Exception("No ID")
            tw_id = match.group(1)
            
            # Використовуємо надійний fxtwitter API
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.fxtwitter.com/status/{tw_id}") as r:
                    if r.status != 200: raise Exception("Twitter API Error")
                    tweet = (await r.json()).get('tweet', {})

            author_name = tweet.get('author', {}).get('name', 'User')
            screen_name = tweet.get('author', {}).get('screen_name', 'user')
            author_link = f"https://x.com/{screen_name}"
            raw_desc = tweet.get('text', '')
            audio_filename = f"{author_name} - twitter_audio.mp3"

            media = tweet.get('media', {}).get('all', [])
            has_video_tw = any(m['type'] in ['video','gif'] for m in media)
            
            if has_video_tw:
                is_video = True
                vid = next(m for m in media if m['type'] in ['video','gif'])
                final_video_bytes = await download_content(vid['url'])
            else:
                tasks = [download_content(m['url']) for m in media]
                final_gallery = await asyncio.gather(*tasks)
                if len(final_gallery) == 1:
                    final_photo_bytes = final_gallery[0]
                    final_gallery = []

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
                audio_filename = f"{author_name}.mp3"

                if post.typename == 'GraphSidecar':
                    tasks = []
                    for node in post.get_sidecar_nodes():
                        url = node.video_url if node.is_video else node.display_url
                        tasks.append(download_content(url))
                    final_gallery = await asyncio.gather(*tasks)
                else:
                    is_video = post.is_video
                    url = post.video_url if is_video else post.url
                    content = await download_content(url)
                    if is_video: final_video_bytes = content
                    else: final_photo_bytes = content
                success = True
            except: pass
            
            # 2. Cobalt Fallback
            if not success:
                c_data = await get_cobalt_data(user_url)
                if not c_data: raise Exception("API Error")
                
                author_name = "Instagram User"
                if c_data.get('status') == 'picker':
                    tasks = [download_content(i['url']) for i in c_data['picker']]
                    final_gallery = await asyncio.gather(*tasks)
                else:
                    url = c_data.get('url')
                    content = await download_content(url)
                    is_video = ".mp4" in url or "video" in c_data.get('filename', '')
                    if is_video: final_video_bytes = content
                    else: final_photo_bytes = content

        # --- ВІДПРАВКА ---
        
        orig_text, trans_text = await prepare_text_data(raw_desc)
        
        # 1. Ручний режим (Clean/Audio only)
        if clean_mode:
            if is_video and final_video_bytes:
                await message.answer_video(BufferedInputFile(final_video_bytes, filename="video.mp4"))
            elif final_photo_bytes:
                await message.answer_photo(BufferedInputFile(final_photo_bytes, filename="photo.jpg"))
            elif final_gallery:
                mg = MediaGroupBuilder()
                for i, b in enumerate(final_gallery):
                    if b: mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await message.answer_media_group(mg.build())
            if status_msg: await status_msg.delete()
            return

        if audio_mode:
            if final_audio_bytes:
                await message.answer_audio(BufferedInputFile(final_audio_bytes, filename=audio_filename))
            elif is_video and final_video_bytes:
                ab = await asyncio.to_thread(extract_audio_from_video, final_video_bytes)
                if ab: await message.answer_audio(BufferedInputFile(ab, filename=audio_filename))
            if status_msg: await status_msg.delete()
            return

        # 2. Стандартний режим
        
        # Визначаємо, який текст показувати (оригінал чи переклад)
        text_to_show = trans_text if force_lang == 'uk' else orig_text
        caption = format_caption(author_name, author_link, text_to_show, user_url)
        
        sent_msg = None
        
        # ВІДЕО
        if is_video and final_video_bytes:
            sent_msg = await message.answer_video(
                BufferedInputFile(final_video_bytes, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML"
            )
            # Зберігаємо дані в кеш для "на льоту" редагування
            key = f"{sent_msg.chat.id}:{sent_msg.message_id}"
            VIDEO_CACHE[key] = {
                'orig_text': orig_text,
                'trans_text': trans_text,
                'author': author_name,
                'link': author_link,
                'url': user_url,
                'video_bytes': final_video_bytes,
                'audio_name': audio_filename
            }
            # Кнопки для відео
            await bot.edit_message_reply_markup(
                chat_id=sent_msg.chat.id, message_id=sent_msg.message_id,
                reply_markup=get_video_keyboard(key, force_lang)
            )

        # ФОТО / ГАЛЕРЕЯ
        else:
            if final_photo_bytes:
                await message.answer_photo(
                    BufferedInputFile(final_photo_bytes, filename="photo.jpg"),
                    caption=caption,
                    parse_mode="HTML"
                )
            elif final_gallery:
                mg = MediaGroupBuilder()
                for i, b in enumerate(final_gallery):
                    if i == 0:
                        mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"), caption=caption, parse_mode="HTML")
                    else:
                        mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await message.answer_media_group(mg.build())

            # Окремо аудіо (якщо є)
            if final_audio_bytes:
                await message.answer_audio(BufferedInputFile(final_audio_bytes, filename=audio_filename))
            
            # Кнопки для фото/галереї (ОКРЕМИМ ПОВІДОМЛЕННЯМ ЗНИЗУ)
            # Тому що під галереєю кнопки не чіпляються
            kb = get_photo_keyboard(user_url, force_lang)
            await message.answer("Опції:", reply_markup=kb)

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if status_msg: await status_msg.edit_text("❌ Помилка. Перевірте посилання.")

# --- ОБРОБКА КНОПОК ---
@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    action = callback.data
    
    # --- ВІДЕО КНОПКИ (На льоту) ---
    
    if action.startswith("vid_"):
        _, cmd, key = action.split(":", 2) # vid_lang:uk:key -> cmd=lang, key=uk:key (bug) -> fix split
        
        # Правильний спліт
        parts = action.split(":")
        cmd = parts[1]
        
        # Отримуємо дані з кешу
        # Для toggle_lang ключ буде 3-м елементом
        key = parts[-1] 
        data = VIDEO_CACHE.get(key)
        
        if not data:
            await callback.answer("Застаріло", show_alert=True)
            return

        if cmd == "audio":
            await callback.answer("Витягую...")
            aud = await asyncio.to_thread(extract_audio_from_video, data['video_bytes'])
            if aud: await callback.message.reply_audio(BufferedInputFile(aud, filename=data['audio_name']))
        
        elif cmd == "clean":
            await callback.message.reply_video(BufferedInputFile(data['video_bytes'], filename="video.mp4"))
            await callback.answer()
            
        elif cmd == "lang":
            # vid_lang:uk:key
            lang_code = parts[2]
            real_key = parts[3]
            # data = VIDEO_CACHE.get(real_key) # Вже взяли вище, але ключ може бути іншим
            data = VIDEO_CACHE.get(real_key)
            
            text_to_show = data['trans_text'] if lang_code == 'uk' else data['orig_text']
            new_caption = format_caption(data['author'], data['link'], text_to_show, data['url'])
            
            await bot.edit_message_caption(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                caption=new_caption,
                parse_mode="HTML",
                reply_markup=get_video_keyboard(real_key, lang_code)
            )
            await callback.answer()

    # --- ФОТО КНОПКИ (Перезалив) ---
    
    elif action.startswith("pho_"):
        parts = action.split(":")
        cmd = parts[1]
        link_id = parts[-1]
        user_url = LINK_STORAGE.get(link_id)
        
        if not user_url:
            await callback.answer("Посилання застаріло", show_alert=True)
            return
            
        if cmd == "clean":
            await process_media_request(callback.message, user_url, clean_mode=True)
            await callback.message.delete() # Видаляємо меню опцій
            await callback.answer()
            
        elif cmd == "resend":
            # pho_resend:uk:id
            lang_code = parts[2]
            await callback.message.delete() # Видаляємо старе меню
            # Запускаємо процес наново з примусовою мовою
            await process_media_request(callback.message, user_url, force_lang=lang_code)
            await callback.answer()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Кидай посилання на TikTok, Instagram або Twitter.")

@dp.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    user_url, clean, audio = parse_message_data(message.text)
    await process_media_request(message, user_url, clean, audio)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
