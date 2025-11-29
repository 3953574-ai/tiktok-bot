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
import subprocess

# Активуємо FFmpeg
static_ffmpeg.add_paths()

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"
RENDER_URL = "https://tiktok-bot-z88j.onrender.com" 

# Список дзеркал Cobalt (наші "посередники")
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

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

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
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text or 'video' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text or 'music' in cmd_text)
    
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
    except Exception as e:
        logging.error(f"Download Error: {e}")
    return None

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

# --- COBALT API (ПОРЯТУНОК) ---
async def cobalt_get_url(user_url, quality=720):
    payload = {
        "url": user_url,
        "videoQuality": str(quality),
        "youtubeVideoCodec": "h264", # Просимо MP4 сумісний кодек
        "audioFormat": "mp3",
        "filenamePattern": "classic"
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        for mirror in COBALT_MIRRORS:
            try:
                async with session.post(mirror, json=payload, headers=headers, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('status') in ['stream', 'redirect']:
                            return data.get('url')
                        elif data.get('status') == 'picker':
                            # Для відео зазвичай stream, але про всяк випадок
                            return data['picker'][0]['url']
            except: continue
    return None

# --- LOCAL FFMPEG PROCESSING ---
def process_media_locally(input_path, output_path, audio_only=False, cut_range=None):
    cmd = ['ffmpeg', '-y', '-i', input_path]
    
    if cut_range:
        cmd.extend(['-ss', str(cut_range[0]), '-to', str(cut_range[1])])
    
    if audio_only:
        cmd.extend(['-vn', '-acodec', 'libmp3lame', '-q:a', '2'])
    else:
        # Просто копіюємо потік, якщо не треба перекодувати
        if not cut_range:
            cmd.extend(['-c', 'copy']) 
        else:
            # При нарізці краще перекодувати для точності
            cmd.extend(['-c:v', 'libx264', '-c:a', 'aac'])

    cmd.append(output_path)
    
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# --- ФОНОВІ ЗАДАЧІ ---
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

# --- ОБРОБНИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я качаю з TikTok, Instagram, Twitter та YouTube (через проксі).")

# === YOUTUBE (COBALT PROXY + LOCAL CUT) ===
@dp.message(F.text.contains("youtube.com") | F.text.contains("youtu.be"))
async def handle_youtube(message: types.Message):
    user_url, clean_mode, audio_mode, cut_range, quality = parse_message_data(message.text)
    if not user_url: return

    action_text = "Завантажую..."
    if cut_range:
        if (cut_range[1] - cut_range[0]) > 300:
            await message.reply("✂️ Максимум 5 хвилин для нарізки.")
            return
        action_text = "Качаю та ріжу..."
        
    status_msg = await message.reply(f"📺 YouTube: {action_text} (через Cobalt)")

    if not os.path.exists("downloads"): os.makedirs("downloads")
    
    # 1. Отримуємо пряме посилання через Cobalt
    direct_url = await cobalt_get_url(user_url, quality)
    
    if not direct_url:
        await status_msg.edit_text("❌ Всі сервери зайняті або відео недоступне.")
        return

    # 2. Скачуємо повний файл
    raw_path = f"downloads/raw_{message.message_id}.mp4"
    file_bytes = await download_content(direct_url)
    
    if not file_bytes:
        await status_msg.edit_text("❌ Помилка скачування файлу.")
        return
        
    with open(raw_path, 'wb') as f:
        f.write(file_bytes)

    # 3. Обробка (Нарізка / Аудіо)
    final_path = raw_path
    
    # Якщо треба нарізка АБО аудіо - запускаємо FFmpeg
    if cut_range or audio_mode:
        ext = "mp3" if audio_mode else "mp4"
        final_path = f"downloads/final_{message.message_id}.{ext}"
        
        # Обробка в окремому потоці, щоб не блокувати бота
        await asyncio.to_thread(process_media_locally, raw_path, final_path, audio_mode, cut_range)
        
        # Видаляємо оригінал, якщо створили новий файл
        if os.path.exists(raw_path) and raw_path != final_path:
            os.remove(raw_path)

    # 4. Відправка
    try:
        caption_text = None
        if not clean_mode:
            caption_text = f"📺 YouTube Video\n🔗 <a href='{user_url}'>Оригінал</a>"

        input_file = FSInputFile(final_path)
        
        if final_path.endswith(".mp3"):
            await message.answer_audio(input_file, caption=caption_text, parse_mode="HTML")
        else:
            await message.answer_video(input_file, caption=caption_text, parse_mode="HTML")
            
    except Exception as e:
        logging.error(f"Send Error: {e}")
        await status_msg.edit_text("❌ Помилка відправки (файл завеликий?)")
    finally:
        await status_msg.delete()
        if os.path.exists(final_path): os.remove(final_path)
        if os.path.exists(raw_path): os.remove(raw_path)

# === INSTAGRAM (Instaloader) ===
@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    # Код для Інсти залишаємо той самий (він працював, якщо не брати до уваги глюки API)
    # Я просто продублюю робочу версію скорочено
    user_url, clean_mode, audio_mode, _, _ = parse_message_data(message.text)
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

        # Скачуємо та відправляємо (спрощено)
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
        logging.error(f"Insta Error: {e}")
        await status_msg.edit_text("❌ Instagram: API Error (Login required).")

# === TIKTOK & TWITTER (Залишаємо як було, вони працюють) ===
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    user_url, clean_mode, audio_mode, _, _ = parse_message_data(message.text)
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
    user_url, clean_mode, audio_mode, _, _ = parse_message_data(message.text)
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
