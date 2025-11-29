import logging
import sys
import os
import asyncio
import re
import glob
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, BufferedInputFile
from aiogram.utils.media_group import MediaGroupBuilder
import aiohttp
from aiohttp import web
from deep_translator import GoogleTranslator
from langdetect import detect
import instaloader
import yt_dlp
import static_ffmpeg

# Активуємо FFmpeg
static_ffmpeg.add_paths()

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"
RENDER_URL = "https://tiktok-bot-z88j.onrender.com" 

# Список дзеркал (Invidious / Piped)
# Це сайти-посередники, які віддають відео без капчі
YOUTUBE_MIRRORS = [
    "https://inv.tux.pizza",
    "https://yewtu.be",
    "https://invidious.drgns.space",
    "https://piped.video",
    "https://invidious.fdn.fr"
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

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def time_to_seconds(time_str):
    parts = list(map(int, time_str.split(':')))
    if len(parts) == 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2: return parts[0] * 60 + parts[1]
    return 0

def parse_message_data(text):
    if not text: return None, False, False, None
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False, False, None
    
    found_url = url_match.group(1)
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text or 'video' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text or 'music' in cmd_text)
    
    cut_range = None
    cut_match = re.search(r'cut\s+(\d{1,2}:\d{2}(?::\d{2})?)-(\d{1,2}:\d{2}(?::\d{2})?)', cmd_text)
    if cut_match:
        start_sec = time_to_seconds(cut_match.group(1))
        end_sec = time_to_seconds(cut_match.group(2))
        if end_sec > start_sec: cut_range = (start_sec, end_sec)

    return found_url, clean_mode, audio_mode, cut_range

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

def format_caption(nickname, username, profile_url, title, original_url):
    caption = f"👤 <b>{nickname}</b> (<a href='{profile_url}'>@{username}</a>)\n\n"
    if title: caption += f"📝 {title}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    return caption[:1000] + "..." if len(caption) > 1024 else caption

# --- ФОНОВІ ЗАДАЧІ ---
async def keep_alive_ping():
    logging.info("🚀 Ping service started! Waiting 10s...")
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

# --- ОБРОБНИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я качаю з TikTok, Twitter, Instagram та YouTube (через дзеркала).")

# === YOUTUBE (INVIDIOUS MIRRORS) ===
@dp.message(F.text.contains("youtube.com") | F.text.contains("youtu.be"))
async def handle_youtube(message: types.Message):
    user_url, clean_mode, audio_mode, cut_range = parse_message_data(message.text)
    if not user_url: return

    action_text = "Завантажую..."
    if cut_range:
        if (cut_range[1] - cut_range[0]) > 300:
            await message.reply("✂️ Максимум 5 хвилин для нарізки.")
            return
        action_text = "Вирізаю шматок..."
        
    status_msg = await message.reply(f"📺 YouTube: {action_text} (шукаю дзеркало...)")

    if not os.path.exists("downloads"): os.makedirs("downloads")

    # Витягуємо ID відео
    video_id = None
    if "youtu.be" in user_url:
        video_id = user_url.split("/")[-1].split("?")[0]
    elif "v=" in user_url:
        video_id = user_url.split("v=")[1].split("&")[0]
    elif "shorts" in user_url:
        video_id = user_url.split("shorts/")[1].split("?")[0]

    if not video_id:
        await status_msg.edit_text("❌ Не зміг знайти ID відео.")
        return

    # Налаштування yt-dlp
    ydl_opts = {
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    if audio_mode and not cut_range:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'}]
        ydl_opts['outtmpl'] = 'downloads/%(id)s.mp3'

    if cut_range:
        ydl_opts['download_ranges'] = lambda info, ydl: [{'start_time': cut_range[0], 'end_time': cut_range[1]}]
        ydl_opts['force_keyframes_at_cuts'] = True 

    file_path = None
    info_dict = None
    success = False

    # 🔄 ЦИКЛ: Пробуємо різні дзеркала
    # Спочатку пробуємо оригінал (а раптом?), потім дзеркала
    targets = [user_url] + [f"{mirror}/watch?v={video_id}" for mirror in YOUTUBE_MIRRORS]

    loop = asyncio.get_event_loop()

    for target_url in targets:
        logging.info(f"🔄 Trying URL: {target_url}")
        try:
            def download_task(url):
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=True)

            info_dict = await loop.run_in_executor(None, download_task, target_url)
            
            # Перевіряємо чи файл створився
            # yt-dlp використовує ID відео в назві файлу. 
            # Для дзеркал ID може бути тим самим, що і в оригінала.
            
            # Шукаємо будь-який файл у папці downloads, створений щойно
            list_of_files = glob.glob('downloads/*')
            if list_of_files:
                latest_file = max(list_of_files, key=os.path.getctime)
                file_path = latest_file
                success = True
                logging.info(f"✅ Success with: {target_url}")
                break
        except Exception as e:
            logging.warning(f"❌ Failed: {target_url} -> {e}")
            continue

    if not success or not file_path:
        await status_msg.edit_text("❌ Всі сервери заблоковані або відео недоступне.")
        return

    try:
        caption_text = None
        if not clean_mode and info_dict:
            title = info_dict.get('title', 'YouTube Video')
            trans_title = await translate_text(title)
            author = info_dict.get('uploader', 'User')
            caption_text = f"📺 <b>{author}</b>\n\n📝 {trans_title}\n\n🔗 <a href='{user_url}'>YouTube</a>"

        input_file = FSInputFile(file_path)

        if file_path.endswith(".mp3"):
            await message.answer_audio(input_file, caption=caption_text, parse_mode="HTML")
        else:
            await message.answer_video(input_file, caption=caption_text, parse_mode="HTML")
            if audio_mode and cut_range:
                 audio_file = FSInputFile(file_path, filename="cut_audio.mp3")
                 await message.answer_audio(audio_file, caption="🎵 Звук")

    except Exception as e:
        logging.error(f"Send Error: {e}")
        await status_msg.edit_text("❌ Помилка відправки.")
    finally:
        await status_msg.delete()
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

# === INSTAGRAM ===
@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("📸 Instagram: Аналізую...")
    shortcode_match = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url)
    if not shortcode_match:
        await status_msg.edit_text("❌ Bad link.")
        return
    shortcode = shortcode_match.group(1)
    try:
        def get_data():
            L = instaloader.Instaloader(quiet=True)
            L.context._user_agent = "Instagram 269.0.0.18.75 Android"
            return instaloader.Post.from_shortcode(L.context, shortcode)
        post = await asyncio.to_thread(get_data)
        caption = f"👤 <b>{post.owner_username}</b>\n🔗 <a href='{user_url}'>Post</a>" if not clean_mode else None
        media_urls = []
        if post.typename == 'GraphSidecar':
            for node in post.get_sidecar_nodes():
                media_urls.append((node.video_url if node.is_video else node.display_url, node.is_video))
        else:
            media_urls.append((post.video_url if post.is_video else post.url, post.is_video))
        group = MediaGroupBuilder()
        tasks = [download_content(u[0]) for u in media_urls]
        files = await asyncio.gather(*tasks)
        added = 0
        for i, file_bytes in enumerate(files):
            if file_bytes:
                is_vid = media_urls[i][1]
                f = BufferedInputFile(file_bytes, filename=f"inst{i}.{'mp4' if is_vid else 'jpg'}")
                if is_vid: 
                    if added==0 and caption: group.add_video(f, caption=caption, parse_mode="HTML")
                    else: group.add_video(f)
                else:
                    if added==0 and caption: group.add_photo(f, caption=caption, parse_mode="HTML")
                    else: group.add_photo(f)
                added += 1
        if added > 0: await message.answer_media_group(group.build())
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text("❌ Instagram: API Error (Login required).")

# === TIKTOK & TWITTER ===
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("🎵 TikTok...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as r:
                data = (await r.json())['data']
        vid_url = data.get('hdplay') or data.get('play')
        vbytes = await download_content(vid_url)
        cap = f"👤 {data['author']['nickname']}\n🔗 <a href='{user_url}'>TikTok</a>" if not clean_mode else None
        if vbytes:
            await message.answer_video(BufferedInputFile(vbytes, filename="tk.mp4"), caption=cap, parse_mode="HTML")
            if audio_mode:
                mbytes = await download_content(data.get('music'))
                if mbytes: await message.answer_audio(BufferedInputFile(mbytes, filename="music.mp3"))
        await status_msg.delete()
    except: await status_msg.edit_text("❌ Error TikTok")

@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("🐦 Twitter...")
    match = re.search(r"/status/(\d+)", user_url)
    if not match: return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.fxtwitter.com/status/{match.group(1)}") as r:
                data = (await r.json())['tweet']
        media = data.get('media', {}).get('all', [])
        if not media: return
        cap = f"👤 {data['author']['name']}\n🔗 <a href='{user_url}'>Twitter</a>" if not clean_mode else None
        if any(m['type'] in ['video','gif'] for m in media):
            vid = next(m for m in media if m['type'] in ['video','gif'])
            vbytes = await download_content(vid['url'])
            if vbytes:
                await message.answer_video(BufferedInputFile(vbytes, filename="tw.mp4"), caption=cap, parse_mode="HTML")
                if audio_mode: await message.answer_audio(BufferedInputFile(vbytes, filename="tw.mp3"))
        else:
            tasks = [download_content(m['url']) for m in media]
            images = await asyncio.gather(*tasks)
            group = MediaGroupBuilder()
            for i, img in enumerate(images):
                if img:
                    f = BufferedInputFile(img, filename=f"tw{i}.jpg")
                    if i==0 and cap: group.add_photo(f, caption=cap, parse_mode="HTML")
                    else: group.add_photo(f)
            await message.answer_media_group(group.build())
        await status_msg.delete()
    except: await status_msg.edit_text("❌ Error Twitter")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
