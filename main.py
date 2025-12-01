import logging
import sys
import os
import asyncio
import re
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
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

# Пам'ять для посилань (щоб працювала кнопка "Перезавантажити з перекладом")
LINK_STORAGE = {}

# Дзеркала Cobalt (Instagram Fallback)
COBALT_MIRRORS = [
    "https://co.wuk.sh/api/json",
    "https://api.cobalt.tools/api/json",
    "https://cobalt.pub/api/json",
    "https://api.succoon.com/api/json"
]

# Кеш текстів (для відео, де переклад на льоту)
VIDEO_CACHE = {}

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
            InlineKeyboardButton(text="🎵 Аудіо", callback_data="get_audio"),
            InlineKeyboardButton(text="🎬 Відео", callback_data="get_clean")
        ]
    ]
    # Додаємо кнопку перекладу, якщо є текст
    data = VIDEO_CACHE.get(cache_key)
    if data and data.get('orig_text') != data.get('trans_text'):
        btn_text = "🇺🇦 Переклад" if current_lang == 'orig' else "🌐 Оригінал"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data="toggle_lang_video")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_photo_keyboard(url, current_lang='orig'):
    """Кнопки для ФОТО (Переклад через перезалив)"""
    link_id = str(uuid.uuid4())[:8]
    LINK_STORAGE[link_id] = url
    
    buttons = [
        [InlineKeyboardButton(text="🖼 Тільки фото", callback_data=f"clean_photo:{link_id}")]
    ]
    
    # Кнопка перекладу (resend)
    if current_lang == 'orig':
        buttons.append([InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"resend:uk:{link_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"resend:orig:{link_id}")])
        
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

async def prepare_text_data(text, target_lang='orig'):
    if not text: return "", ""
    try:
        lang = detect(text)
        trans_text = text
        # Якщо ціль - оригінал, повертаємо як є
        # Але нам треба мати обидва варіанти для кешу
        if lang != 'uk':
            trans_text = await asyncio.to_thread(translator.translate, text)
        
        return text, trans_text
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

# --- TASKS ---
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
# 🔥 ГОЛОВНА ЛОГІКА ОБРОБКИ 🔥
# ==========================================

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, force_lang='orig'):
    # force_lang: 'orig' (за замовчуванням) або 'uk' (якщо натиснули перекласти)
    
    # Не показуємо "Обробляю" якщо це перезалив (редагування)
    status_msg = None
    if not clean_mode and not audio_mode:
        status_msg = await message.answer("⏳ ...") # Коротке повідомлення, щоб не спамити

    try:
        final_video = None
        final_photo = None
        final_gallery = [] 
        final_audio = None
        
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
            if mb: final_audio = mb

            if 'images' in data and data['images']:
                tasks = [download_content(u) for u in data['images']]
                final_gallery = await asyncio.gather(*tasks)
            else:
                is_video = True
                final_video = await download_content(data.get('hdplay') or data.get('play'))

        # --- INSTAGRAM ---
        elif "instagram.com" in user_url:
            success = False
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
                    if is_video: final_video = content
                    else: final_photo = content
                success = True
            except: pass
            
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
                    if is_video: final_video = content
                    else: final_photo = content

        # --- TWITTER (X) Fix ---
        elif "twitter.com" in user_url or "x.com" in user_url:
            # Використовуємо api.vxtwitter.com (він надійніший за fxtwitter)
            match = re.search(r"/status/(\d+)", user_url)
            if not match: raise Exception("No ID")
            tw_id = match.group(1)
            
            async with aiohttp.ClientSession() as session:
                # Звертаємось до vxtwitter API
                async with session.get(f"https://api.vxtwitter.com/Twitter/status/{tw_id}") as r:
                    if r.status != 200: raise Exception("Twitter API Error")
                    tweet = await r.json()

            author_name = tweet.get('user_name', 'User')
            screen_name = tweet.get('user_screen_name', 'user')
            author_link = f"https://x.com/{screen_name}"
            raw_desc = tweet.get('text', '')
            audio_filename = f"{author_name} - twitter_audio.mp3"

            media_list = tweet.get('media_extended', [])
            
            # Якщо медіа немає в extended, шукаємо в звичайному
            if not media_list and 'media_url' in tweet:
                 # Спрощена логіка для одиночних
                 pass # VXTwitter зазвичай дає extended

            has_video_tw = any(m['type'] in ['video','gif'] for m in media_list)
            
            if has_video_tw:
                is_video = True
                # Беремо перше відео
                vid = next(m for m in media_list if m['type'] in ['video','gif'])
                final_video = await download_content(vid['url'])
            else:
                # Галерея
                tasks = [download_content(m['url']) for m in media_list]
                final_gallery = await asyncio.gather(*tasks)
                if len(final_gallery) == 1:
                    final_photo = final_gallery[0]
                    final_gallery = []

        # --- ОБРОБКА ТЕКСТУ ---
        orig_text, trans_text = await prepare_text_data(raw_desc)
        
        # Вибираємо текст для відображення
        text_to_show = trans_text if force_lang == 'uk' else orig_text
        
        # --- РУЧНИЙ РЕЖИМ ---
        if clean_mode:
            if is_video and final_video:
                await message.answer_video(BufferedInputFile(final_video, filename="video.mp4"))
            elif final_photo:
                await message.answer_photo(BufferedInputFile(final_photo, filename="photo.jpg"))
            elif final_gallery:
                mg = MediaGroupBuilder()
                for i, b in enumerate(final_gallery):
                    if b: mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await message.answer_media_group(mg.build())
            if status_msg: await status_msg.delete()
            return

        if audio_mode:
            if final_audio:
                await message.answer_audio(BufferedInputFile(final_audio, filename=audio_filename))
            elif is_video and final_video:
                ab = await asyncio.to_thread(extract_audio_from_video, final_video)
                if ab: await message.answer_audio(BufferedInputFile(ab, filename=audio_filename))
            if status_msg: await status_msg.delete()
            return

        # --- СТАНДАРТНИЙ РЕЖИМ ---
        caption = format_caption(author_name, author_link, text_to_show, user_url)
        sent_msg = None
        
        # 1. ВІДЕО (Переклад на льоту)
        if is_video and final_video:
            sent_msg = await message.answer_video(
                BufferedInputFile(final_video, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML"
            )
            # Зберігаємо для на-льотного редагування
            key = f"{sent_msg.chat.id}:{sent_msg.message_id}"
            VIDEO_CACHE[key] = {
                'orig_text': orig_text,
                'trans_text': trans_text,
                'author': author_name,
                'link': author_link,
                'url': user_url,
                'video_bytes': final_video, # Для кнопки Аудіо/Clean
                'audio_name': audio_filename
            }
            await bot.edit_message_reply_markup(
                chat_id=sent_msg.chat.id, message_id=sent_msg.message_id,
                reply_markup=get_video_keyboard(key, force_lang)
            )

        # 2. ФОТО/ГАЛЕРЕЯ (Переклад через Resend)
        else:
            # Кнопки для фото (з посиланням для resend)
            kb = get_photo_keyboard(user_url, force_lang)
            
            if final_photo:
                await message.answer_photo(
                    BufferedInputFile(final_photo, filename="photo.jpg"),
                    caption=caption,
                    parse_mode="HTML"
                )
            elif final_gallery:
                mg = MediaGroupBuilder()
                # Підпис тільки до першого фото, щоб воно було одним повідомленням
                for i, b in enumerate(final_gallery):
                    if i == 0:
                        mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"), caption=caption, parse_mode="HTML")
                    else:
                        mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await message.answer_media_group(mg.build())

            # Окремо аудіо (якщо є)
            if final_audio:
                await message.answer_audio(BufferedInputFile(final_audio, filename=audio_filename))
            
            # Кнопки для фото/галерей завжди окремим повідомленням знизу
            # Це єдиний спосіб дати кнопки для галереї
            await message.answer("Опції:", reply_markup=kb)

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if status_msg: await status_msg.edit_text("❌ Помилка. Спробуйте ще раз.")

# --- CALLBACKS ---
@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    action = callback.data
    
    # 1. ВІДЕО: Переклад на льоту
    if action == "toggle_lang_video":
        key = f"{callback.message.chat.id}:{callback.message.message_id}"
        data = VIDEO_CACHE.get(key)
        if not data:
            await callback.answer("Застаріло", show_alert=True)
            return
            
        # Визначаємо поточну мову по кнопці (трохи хак, але працює)
        # Якщо зараз кнопка "Переклад", значить ми в оригіналі.
        # Але ми можемо просто тоглити стан.
        # Краще дивитись на current_lang, який ми передамо в клавіатуру.
        # Спростимо: ми знаємо текст. 
        
        # Перемикаємо
        current_text = callback.message.caption # Поточний текст (з HTML тегами може бути складніше)
        # Просто дивимось: якщо зараз показуємо orig, то ставимо trans
        # Але ми не знаємо, що зараз. 
        # Зробимо простіше: в reply_markup кнопки ми знаємо, що пропонуємо.
        
        # Отримуємо поточну кнопку
        current_btn_text = callback.message.reply_markup.inline_keyboard[-1][0].text
        new_lang = 'uk' if "Переклад" in current_btn_text else 'orig'
        
        text_to_show = data['trans_text'] if new_lang == 'uk' else data['orig_text']
        new_caption = format_caption(data['author'], data['link'], text_to_show, data['url'])
        
        await bot.edit_message_caption(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            caption=new_caption,
            parse_mode="HTML",
            reply_markup=get_video_keyboard(key, new_lang)
        )
        await callback.answer()

    # 2. ВІДЕО: Аудіо / Clean
    elif action == "get_audio" or action == "get_clean":
        key = f"{callback.message.chat.id}:{callback.message.message_id}"
        data = VIDEO_CACHE.get(key)
        if not data: return
        
        if action == "get_audio":
            if data.get('audio_bytes'): # Якщо було окреме аудіо (рідко для відео)
                 pass 
            # Витягуємо з відео
            await callback.answer("Витягую...")
            aud = await asyncio.to_thread(extract_audio_from_video, data['video_bytes'])
            if aud: await callback.message.reply_audio(BufferedInputFile(aud, filename=data['audio_name']))
        
        elif action == "get_clean":
            await callback.message.reply_video(BufferedInputFile(data['video_bytes'], filename="video.mp4"))
        await callback.answer()

    # 3. ФОТО: Перезалив (Resend)
    elif action.startswith("resend:"):
        _, lang, link_id = action.split(":")
        user_url = LINK_STORAGE.get(link_id)
        if not user_url:
            await callback.answer("Посилання застаріло", show_alert=True)
            return
        
        await callback.message.delete() # Видаляємо старі кнопки
        # Запускаємо процес наново з примусовою мовою
        await process_media_request(callback.message, user_url, force_lang=lang)
        await callback.answer()

    # 4. ФОТО: Clean
    elif action.startswith("clean_photo:"):
        _, link_id = action.split(":")
        user_url = LINK_STORAGE.get(link_id)
        if user_url:
            await callback.message.delete()
            await process_media_request(callback.message, user_url, clean_mode=True)
        await callback.answer()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Кидай посилання.")

@dp.message(F.text.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    user_url, clean, audio = parse_message_data(message.text)
    await process_media_request(message, user_url, clean, audio)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
