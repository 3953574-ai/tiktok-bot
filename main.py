import logging
import sys
import os
import asyncio
import re
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

# Пам'ять для кнопок
LINK_STORAGE = {}

# Дзеркала Cobalt (Instagram Fallback, Threads, Reddit)
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
    
    # Нова команда перекладу (t або translate)
    toggle_trans = ('(t)' in cmd_text or '(translate)' in cmd_text or ' t ' in cmd_text or 'translate' in cmd_text)
    
    return found_url, clean_mode, audio_mode, toggle_trans

async def download_content(url):
    if not url: return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200: return await response.read()
    except: return None

# 🔥 ОНОВЛЕНА ЛОГІКА ПЕРЕКЛАДУ 🔥
async def translate_text_logic(text, toggle_trans=False):
    if not text or not text.strip(): return ""
    try:
        lang = detect(text)
        
        # Логіка:
        # 1. За замовчуванням (toggle_trans=False): EN -> EN, Other -> UK
        # 2. З командою (toggle_trans=True): EN -> UK, Other -> Other (Original)
        
        if not toggle_trans:
            if lang == 'en': return text # Англ залишаємо
            else: return await asyncio.to_thread(translator.translate, text) # Інші перекладаємо
        else:
            if lang == 'en': return await asyncio.to_thread(translator.translate, text) # Англ перекладаємо
            else: return text # Інші залишаємо в оригіналі
            
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

# --- COBALT API (Universal Helper) ---
async def get_cobalt_data(user_url):
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
                        if data.get('status') in ['stream', 'redirect', 'picker']:
                            return data
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
# 🔥 УНІВЕРСАЛЬНА ЛОГІКА ОБРОБКИ 🔥
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

            # ФОТО
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
                
                # Спочатку аудіо з кнопкою (якщо є)
                kb = get_media_keyboard(user_url, content_type='photo') if not clean_mode else None
                if music_file and not clean_mode:
                    await message.answer_audio(music_file, reply_markup=kb)
                elif not clean_mode:
                    await message.answer("Опції:", reply_markup=kb)

            # ВІДЕО
            else:
                vid_url = data.get('hdplay') or data.get('play')
                vb = await download_content(vid_url)
                if vb:
                    kb = get_media_keyboard(user_url, content_type='video') if (not clean_mode and not audio_mode) else None
                    await message.answer_video(
                        BufferedInputFile(vb, filename="tiktok.mp4"), 
                        caption=caption_text, 
                        parse_mode="HTML", 
                        reply_markup=kb
                    )
            
            if audio_mode and not ('images' in data) and music_file: 
                await message.answer_audio(music_file, caption="🎵 Звук")

        # --- INSTAGRAM / THREADS / REDDIT (General Logic) ---
        elif any(x in user_url for x in ["instagram.com", "threads.net", "reddit.com"]):
            
            is_insta = "instagram.com" in user_url
            success = False
            
            # 1. Для Instagram спочатку пробуємо Instaloader (він краще дає опис/автора)
            if is_insta:
                try:
                    shortcode = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url).group(1)
                    def get_insta():
                        L = instaloader.Instaloader(quiet=True)
                        L.context._user_agent = "Instagram 269.0.0.18.75 Android"
                        return instaloader.Post.from_shortcode(L.context, shortcode)
                    
                    post = await asyncio.to_thread(get_insta)
                    
                    # Формуємо caption
                    caption_text = None
                    raw_cap = (post.caption or "").split('\n')[0]
                    author_name = post.owner_username
                    
                    if not clean_mode:
                        trans = await translate_text_logic(raw_cap, toggle_trans)
                        caption_text = format_caption(author_name, f"https://instagram.com/{author_name}", trans, user_url)

                    # Завантаження
                    tasks = []
                    if post.typename == 'GraphSidecar':
                        for node in post.get_sidecar_nodes():
                            tasks.append((download_content(node.video_url if node.is_video else node.display_url), node.is_video))
                    else:
                        tasks.append((download_content(post.video_url if post.is_video else post.url), post.is_video))

                    results = await asyncio.gather(*[t[0] for t in tasks])
                    
                    # Відправка
                    if len(results) == 1 and results[0]: # Один файл
                        content, is_vid = results[0], tasks[0][1]
                        f = BufferedInputFile(content, filename=f"insta.{'mp4' if is_vid else 'jpg'}")
                        
                        if is_vid:
                            if audio_mode and not clean_mode:
                                ab = await asyncio.to_thread(extract_audio_from_video, content)
                                if ab: await message.answer_audio(BufferedInputFile(ab, filename=f"{author_name}.mp3"), caption="🎵 Звук")
                            else:
                                kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                                await message.answer_video(f, caption=caption_text, parse_mode="HTML", reply_markup=kb)
                        else:
                            await message.answer_photo(f, caption=caption_text, parse_mode="HTML")
                            if not clean_mode: await message.answer("Опції:", reply_markup=get_media_keyboard(user_url, 'photo'))
                    
                    elif len(results) > 1: # Галерея
                        mg = MediaGroupBuilder()
                        for i, content in enumerate(results):
                            if content:
                                is_vid = tasks[i][1]
                                f = BufferedInputFile(content, filename=f"m{i}.{'mp4' if is_vid else 'jpg'}")
                                if i==0 and caption_text: 
                                    if is_vid: mg.add_video(f, caption=caption_text, parse_mode="HTML")
                                    else: mg.add_photo(f, caption=caption_text, parse_mode="HTML")
                                else:
                                    if is_vid: mg.add_video(f)
                                    else: mg.add_photo(f)
                        await message.answer_media_group(mg.build())
                        if not clean_mode and not audio_mode:
                            await message.answer("Опції:", reply_markup=get_media_keyboard(user_url, 'photo'))
                            
                    success = True
                except:
                    logging.warning("Instaloader failed, switching to Cobalt...")
            
            # 2. FALLBACK або OTHERS (Threads/Reddit) через Cobalt
            if not success:
                cobalt_data = await get_cobalt_data(user_url)
                if not cobalt_data: raise Exception("API Error")
                
                # Спробуємо дістати дані (Cobalt іноді дає назву)
                title_text = ""
                # Threads/Reddit/Insta Cobalt не завжди дають автора, тому ставимо заглушку або домен
                domain = "Threads" if "threads" in user_url else "Reddit" if "reddit" in user_url else "Instagram"
                author_name = domain 
                
                # Якщо це Reddit/Threads, спробуємо знайти текст в відповіді, але зазвичай там просто файл
                # Тому робимо базовий підпис
                caption_text = None
                if not clean_mode:
                    caption_text = f"📄 <b>{domain} Post</b>\n🔗 <a href='{user_url}'>Оригінал</a>"

                # Обробка результату
                if cobalt_data.get('status') == 'picker':
                    # Галерея
                    mg = MediaGroupBuilder()
                    tasks = [download_content(item['url']) for item in cobalt_data['picker']]
                    files = await asyncio.gather(*tasks)
                    
                    for i, content in enumerate(files):
                        if content:
                            is_vid = (cobalt_data['picker'][i]['type'] == 'video')
                            f = BufferedInputFile(content, filename=f"c{i}.{'mp4' if is_vid else 'jpg'}")
                            if i==0 and caption_text:
                                if is_vid: mg.add_video(f, caption=caption_text, parse_mode="HTML")
                                else: mg.add_photo(f, caption=caption_text, parse_mode="HTML")
                            else:
                                if is_vid: mg.add_video(f)
                                else: mg.add_photo(f)
                    
                    await message.answer_media_group(mg.build())
                    if not clean_mode: await message.answer("Опції:", reply_markup=get_media_keyboard(user_url, 'photo'))

                else:
                    # Один файл
                    media_url = cobalt_data.get('url')
                    content = await download_content(media_url)
                    if content:
                        is_vid = ".mp4" in media_url or "video" in cobalt_data.get('filename', '')
                        f = BufferedInputFile(content, filename=f"file.{'mp4' if is_vid else 'jpg'}")
                        
                        if is_vid:
                            if audio_mode and not clean_mode:
                                ab = await asyncio.to_thread(extract_audio_from_video, content)
                                if ab: await message.answer_audio(BufferedInputFile(ab, filename="audio.mp3"), caption="🎵 Звук")
                            else:
                                kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                                await message.answer_video(f, caption=caption_text, parse_mode="HTML", reply_markup=kb)
                        else:
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

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Processing error: {e}")
        if status_msg: await status_msg.edit_text("❌ Сталася помилка. Перевірте посилання.")

# ==========================
# 🎮 ОБРОБНИКИ
# ==========================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я качаю з TikTok, Instagram, Twitter, Threads та Reddit.")

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
