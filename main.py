import logging
import sys
import os
import asyncio
import re
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

# Дзеркала для YouTube (щоб не банили)
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

# 🔥 ВИПРАВЛЕНИЙ ФОРМАТ ПІДПИСУ 🔥
# Тепер ім'я автора - це і є посилання на нього
def format_caption(nickname, profile_url, title, original_url):
    # 1. Ім'я автора (жирним і клікабельним)
    caption = f"👤 <a href='{profile_url}'><b>{nickname}</b></a>\n\n"
    
    # 2. Опис (якщо є)
    if title:
        caption += f"📝 {title}\n\n"
    
    # 3. Посилання на оригінал
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    
    if len(caption) > 1024: caption = caption[:1000] + "..."
    return caption

# --- YOUTUBE HELPERS (COBALT + FFMPEG) ---
async def cobalt_get_url(user_url, quality=720):
    payload = {
        "url": user_url,
        "videoQuality": str(quality),
        "youtubeVideoCodec": "h264",
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
    await message.answer("Привіт! Я качаю з TikTok, Twitter (X), Instagram та YouTube.")

# === TIKTOK ===
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    user_url, clean_mode, audio_mode, _, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("🎵 TikTok: Обробляю...")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as r:
                result = await r.json()
        
        if result.get('code') != 0:
            await status_msg.edit_text("❌ TikTok: Не знайдено.")
            return
            
        data = result['data']
        
        # 🟢 ПІДПИС
        caption_text = None
        if not clean_mode:
            trans_desc = await translate_text(data.get('title', ''))
            author = data.get('author', {})
            unique_id = author.get('unique_id', '')
            caption_text = format_caption(
                author.get('nickname', 'User'), # Нікнейм для відображення
                f"https://www.tiktok.com/@{unique_id}", # Посилання на профіль
                trans_desc, 
                user_url
            )

        has_images = 'images' in data and data['images']
        music_file = None
        
        if audio_mode or has_images:
            music_bytes = await download_content(data.get('music'))
            if music_bytes: music_file = BufferedInputFile(music_bytes, filename="music.mp3")

        if has_images:
            await status_msg.edit_text("📸 TikTok: Качаю фото...")
            tasks = [download_content(url) for url in data['images']]
            images = await asyncio.gather(*tasks)
            mg = MediaGroupBuilder()
            added = 0
            for idx, img in enumerate(images):
                if img:
                    f = BufferedInputFile(img, filename=f"img_{idx}.jpg")
                    if added==0 and caption_text: mg.add_photo(f, caption=caption_text, parse_mode="HTML")
                    else: mg.add_photo(f)
                    added += 1
            if added > 0: await message.answer_media_group(mg.build())
            if music_file: await message.answer_audio(music_file)
            await status_msg.delete()
            
        else:
            await status_msg.edit_text("🎥 TikTok: Відео...")
            vid_bytes, cover_bytes = await asyncio.gather(download_content(data.get('hdplay') or data.get('play')), download_content(data.get('origin_cover')))
            if vid_bytes:
                vfile = BufferedInputFile(vid_bytes, filename=f"tk_{data['id']}.mp4")
                tfile = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
                await message.answer_video(vfile, caption=caption_text, parse_mode="HTML", thumbnail=tfile, width=720, height=1280)
                if music_file: await message.answer_audio(music_file)
                await status_msg.delete()
                
    except Exception as e:
        logging.error(f"TikTok Error: {e}")
        await status_msg.edit_text("❌ Помилка TikTok.")

# === INSTAGRAM ===
@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    user_url, clean_mode, audio_mode, _, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("📸 Instagram: Аналізую...")
    
    shortcode_match = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', user_url)
    if not shortcode_match:
        await status_msg.edit_text("❌ Instagram: Не зрозумів посилання.")
        return
    shortcode = shortcode_match.group(1)

    try:
        def get_insta_data(code):
            L = instaloader.Instaloader(quiet=True)
            L.context._user_agent = "Instagram 269.0.0.18.75 Android"
            return instaloader.Post.from_shortcode(L.context, code)

        post = await asyncio.to_thread(get_insta_data, shortcode)
        
        # 🟢 ПІДПИС
        caption_text = None
        if not clean_mode:
            raw_caption = post.caption or ""
            raw_caption = raw_caption.split('\n')[0] if raw_caption else ""
            trans_desc = await translate_text(raw_caption)
            author = post.owner_username
            caption_text = format_caption(
                author,
                f"https://instagram.com/{author}",
                trans_desc,
                user_url
            )

        media_group = MediaGroupBuilder()
        tasks = []
        if post.typename == 'GraphSidecar':
            nodes = list(post.get_sidecar_nodes())
            for node in nodes:
                if node.is_video: tasks.append((download_content(node.video_url), 'video'))
                else: tasks.append((download_content(node.display_url), 'photo'))
        else:
            if post.is_video: tasks.append((download_content(post.video_url), 'video'))
            else: tasks.append((download_content(post.url), 'photo'))

        results = await asyncio.gather(*[t[0] for t in tasks])
        files_added = 0
        
        if len(results) == 1 and results[0]:
            content_bytes = results[0]
            type_str = tasks[0][1]
            if type_str == 'video':
                vfile = BufferedInputFile(content_bytes, filename=f"insta_{shortcode}.mp4")
                await message.answer_video(vfile, caption=caption_text, parse_mode="HTML")
                if audio_mode:
                    afile = BufferedInputFile(content_bytes, filename=f"insta_aud_{shortcode}.mp3")
                    await message.answer_audio(afile, caption="🎵 Звук")
            else:
                pfile = BufferedInputFile(content_bytes, filename=f"insta_{shortcode}.jpg")
                await message.answer_photo(pfile, caption=caption_text, parse_mode="HTML")
                
        elif len(results) > 1:
            for idx, content_bytes in enumerate(results):
                if content_bytes:
                    type_str = tasks[idx][1]
                    if type_str == 'video':
                        m_file = BufferedInputFile(content_bytes, filename=f"inst_{idx}.mp4")
                        if files_added == 0 and caption_text: media_group.add_video(media=m_file, caption=caption_text, parse_mode="HTML")
                        else: media_group.add_video(media=m_file)
                    else:
                        m_file = BufferedInputFile(content_bytes, filename=f"inst_{idx}.jpg")
                        if files_added == 0 and caption_text: media_group.add_photo(media=m_file, caption=caption_text, parse_mode="HTML")
                        else: media_group.add_photo(media=m_file)
                    files_added += 1
            if files_added > 0: await message.answer_media_group(media_group.build())
            
        await status_msg.delete()
        
    except Exception as e:
        logging.error(f"Instagram Instaloader Error: {e}")
        await status_msg.edit_text("❌ Instagram: Не вдалося завантажити.")

# === TWITTER ===
@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    user_url, clean_mode, audio_mode, _, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("🐦 Twitter: Аналізую...")
    match = re.search(r"/status/(\d+)", user_url)
    if not match: return
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.fxtwitter.com/status/{match.group(1)}") as r:
                json_data = await r.json()
        tweet = json_data.get('tweet', {})
        
        # 🟢 ПІДПИС
        caption_text = None
        if not clean_mode:
            text = await translate_text(tweet.get('text', ''))
            author = tweet.get('author', {})
            caption_text = format_caption(
                author.get('name', 'User'), 
                f"https://twitter.com/{author.get('screen_name', 'tw')}",
                text, 
                user_url
            )
            
        media_list = tweet.get('media', {}).get('all', [])
        if not media_list:
            await message.answer("❌ Немає медіа.")
            return
            
        has_video = any(m['type'] in ['video', 'gif'] for m in media_list)
        if has_video:
            vdata = next((m for m in media_list if m['type'] in ['video', 'gif']), None)
            vbytes = await download_content(vdata['url'])
            if vbytes:
                await message.answer_video(BufferedInputFile(vbytes, filename="tw.mp4"), caption=caption_text, parse_mode="HTML")
                if audio_mode: await message.answer_audio(BufferedInputFile(vbytes, filename="tw.mp3"))
        else:
            tasks = [download_content(m['url']) for m in media_list]
            images = await asyncio.gather(*tasks)
            mg = MediaGroupBuilder()
            added = 0
            for idx, img in enumerate(images):
                if img:
                    if added==0 and caption_text: mg.add_photo(BufferedInputFile(img, filename="p.jpg"), caption=caption_text, parse_mode="HTML")
                    else: mg.add_photo(BufferedInputFile(img, filename="p.jpg"))
                    added += 1
            if added>0: await message.answer_media_group(mg.build())
        await status_msg.delete()
    except:
        await status_msg.edit_text("❌ Помилка Twitter.")

# === YOUTUBE (PROXY) ===
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
        
    status_msg = await message.reply(f"📺 YouTube: {action_text}")

    if not os.path.exists("downloads"): os.makedirs("downloads")
    
    direct_url = await cobalt_get_url(user_url, quality)
    
    if not direct_url:
        await status_msg.edit_text("❌ Всі сервери зайняті або відео недоступне.")
        return

    raw_path = f"downloads/raw_{message.message_id}.mp4"
    file_bytes = await download_content(direct_url)
    
    if not file_bytes:
        await status_msg.edit_text("❌ Помилка скачування файлу.")
        return
        
    with open(raw_path, 'wb') as f:
        f.write(file_bytes)

    final_path = raw_path
    if cut_range or audio_mode:
        ext = "mp3" if audio_mode else "mp4"
        final_path = f"downloads/final_{message.message_id}.{ext}"
        await asyncio.to_thread(process_media_locally, raw_path, final_path, audio_mode, cut_range)
        if os.path.exists(raw_path) and raw_path != final_path:
            os.remove(raw_path)

    try:
        caption_text = None
        if not clean_mode:
            caption_text = f"📺 <b>YouTube Video</b>\n\n🔗 <a href='{user_url}'>Оригінал</a>"

        input_file = FSInputFile(final_path)
        
        if final_path.endswith(".mp3"):
            await message.answer_audio(input_file, caption=caption_text, parse_mode="HTML")
        else:
            await message.answer_video(input_file, caption=caption_text, parse_mode="HTML")
            
    except Exception as e:
        logging.error(f"Send Error: {e}")
        await status_msg.edit_text("❌ Помилка відправки.")
    finally:
        await status_msg.delete()
        if os.path.exists(final_path): os.remove(final_path)
        if os.path.exists(raw_path): os.remove(raw_path)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(start_web_server(), keep_alive_ping(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
