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

# Пам'ять: ID -> {url, desc, author, profile_link}
LINK_STORAGE = {}

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
def get_media_keyboard(data_id, content_type='video'):
    # content_type: 'video' або 'photo'
    
    buttons = []
    
    # 1. Кнопка Переклад (🇺🇦)
    trans_btn = InlineKeyboardButton(text="🇺🇦 Переклад", callback_data=f"trans:{data_id}")
    
    # 2. Кнопка Чисте Відео/Фото (🎬)
    clean_btn = InlineKeyboardButton(text="🎬 Відео" if content_type == 'video' else "🖼 Фото", callback_data=f"clean:{data_id}")
    
    if content_type == 'video':
        # 3. Кнопка Аудіо (🎵)
        audio_btn = InlineKeyboardButton(text="🎵 Аудіо", callback_data=f"audio:{data_id}")
        # Рядок 1: Аудіо | Відео
        buttons.append([audio_btn, clean_btn])
        # Рядок 2: Переклад
        buttons.append([trans_btn])
    else:
        # Для фото: Відео(Фото) | Переклад
        buttons.append([clean_btn, trans_btn])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def save_data_for_buttons(url, desc, author, profile):
    """Зберігає дані, щоб кнопки могли їх використати"""
    unique_id = str(uuid.uuid4())[:8]
    LINK_STORAGE[unique_id] = {
        'url': url,
        'desc': desc,
        'author': author,
        'profile': profile
    }
    return unique_id

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
    
    # Технічні команди (залишили для "своїх")
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

# Функція простого перекладу тексту
async def perform_translation(text):
    if not text or not text.strip(): return ""
    try:
        return await asyncio.to_thread(translator.translate, text)
    except: return text

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

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, is_button_click=False):
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
            
            author_name = data['author']['nickname']
            unique_id = data['author']['unique_id']
            profile_link = f"https://www.tiktok.com/@{unique_id}"
            title_text = data.get('title', '')
            
            # Зберігаємо дані для кнопок
            data_id = save_data_for_buttons(user_url, title_text, author_name, profile_link)
            
            # Формуємо підпис (в оригіналі)
            caption_text = None
            if not clean_mode:
                caption_text = format_caption(author_name, profile_link, title_text, user_url)

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
                
                # Кнопки для фото
                kb = get_media_keyboard(data_id, content_type='photo') if not clean_mode else None
                
                # Якщо є аудіо -> кидаємо з кнопками. Якщо ні -> кидаємо просто кнопки
                if music_file and not clean_mode:
                    await message.answer_audio(music_file, reply_markup=kb)
                elif not clean_mode:
                    await message.answer("Опції:", reply_markup=kb)

            # ВІДЕО
            else:
                vid_url = data.get('hdplay') or data.get('play')
                vb = await download_content(vid_url)
                if vb:
                    kb = get_media_keyboard(data_id, content_type='video') if (not clean_mode and not audio_mode) else None
                    await message.answer_video(
                        BufferedInputFile(vb, filename="tiktok.mp4"), 
                        caption=caption_text, 
                        parse_mode="HTML", 
                        reply_markup=kb
                    )
            
            if audio_mode and not ('images' in data) and music_file: 
                await message.answer_audio(music_file, caption="🎵 Звук")

        # --- INSTAGRAM ---
        elif "instagram.com" in user_url:
            shortcode_match = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url)
            shortcode = shortcode_match.group(1)

            def get_insta():
                L = instaloader.Instaloader(quiet=True)
                L.context._user_agent = "Instagram 269.0.0.18.75 Android"
                return instaloader.Post.from_shortcode(L.context, shortcode)
            
            post = await asyncio.to_thread(get_insta)
            
            author_name = post.owner_username
            profile_link = f"https://instagram.com/{author_name}"
            raw_cap = (post.caption or "").split('\n')[0] # Беремо перший рядок для заголовку
            
            data_id = save_data_for_buttons(user_url, raw_cap, author_name, profile_link)
            
            caption_text = None
            if not clean_mode:
                caption_text = format_caption(author_name, profile_link, raw_cap, user_url)

            audio_filename = f"{author_name} - {sanitize_filename(raw_cap)}.mp3" if raw_cap else f"{author_name} - Audio.mp3"

            tasks = []
            if post.typename == 'GraphSidecar':
                for node in post.get_sidecar_nodes():
                    tasks.append((download_content(node.video_url if node.is_video else node.display_url), node.is_video))
            else:
                tasks.append((download_content(post.video_url if post.is_video else post.url), post.is_video))

            results = await asyncio.gather(*[t[0] for t in tasks])
            
            # 1 файл
            if len(results) == 1 and results[0]:
                content, is_vid = results[0], tasks[0][1]
                f = BufferedInputFile(content, filename=f"insta.{'mp4' if is_vid else 'jpg'}")
                
                if is_vid:
                    if audio_mode and not clean_mode:
                         aud_bytes = await asyncio.to_thread(extract_audio_from_video, content)
                         if aud_bytes: await message.answer_audio(BufferedInputFile(aud_bytes, filename=audio_filename), caption="🎵 Звук з Instagram")
                    else:
                        kb = get_media_keyboard(data_id, 'video') if (not clean_mode and not audio_mode) else None
                        await message.answer_video(f, caption=caption_text, parse_mode="HTML", reply_markup=kb)
                else:
                    await message.answer_photo(f, caption=caption_text, parse_mode="HTML")
                    if not clean_mode:
                        await message.answer("Опції:", reply_markup=get_media_keyboard(data_id, 'photo'))
            
            # Галерея
            elif len(results) > 1:
                media_group = MediaGroupBuilder()
                for i, content in enumerate(results):
                    if content:
                        is_vid = tasks[i][1]
                        f = BufferedInputFile(content, filename=f"m{i}.{'mp4' if is_vid else 'jpg'}")
                        if i == 0 and caption_text:
                            if is_vid: media_group.add_video(f, caption=caption_text, parse_mode="HTML")
                            else: media_group.add_photo(f, caption=caption_text, parse_mode="HTML")
                        else:
                            if is_vid: media_group.add_video(f)
                            else: media_group.add_photo(f)
                
                await message.answer_media_group(media_group.build())
                if not clean_mode and not audio_mode:
                     await message.answer("Опції:", reply_markup=get_media_keyboard(data_id, 'photo'))

        # --- TWITTER ---
        elif "twitter.com" in user_url or "x.com" in user_url:
            tw_id = re.search(r"/status/(\d+)", user_url).group(1)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.fxtwitter.com/status/{tw_id}") as r:
                    tweet = (await r.json()).get('tweet', {})
            
            text_content = tweet.get('text', '')
            author_name = tweet.get('author', {}).get('name', 'User')
            screen_name = tweet.get('author', {}).get('screen_name', 'tw')
            profile_link = f"https://x.com/{screen_name}"
            
            data_id = save_data_for_buttons(user_url, text_content, author_name, profile_link)
            
            caption_text = None
            if not clean_mode:
                caption_text = format_caption(author_name, profile_link, text_content, user_url)

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
                        kb = get_media_keyboard(data_id, 'video') if (not clean_mode and not audio_mode) else None
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
                if not clean_mode: await message.answer("Опції:", reply_markup=get_media_keyboard(data_id, 'photo'))

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Processing error: {e}")
        if status_msg: await status_msg.edit_text("❌ Сталася помилка. Перевірте посилання.")

# ==========================
# 🎮 ОБРОБНИКИ
# ==========================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Привіт!</b>\n\n"
        "Я завантажую контент у найкращій якості з:\n"
        "🎵 <b>TikTok</b>\n"
        "📸 <b>Instagram</b>\n"
        "🐦 <b>Twitter (X)</b>\n\n"
        "Просто надішли мені посилання 🚀",
        parse_mode="HTML"
    )

@dp.callback_query()
async def on_button_click(callback: CallbackQuery):
    try:
        action, data_id = callback.data.split(":")
        
        # Отримуємо дані про пост з пам'яті
        post_data = LINK_STORAGE.get(data_id)
        
        if not post_data:
            await callback.answer("Дані застаріли (бот був перезавантажений).", show_alert=True)
            return
        
        url = post_data['url']
        
        # --- ЛОГІКА КНОПОК ---
        
        # 1. 🎬 Чисте відео / Фото
        if action == "clean":
            await callback.answer("Надсилаю файл...")
            await process_media_request(callback.message, url, clean_mode=True, is_button_click=True)
        
        # 2. 🎵 Аудіо
        elif action == "audio":
            await callback.answer("Витягую звук...")
            await process_media_request(callback.message, url, audio_mode=True, clean_mode=False, is_button_click=True)
            
        # 3. 🇺🇦 Переклад
        elif action == "trans":
            await callback.answer("Перекладаю...")
            
            # Робимо переклад
            translated_desc = await perform_translation(post_data['desc'])
            
            # Формуємо новий підпис
            new_caption = format_caption(
                post_data['author'],
                post_data['profile'],
                translated_desc,
                url
            )
            
            # Редагуємо повідомлення, до якого прикріплена кнопка
            try:
                await callback.message.edit_caption(
                    caption=new_caption,
                    parse_mode="HTML",
                    reply_markup=callback.message.reply_markup # Залишаємо кнопки на місці
                )
            except Exception as e:
                logging.error(f"Edit error: {e}")
                # Якщо змінити не вийшло (наприклад, текст той самий), нічого не робимо
                pass

    except Exception as e:
        logging.error(f"Callback error: {e}")

@dp.message(F.text.regexp(r'(https?://[^\s]+)') | F.caption.regexp(r'(https?://[^\s]+)'))
@dp.edited_message(F.text.regexp(r'(https?://[^\s]+)') | F.caption.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    content = message.text or message.caption
    user_url, clean, audio = parse_message_data(content)
    await process_media_request(message, user_url, clean, audio)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
