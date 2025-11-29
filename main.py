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
    # content_type: 'video' або 'photo'
    link_id = str(uuid.uuid4())[:8]
    LINK_STORAGE[link_id] = url
    
    buttons = []
    # Кнопка "Чисте" (є завжди)
    clean_btn = InlineKeyboardButton(text="🙈 Без підписів", callback_data=f"clean:{link_id}")
    
    if content_type == 'video':
        # Для відео додаємо кнопку отримання аудіо
        audio_btn = InlineKeyboardButton(text="🎵 + Аудіо", callback_data=f"audio:{link_id}")
        buttons.append([audio_btn, clean_btn])
    else:
        # Для фото тільки кнопка очистки (бо аудіо ми вже скинули автоматом)
        buttons.append([clean_btn])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def sanitize_filename(name):
    """Прибирає погані символи з назви файлу"""
    if not name: return "audio"
    # Залишаємо тільки букви, цифри, дужки, дефіси
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name[:50]  # Обрізаємо, щоб не було занадто довгим

def time_to_seconds(time_str):
    parts = list(map(int, time_str.split(':')))
    if len(parts) == 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2: return parts[0] * 60 + parts[1]
    return 0

def parse_message_data(text):
    if not text: return None, False, False, None, 720
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False, False, None, 720
    
    found_url = url_match.group(1)
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text)
    
    cut_range = None
    cut_match = re.search(r'cut\s+(\d{1,2}:\d{2}(?::\d{2})?)-(\d{1,2}:\d{2}(?::\d{2})?)', cmd_text)
    if cut_match:
        start_sec = time_to_seconds(cut_match.group(1))
        end_sec = time_to_seconds(cut_match.group(2))
        if end_sec > start_sec: cut_range = (start_sec, end_sec)

    quality = 720
    res_match = re.search(r'\b(144|240|360|480|720|1080|1440|2160)\b', cmd_text)
    if res_match: quality = int(res_match.group(1))

    return found_url, clean_mode, audio_mode, cut_range, quality

async def download_content(url):
    if not url: return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200: return await response.read()
    except: return None

async def translate_text(text):
    if not text or not text.strip(): return ""
    try:
        lang = detect(text)
        if lang != 'en': return await asyncio.to_thread(translator.translate, text)
    except: pass
    return text

def format_caption(nickname, profile_url, title, original_url):
    caption = f"👤 <a href='{profile_url}'><b>{nickname}</b></a>\n\n"
    if title: caption += f"📝 {title}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    return caption[:1000] + "..." if len(caption) > 1024 else caption

# --- YOUTUBE HELPERS ---
async def cobalt_get_url(user_url, quality=720):
    payload = {
        "url": user_url, "videoQuality": str(quality),
        "youtubeVideoCodec": "h264", "audioFormat": "mp3",
        "filenamePattern": "classic"
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        for mirror in COBALT_MIRRORS:
            try:
                async with session.post(mirror, json=payload, headers=headers, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('status') in ['stream', 'redirect']: return data.get('url')
                        elif data.get('status') == 'picker': return data['picker'][0]['url']
            except: continue
    return None

def process_media_locally(input_path, output_path, audio_only=False, cut_range=None):
    cmd = ['ffmpeg', '-y', '-i', input_path]
    if cut_range: cmd.extend(['-ss', str(cut_range[0]), '-to', str(cut_range[1])])
    if audio_only: cmd.extend(['-vn', '-acodec', 'libmp3lame', '-q:a', '2'])
    else:
        if not cut_range: cmd.extend(['-c', 'copy']) 
        else: cmd.extend(['-c:v', 'libx264', '-c:a', 'aac'])
    cmd.append(output_path)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
# 🔥 ЛОГІКА ОБРОБКИ 🔥
# ==========================================

async def process_media_request(message: types.Message, user_url, clean_mode=False, audio_mode=False, cut_range=None, quality=720, is_button_click=False):
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
            if not clean_mode:
                trans = await translate_text(data.get('title', ''))
                unique_id = data['author']['unique_id']
                caption_text = format_caption(data['author']['nickname'], f"https://www.tiktok.com/@{unique_id}", trans, user_url)

            # Підготовка аудіо (назва файлу!)
            music_file = None
            music_url = data.get('music')
            # Для фото музика качається завжди, для відео - якщо просили
            should_download_music = audio_mode or ('images' in data and data['images'])
            
            if should_download_music and music_url:
                mb = await download_content(music_url)
                if mb:
                    # Формуємо гарну назву: "Автор - Трек.mp3"
                    m_author = data.get('music_info', {}).get('author', 'TikTok')
                    m_title = data.get('music_info', {}).get('title', 'Audio')
                    fname = f"{sanitize_filename(m_author)} - {sanitize_filename(m_title)}.mp3"
                    music_file = BufferedInputFile(mb, filename=fname)

            # === ФОТО (Слайдшоу) ===
            if 'images' in data and data['images']:
                tasks = [download_content(u) for u in data['images']]
                imgs = await asyncio.gather(*tasks)
                mg = MediaGroupBuilder()
                for i, img in enumerate(imgs):
                    if img:
                        f = BufferedInputFile(img, filename=f"i{i}.jpg")
                        if i==0 and caption_text: mg.add_photo(f, caption=caption_text, parse_mode="HTML")
                        else: mg.add_photo(f)
                
                msgs = await message.answer_media_group(mg.build())
                
                # Автоматично кидаємо аудіо, якщо це не чистий режим
                if music_file and not clean_mode:
                    await message.answer_audio(music_file)

                # Додаємо кнопку "Тільки фото" (Clean)
                if not clean_mode and msgs:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=msgs[0].chat.id, 
                            message_id=msgs[0].message_id, 
                            reply_markup=get_media_keyboard(user_url, content_type='photo')
                        )
                    except: pass

            # === ВІДЕО ===
            else:
                vid_url = data.get('hdplay') or data.get('play')
                vb = await download_content(vid_url)
                if vb:
                    kb = get_media_keyboard(user_url, content_type='video') if (not clean_mode and not audio_mode) else None
                    await message.answer_video(
                        BufferedInputFile(vb, filename="tk.mp4"), 
                        caption=caption_text, 
                        parse_mode="HTML",
                        reply_markup=kb
                    )
            
            # Якщо просили тільки аудіо (через команду !a)
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
            
            caption_text = None
            if not clean_mode:
                raw_cap = (post.caption or "").split('\n')[0]
                trans = await translate_text(raw_cap)
                caption_text = format_caption(post.owner_username, f"https://instagram.com/{post.owner_username}", trans, user_url)

            media_group = MediaGroupBuilder()
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
                         await message.answer_audio(BufferedInputFile(content, filename="audio.mp3"), caption="🎵 Звук з Instagram")
                    else:
                        kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode) else None
                        await message.answer_video(f, caption=caption_text, parse_mode="HTML", reply_markup=kb)
                else:
                    kb = get_media_keyboard(user_url, 'photo') if not clean_mode else None
                    await message.answer_photo(f, caption=caption_text, parse_mode="HTML", reply_markup=kb)
            
            # Галерея
            elif len(results) > 1:
                has_video_in_gallery = False
                for i, content in enumerate(results):
                    if content:
                        is_vid = tasks[i][1]
                        if is_vid: has_video_in_gallery = True
                        f = BufferedInputFile(content, filename=f"m{i}.{'mp4' if is_vid else 'jpg'}")
                        if i == 0 and caption_text:
                            if is_vid: media_group.add_video(f, caption=caption_text, parse_mode="HTML")
                            else: media_group.add_photo(f, caption=caption_text, parse_mode="HTML")
                        else:
                            if is_vid: media_group.add_video(f)
                            else: media_group.add_photo(f)
                
                msgs = await message.answer_media_group(media_group.build())
                if not clean_mode and not audio_mode and msgs:
                     try:
                        # Якщо в галереї є відео, даємо кнопки з аудіо, якщо ні - тільки clean
                        ctype = 'video' if has_video_in_gallery else 'photo'
                        await bot.edit_message_reply_markup(
                            chat_id=msgs[0].chat.id, 
                            message_id=msgs[0].message_id, 
                            reply_markup=get_media_keyboard(user_url, ctype)
                        )
                     except: pass

        # --- TWITTER ---
        elif "twitter.com" in user_url or "x.com" in user_url:
            tw_id = re.search(r"/status/(\d+)", user_url).group(1)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.fxtwitter.com/status/{tw_id}") as r:
                    tweet = (await r.json()).get('tweet', {})
            
            caption_text = None
            if not clean_mode:
                text = await translate_text(tweet.get('text', ''))
                u = tweet.get('author', {})
                caption_text = format_caption(u.get('name', 'User'), f"https://x.com/{u.get('screen_name')}", text, user_url)

            media = tweet.get('media', {}).get('all', [])
            has_video = any(m['type'] in ['video','gif'] for m in media)
            
            if has_video:
                vid = next(m for m in media if m['type'] in ['video','gif'])
                vb = await download_content(vid['url'])
                if vb:
                    if audio_mode and not clean_mode:
                         await message.answer_audio(BufferedInputFile(vb, filename="tw.mp3"), caption="🎵 Звук")
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
                msgs = await message.answer_media_group(mg.build())
                if not clean_mode and msgs:
                     try:
                        await bot.edit_message_reply_markup(chat_id=msgs[0].chat.id, message_id=msgs[0].message_id, reply_markup=get_media_keyboard(user_url, 'photo'))
                     except: pass

        # --- YOUTUBE ---
        elif "youtube.com" in user_url or "youtu.be" in user_url:
            if not is_button_click: await status_msg.edit_text("📺 YouTube: Завантажую...")
            
            if not os.path.exists("downloads"): os.makedirs("downloads")
            direct_url = await cobalt_get_url(user_url, quality)
            if not direct_url: raise Exception("No URL")
            
            raw_path = f"downloads/raw_{uuid.uuid4()}.mp4"
            with open(raw_path, 'wb') as f:
                f.write(await download_content(direct_url))
            
            final_path = raw_path
            if cut_range:
                final_path = f"downloads/final_{uuid.uuid4()}.mp4"
                await asyncio.to_thread(process_media_locally, raw_path, final_path, False, cut_range)

            # Аудіо
            if audio_mode:
                audio_path = f"downloads/aud_{uuid.uuid4()}.mp3"
                await asyncio.to_thread(process_media_locally, final_path, audio_path, True, None)
                await message.answer_audio(FSInputFile(audio_path), caption="🎵 Звук з YouTube")
                if os.path.exists(audio_path): os.remove(audio_path)
            
            # Відео
            else:
                caption_text = None
                if not clean_mode:
                    caption_text = f"📺 <b>YouTube Video</b>\n\n🔗 <a href='{user_url}'>Оригінал</a>"
                
                kb = get_media_keyboard(user_url, 'video') if (not clean_mode and not audio_mode and not cut_range) else None
                await message.answer_video(FSInputFile(final_path), caption=caption_text, parse_mode="HTML", reply_markup=kb)

            if os.path.exists(raw_path): os.remove(raw_path)
            if os.path.exists(final_path) and final_path != raw_path: os.remove(final_path)

        if status_msg: await status_msg.delete()

    except Exception as e:
        logging.error(f"Processing error: {e}")
        if status_msg: await status_msg.edit_text("❌ Сталася помилка.")

# ==========================
# 🎮 ОБРОБНИКИ
# ==========================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Надішли посилання на TikTok, Instagram, Twitter або YouTube.")

@dp.callback_query()
async def on_button_click(callback: CallbackQuery):
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

@dp.message(F.text.regexp(r'(https?://[^\s]+)') | F.caption.regexp(r'(https?://[^\s]+)'))
@dp.edited_message(F.text.regexp(r'(https?://[^\s]+)') | F.caption.regexp(r'(https?://[^\s]+)'))
async def handle_link(message: types.Message):
    content = message.text or message.caption
    user_url, clean, audio, cut, qual = parse_message_data(content)
    await process_media_request(message, user_url, clean, audio, cut, qual)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
