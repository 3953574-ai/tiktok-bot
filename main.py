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

# Дзеркала Cobalt
COBALT_MIRRORS = [
    "https://co.wuk.sh/api/json",
    "https://api.cobalt.tools/api/json",
    "https://cobalt.pub/api/json",
    "https://api.succoon.com/api/json"
]

# Кеш
CACHE = {}

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

# --- КЛАВІАТУРА ---
def get_keyboard(cache_key, content_type='video', current_lang='orig', show_translate=True):
    buttons = []
    row1 = []
    
    # 1. Аудіо (для відео) та Чистий файл
    if content_type == 'video':
        row1.append(InlineKeyboardButton(text="🎵 Аудіо", callback_data="get_audio"))
        row1.append(InlineKeyboardButton(text="🎬 Відео", callback_data="get_clean"))
    else:
        row1.append(InlineKeyboardButton(text="🖼 Тільки фото", callback_data="get_clean"))
    
    buttons.append(row1)
    
    # 2. Переклад (якщо текст різний)
    if show_translate:
        if current_lang == 'orig':
            buttons.append([InlineKeyboardButton(text="🇺🇦 Переклад", callback_data="toggle_lang")])
        else:
            buttons.append([InlineKeyboardButton(text="🌐 Оригінал", callback_data="toggle_lang")])
            
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

# --- COBALT API ---
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

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False):
    status_msg = await message.reply("⏳ Обробляю...")

    try:
        # Змінні
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

        # --- INSTAGRAM / TWITTER ---
        elif any(x in user_url for x in ["instagram.com", "twitter.com", "x.com"]):
            is_insta = "instagram.com" in user_url
            success = False
            
            # Instaloader
            if is_insta:
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
            
            # Cobalt (Fallback)
            if not success:
                c_data = await get_cobalt_data(user_url)
                if not c_data: raise Exception("API Error")
                
                if "twitter" in user_url or "x.com" in user_url:
                    author_name = "Twitter User"
                    author_link = user_url
                    audio_filename = "twitter_audio.mp3"
                
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
        # Чи є сенс показувати кнопку перекладу? (Тільки якщо тексти різні)
        show_translate = (orig_text != trans_text)
        
        # Ручний режим
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
            await status_msg.delete()
            return

        if audio_mode:
            if final_audio_bytes:
                await message.answer_audio(BufferedInputFile(final_audio_bytes, filename=audio_filename))
            elif is_video and final_video_bytes:
                ab = await asyncio.to_thread(extract_audio_from_video, final_video_bytes)
                if ab: await message.answer_audio(BufferedInputFile(ab, filename=audio_filename))
            await status_msg.delete()
            return

        # Стандартний режим - СТАРТ З ОРИГІНАЛУ
        caption = format_caption(author_name, author_link, orig_text, user_url)
        sent_msg = None
        
        # Дані для кешу
        cache_data = {
            'orig_text': orig_text,
            'trans_text': trans_text,
            'author': author_name,
            'link': author_link,
            'url': user_url,
            'current': 'orig',
            'video': final_video_bytes,
            'photo': final_photo_bytes,
            'gallery': final_gallery,
            'audio': final_audio_bytes,
            'audio_name': audio_filename,
            'show_translate': show_translate
        }

        # 1. ВІДЕО
        if is_video and final_video_bytes:
            sent_msg = await message.answer_video(
                BufferedInputFile(final_video_bytes, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML"
            )
            key = f"{sent_msg.chat.id}:{sent_msg.message_id}"
            CACHE[key] = cache_data
            await bot.edit_message_reply_markup(
                chat_id=sent_msg.chat.id, message_id=sent_msg.message_id,
                reply_markup=get_keyboard(key, 'video', 'orig', show_translate)
            )

        # 2. ОДНЕ ФОТО
        elif final_photo_bytes:
            sent_msg = await message.answer_photo(
                BufferedInputFile(final_photo_bytes, filename="photo.jpg"),
                caption=caption,
                parse_mode="HTML"
            )
            key = f"{sent_msg.chat.id}:{sent_msg.message_id}"
            CACHE[key] = cache_data
            await bot.edit_message_reply_markup(
                chat_id=sent_msg.chat.id, message_id=sent_msg.message_id,
                reply_markup=get_keyboard(key, 'photo', 'orig', show_translate)
            )

        # 3. ГАЛЕРЕЯ
        elif final_gallery:
            mg = MediaGroupBuilder()
            for i, b in enumerate(final_gallery):
                if b: mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
            
            # Спочатку медіа
            await message.answer_media_group(mg.build())
            
            # Потім ТЕКСТ окремим повідомленням
            sent_msg = await message.answer(caption, parse_mode="HTML", disable_web_page_preview=True)
            
            key = f"{sent_msg.chat.id}:{sent_msg.message_id}"
            cache_data['is_gallery_text'] = True # Маркер для перевідправки
            CACHE[key] = cache_data
            
            await bot.edit_message_reply_markup(
                chat_id=sent_msg.chat.id, message_id=sent_msg.message_id,
                reply_markup=get_keyboard(key, 'photo', 'orig', show_translate)
            )

        # Авто-аудіо для фото/галерей (без підпису!)
        if (final_photo_bytes or final_gallery) and final_audio_bytes:
            await message.answer_audio(BufferedInputFile(final_audio_bytes, filename=audio_filename))

        await status_msg.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if status_msg: await status_msg.edit_text("❌ Помилка. Перевірте посилання.")

# --- ОБРОБКА КНОПОК ---
@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    try:
        key = f"{callback.message.chat.id}:{callback.message.message_id}"
        data = CACHE.get(key)
        
        if not data:
            await callback.answer("Дані застаріли 😔", show_alert=True)
            return

        action = callback.data

        if action == "get_clean":
            if data.get('video'):
                await callback.message.reply_video(BufferedInputFile(data['video'], filename="video.mp4"))
            elif data.get('photo'):
                await callback.message.reply_photo(BufferedInputFile(data['photo'], filename="photo.jpg"))
            elif data.get('gallery'):
                mg = MediaGroupBuilder()
                for i, b in enumerate(data['gallery']):
                    if b: mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await callback.message.reply_media_group(mg.build())
            await callback.answer()

        elif action == "get_audio":
            aud = data.get('audio')
            if not aud and data.get('video'):
                await callback.answer("Витягую звук...")
                aud = await asyncio.to_thread(extract_audio_from_video, data['video'])
            
            if aud:
                fname = data.get('audio_name', 'audio.mp3')
                # ТУТ ВАЖЛИВО: reply_audio без caption
                await callback.message.reply_audio(BufferedInputFile(aud, filename=fname))
            else:
                await callback.answer("Немає звуку", show_alert=True)
            await callback.answer()

        elif action == "toggle_lang":
            new_lang = 'trans' if data['current'] == 'orig' else 'orig'
            
            text_to_show = data['trans_text'] if new_lang == 'trans' else data['orig_text']
            new_caption = format_caption(data['author'], data['link'], text_to_show, data['url'])
            
            # --- ЛОГІКА ПЕРЕКЛАДУ ---
            
            # 1. Якщо це відео або одне фото -> Редагуємо на льоту (edit_caption)
            if not data.get('is_gallery_text'):
                CACHE[key]['current'] = new_lang
                ctype = 'video' if data.get('video') else 'photo'
                
                await bot.edit_message_caption(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    caption=new_caption,
                    parse_mode="HTML",
                    reply_markup=get_keyboard(key, ctype, new_lang, data['show_translate'])
                )
            
            # 2. Якщо це текст під галереєю -> Відправляємо НОВЕ повідомлення (re-send)
            else:
                # Видаляємо старе текстове повідомлення (щоб не смітити)
                try: await callback.message.delete()
                except: pass
                
                # Відправляємо нове
                sent_msg = await callback.message.answer(new_caption, parse_mode="HTML", disable_web_page_preview=True)
                
                # Оновлюємо ключ в кеші на новий
                new_key = f"{sent_msg.chat.id}:{sent_msg.message_id}"
                data['current'] = new_lang
                CACHE[new_key] = data # Переносимо дані на новий ключ
                
                # Додаємо кнопки до нового повідомлення
                await bot.edit_message_reply_markup(
                    chat_id=sent_msg.chat.id,
                    message_id=sent_msg.message_id,
                    reply_markup=get_keyboard(new_key, 'photo', new_lang, data['show_translate'])
                )

            await callback.answer()

    except Exception as e:
        logging.error(f"Callback Error: {e}")

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
