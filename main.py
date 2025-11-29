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

# Логи
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
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
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
        if end_sec > start_sec:
            cut_range = (start_sec, end_sec)

    return found_url, clean_mode, audio_mode, cut_range

async def download_content(url):
    if not url: return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.read()
    except Exception as e:
        logging.error(f"Download Error: {e}")
    return None

async def translate_text(text):
    if not text or not text.strip(): return ""
    try:
        lang = detect(text)
        if lang != 'en':
            return await asyncio.to_thread(translator.translate, text)
    except: pass
    return text

def format_caption(nickname, username, profile_url, title, original_url):
    caption = f"👤 <b>{nickname}</b> (<a href='{profile_url}'>@{username}</a>)\n\n"
    if title:
        caption += f"📝 {title}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    if len(caption) > 1024: caption = caption[:1000] + "..."
    return caption

# --- ФОНОВІ ЗАДАЧІ ---
async def keep_alive_ping():
    logging.info("🚀 Ping service started! Waiting 10s...")
    await asyncio.sleep(10)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL) as response:
                    logging.info(f"🔔 Self-Ping status: {response.status}")
        except Exception as e:
            logging.error(f"❌ Ping failed: {e}")
        await asyncio.sleep(120)

async def start_web_server():
    app = web.Application()
    async def health_check(request):
        return web.Response(text="Bot is alive!")
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"🌍 Web server started on port {port}")

# --- ОБРОБНИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привіт! Я качаю з:\n"
        "🎵 <b>TikTok</b>\n"
        "📸 <b>Instagram</b>\n"
        "🐦 <b>Twitter (X)</b>\n"
        "📺 <b>YouTube</b>\n\n"
        "✂️ <b>Нарізка:</b> посилання + <code>cut 00:10-00:15</code>",
        parse_mode="HTML"
    )

# === YOUTUBE ===
@dp.message(F.text.contains("youtube.com") | F.text.contains("youtu.be"))
async def handle_youtube(message: types.Message):
    user_url, clean_mode, audio_mode, cut_range = parse_message_data(message.text)
    if not user_url: return

    if cut_range:
        duration = cut_range[1] - cut_range[0]
        if duration > 300:
            await message.reply("✂️ Максимум 5 хвилин для нарізки.")
            return
        status_msg = await message.reply(f"📺 YouTube: Вирізаю ({duration}с)... ✂️")
    else:
        status_msg = await message.reply("📺 YouTube: Завантажую...")

    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    ydl_opts = {
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    if audio_mode and not cut_range:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'}]
        ydl_opts['outtmpl'] = 'downloads/%(id)s.mp3'

    if cut_range:
        ydl_opts['download_ranges'] = lambda info, ydl: [{'start_time': cut_range[0], 'end_time': cut_range[1]}]
        ydl_opts['force_keyframes_at_cuts'] = True 

    try:
        loop = asyncio.get_event_loop()
        def download_task():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(user_url, download=True)

        info_dict = await loop.run_in_executor(None, download_task)
        file_id = info_dict.get('id')
        files = glob.glob(f"downloads/{file_id}*")
        
        if not files:
            await status_msg.edit_text("❌ YouTube: Помилка файлу.")
            return

        file_path = files[0]
        
        caption_text = None
        if not clean_mode:
            title = info_dict.get('title', '')
            trans_title = await translate_text(title)
            author = info_dict.get('uploader', 'YouTube')
            caption_text = f"📺 <b>{author}</b>\n\n📝 {trans_title}\n\n🔗 <a href='{user_url}'>YouTube</a>"

        input_file = FSInputFile(file_path)

        if file_path.endswith(".mp3"):
            await message.answer_audio(input_file, caption=caption_text, parse_mode="HTML")
        else:
            await message.answer_video(input_file, caption=caption_text, parse_mode="HTML")
            if audio_mode and cut_range:
                 audio_file = FSInputFile(file_path, filename="cut_audio.mp3")
                 await message.answer_audio(audio_file, caption="🎵 Звук")

        await status_msg.delete()
        os.remove(file_path)

    except Exception as e:
        logging.error(f"YouTube Error: {e}")
        await status_msg.edit_text("❌ Помилка завантаження.")
        for f in glob.glob(f"downloads/*"):
            try: os.remove(f)
            except: pass

# === INSTAGRAM ===
@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
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
            L.context._user_agent = "Instagram 269.0.0.18.75 Android (26/8.0.0; 480dpi; 1080x1920; samsung; SM-G930F; herolte; samsungexynos8890; en_US; 446464522)"
            return instaloader.Post.from_shortcode(L.context, code)

        post = await asyncio.to_thread(get_insta_data, shortcode)
        caption_text = None
        if not clean_mode:
            raw_caption = post.caption or ""
            raw_caption = raw_caption.split('\n')[0] if raw_caption else ""
            trans_desc = await translate_text(raw_caption)
            author = post.owner_username
            caption_text = f"👤 <b>{author}</b>\n\n📝 {trans_desc}\n\n🔗 <a href='{user_url}'>Оригінал</a>"

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
        await status_msg.edit_text("❌ Instagram: Помилка (спробуй пізніше).")

# === TIKTOK ===
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("🎵 TikTok: Обробляю...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as response:
                result = await response.json()
        if result.get('code') != 0:
            await status_msg.edit_text("❌ TikTok: Не знайдено.")
            return
        data = result['data']
        caption_text = None
        if not clean_mode:
            trans_desc = await translate_text(data.get('title', ''))
            author = data.get('author', {})
            unique_id = author.get('unique_id', '')
            caption_text = format_caption(author.get('nickname', 'User'), unique_id, f"https://www.tiktok.com/@{unique_id}", trans_desc, user_url)
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

# === TWITTER ===
@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
    if not user_url: return
    status_msg = await message.reply("🐦 Twitter: Аналізую...")
    match = re.search(r"/status/(\d+)", user_url)
    if not match: return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.fxtwitter.com/status/{match.group(1)}") as r:
                json_data = await r.json()
        tweet = json_data.get('tweet', {})
        caption_text = None
        if not clean_mode:
            text = await translate_text(tweet.get('text', ''))
            author = tweet.get('author', {})
            caption_text = format_caption(author.get('name', 'User'), author.get('screen_name', 'tw'), user_url, text, user_url)
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


# 🔥 ОСНОВНА ФУНКЦІЯ ЗАПУСКУ (FIXED) 🔥
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    
    # ❗️ Виправляємо помилку: запускаємо все через gather, без lambda
    await asyncio.gather(
        start_web_server(),   # Веб-сервер
        keep_alive_ping(),    # Пінг
        dp.start_polling(bot) # Бот
    )

if __name__ == "__main__":
    asyncio.run(main())
