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

# ГЛОБАЛЬНЕ СХОВИЩЕ ДАНИХ (UUID -> DATA)
# Це "мозок" бота. Тут лежить інфо про останні пости.
STORAGE = {}

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
def get_keyboard(data_id, content_type, current_lang):
    # data_id: унікальний ключ в STORAGE
    # content_type: 'video' або 'photo'
    # current_lang: 'orig' або 'uk'
    
    buttons = []
    
    # Рядок 1: Медіа-кнопки
    row1 = []
    if content_type == 'video':
        row1.append(InlineKeyboardButton(text="🎵 Аудіо", callback_data=f"cmd:audio:{data_id}"))
        row1.append(InlineKeyboardButton(text="🎬 Відео", callback_data=f"cmd:clean:{data_id}"))
    else:
        row1.append(InlineKeyboardButton(text="🖼 Тільки фото", callback_data=f"cmd:clean:{data_id}"))
    buttons.append(row1)
    
    # Рядок 2: Переклад (тільки якщо тексти відрізняються)
    data = STORAGE.get(data_id)
    if data and data['text_orig'] != data['text_trans']:
        if current_lang == 'orig':
            buttons.append([InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"cmd:trans:{data_id}")])
        else:
            buttons.append([InlineKeyboardButton(text="🌐 Оригінал", callback_data=f"cmd:orig:{data_id}")])
            
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def sanitize_filename(name):
    if not name: return "audio"
    name = re.sub(r'[\\/*?:"<>|]', "", str(name))
    return name[:50].strip()

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
            return text, trans
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
        vid_path = f"temp_{unique}.mp4"
        aud_path = f"temp_{unique}.mp3"
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
# 🔥 ОБРОБКА ПОСИЛАНЬ 🔥
# ==========================================

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, force_lang='orig'):
    # force_lang: 'orig' або 'uk'. Використовується при перезаливі фото.
    
    if not clean_mode and not audio_mode and message.from_user.id != bot.id:
        status_msg = await message.reply("⏳ ...")
    else:
        status_msg = None

    try:
        # Збираємо дані
        video_bytes = None
        photo_bytes = None
        gallery_bytes = [] # список байтів
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
                vid_url = data.get('hdplay') or data.get('play')
                video_bytes = await download_content(vid_url)

        # --- TWITTER (X) - VXTWITTER API ---
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
            has_video = any(m['type'] in ['video','gif'] for m in media_list)
            
            if has_video:
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
                    if ".mp4" in url or "video" in c_data.get('filename', ''): video_bytes = content
                    else: photo_bytes = content

        # --- ПІДГОТОВКА ВІДПОВІДІ ---
        
        # 1. Переклад
        orig_text, trans_text = await prepare_text_data(raw_desc)
        
        # 2. Ручні команди (Clean/Audio) - тут все просто
        if clean_mode:
            if video_bytes: await message.answer_video(BufferedInputFile(video_bytes, filename="video.mp4"))
            elif photo_bytes: await message.answer_photo(BufferedInputFile(photo_bytes, filename="photo.jpg"))
            elif gallery_bytes:
                mg = MediaGroupBuilder()
                for i, b in enumerate(gallery_bytes): mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await message.answer_media_group(mg.build())
            if status_msg: await status_msg.delete()
            return

        if audio_mode:
            if audio_bytes:
                await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))
            elif video_bytes:
                ab = await asyncio.to_thread(extract_audio_from_video, video_bytes)
                if ab: await message.answer_audio(BufferedInputFile(ab, filename=audio_name))
            if status_msg: await status_msg.delete()
            return

        # 3. ЗБЕРЕЖЕННЯ В ПАМ'ЯТЬ (Для кнопок)
        data_id = str(uuid.uuid4())[:8]
        STORAGE[data_id] = {
            'orig_text': orig_text,
            'trans_text': trans_text,
            'author_name': author_name,
            'author_link': author_link,
            'user_url': user_url,
            'video_bytes': video_bytes,
            'photo_bytes': photo_bytes,
            'gallery_bytes': gallery_bytes,
            'audio_bytes': audio_bytes,
            'audio_name': audio_name,
            'current_lang': force_lang
        }

        # Формуємо підпис залежно від мови
        current_text = trans_text if force_lang == 'uk' else orig_text
        caption = format_caption(author_name, author_link, current_text, user_url)
        
        # Тип контенту для кнопок
        ctype = 'video' if video_bytes else 'photo'

        # --- ВІДПРАВКА ---
        
        # А) ВІДЕО (Одне повідомлення)
        if video_bytes:
            await message.answer_video(
                BufferedInputFile(video_bytes, filename="video.mp4"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_keyboard(data_id, ctype, force_lang)
            )
        
        # Б) ОДНЕ ФОТО (Одне повідомлення)
        elif photo_bytes:
            await message.answer_photo(
                BufferedInputFile(photo_bytes, filename="photo.jpg"),
                caption=caption,
                parse_mode="HTML"
            )
            # Кнопки окремо
            await message.answer("Опції:", reply_markup=get_keyboard(data_id, ctype, force_lang))

        # В) ГАЛЕРЕЯ (Альбом + Текст з кнопками окремо)
        elif gallery_bytes:
            mg = MediaGroupBuilder()
            for i, b in enumerate(gallery_bytes):
                mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
            await message.answer_media_group(mg.build())
            
            # Текст і кнопки окремо
            await message.answer(caption, parse_mode="HTML", disable_web_page_preview=True, reply_markup=get_keyboard(data_id, ctype, force_lang))

        # Авто-аудіо (для фото/галерей)
        if (photo_bytes or gallery_bytes) and audio_bytes:
            await message.answer_audio(BufferedInputFile(audio_bytes, filename=audio_name))

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Помилка: {e}")

# ==========================
# 🔥 ОБРОБКА КНОПОК (CALLBACKS) 🔥
# ==========================================

@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery):
    try:
        # Формат: "cmd:action:data_id"
        parts = callback.data.split(":")
        if len(parts) != 3: return

        action = parts[1]
        data_id = parts[2]
        
        data = STORAGE.get(data_id)
        
        # Якщо даних немає в пам'яті (бот перезавантажився)
        if not data:
            await callback.answer("Бот оновився. Будь ласка, надішліть посилання ще раз.", show_alert=True)
            return

        # 1. ЧИСТЕ МЕДІА
        if action == "clean":
            await callback.answer("Надсилаю...")
            if data['video_bytes']:
                await callback.message.reply_video(BufferedInputFile(data['video_bytes'], filename="video.mp4"))
            elif data['photo_bytes']:
                await callback.message.reply_photo(BufferedInputFile(data['photo_bytes'], filename="photo.jpg"))
            elif data['gallery_bytes']:
                mg = MediaGroupBuilder()
                for i, b in enumerate(data['gallery_bytes']): mg.add_photo(BufferedInputFile(b, filename=f"p{i}.jpg"))
                await callback.message.reply_media_group(mg.build())

        # 2. АУДІО
        elif action == "audio":
            await callback.answer("Надсилаю аудіо...")
            aud = data['audio_bytes']
            if not aud and data['video_bytes']:
                aud = await asyncio.to_thread(extract_audio_from_video, data['video_bytes'])
            
            if aud:
                await callback.message.reply_audio(BufferedInputFile(aud, filename=data['audio_name']))
            else:
                await callback.answer("Немає звуку", show_alert=True)

        # 3. ПЕРЕКЛАД / ОРИГІНАЛ
        elif action == "trans" or action == "orig":
            target_lang = 'uk' if action == "trans" else 'orig'
            
            # ВІДЕО: Редагуємо на льоту (Edit Caption)
            if data['video_bytes']:
                text_to_show = data['trans_text'] if target_lang == 'uk' else data['orig_text']
                new_caption = format_caption(data['author_name'], data['author_link'], text_to_show, data['user_url'])
                
                try:
                    await bot.edit_message_caption(
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                        caption=new_caption,
                        parse_mode="HTML",
                        reply_markup=get_keyboard(data_id, 'video', target_lang)
                    )
                except: pass # Якщо текст такий самий, телеграм дасть помилку, ігноруємо
                
            # ФОТО/ГАЛЕРЕЯ: Перезаливаємо (Resend)
            else:
                await callback.message.delete() # Видаляємо старе меню
                # Запускаємо функцію з нуля, але форсуємо мову
                # Створюємо фейковий об'єкт Message, щоб перевикористати функцію
                fake_msg = callback.message
                # Але ми не можемо просто так викликати process_media_request, бо message має бути від юзера
                # Тому ми просто відповімо на callback.message (це повідомлення бота)
                # Або краще: просто відправимо новий пост в той же чат
                
                # Щоб не дублювати код, просто викличемо process_media_request
                # Але передамо туди original message object (на який відповідав бот) - це складно дістати.
                # ПРОСТІШЕ: Просто викличемо process_media_request, але з параметром force_lang
                
                # Нам треба "відновити" об'єкт message від імені користувача, але у нас є тільки callback.message (від бота).
                # Тому ми просто відправимо в цей чат.
                await process_media_request(callback.message, data['user_url'], force_lang=target_lang)
            
            await callback.answer()

    except Exception as e:
        logging.error(f"Callback Error: {e}")

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
