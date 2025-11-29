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
import static_ffmpeg # 🎞️ Бібліотека для FFmpeg

# Активуємо FFmpeg (додаємо його в систему)
static_ffmpeg.add_paths()

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"
RENDER_URL = "https://tiktok-bot-z88j.onrender.com" 

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
    """Перетворює час (01:02:03 або 05:30) в секунди"""
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
    
    # Пошук команди cut (наприклад: cut 00:10-00:15)
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
    logging.info("🚀 Ping service started! Waiting 10s for first ping...")
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
        "📺 <b>YouTube (Shorts & Video)</b>\n\n"
        "✂️ <b>Нарізка відео:</b>\n"
        "Додай до посилання: <code>cut 00:10-00:35</code>\n"
        "(виріже шматок з 10-ї по 35-ту секунду)",
        parse_mode="HTML"
    )

# === YOUTUBE (НОВЕ) ===
@dp.message(F.text.contains("youtube.com") | F.text.contains("youtu.be"))
async def handle_youtube(message: types.Message):
    user_url, clean_mode, audio_mode, cut_range = parse_message_data(message.text)
    if not user_url: return

    # Перевірка на тривалість вирізки (щоб не просили 10 годин)
    if cut_range:
        duration = cut_range[1] - cut_range[0]
        if duration > 300: # Ліміт 5 хвилин для нарізки
            await message.reply("✂️ Максимальна довжина вирізки — 5 хвилин.")
            return
        status_msg = await message.reply(f"📺 YouTube: Вирізаю шматок ({duration}с)... ✂️")
    else:
        status_msg = await message.reply("📺 YouTube: Завантажую...")

    # Створюємо папку
    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    # Базові налаштування
    ydl_opts = {
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        # Обмежуємо якість до 1080p, щоб не вбити пам'ять
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    # Якщо треба ТІЛЬКИ АУДІО (режим !a) і немає нарізки
    if audio_mode and not cut_range:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
        }]
        ydl_opts['outtmpl'] = 'downloads/%(id)s.mp3'

    # Якщо є НАРІЗКА (cut)
    if cut_range:
        # Функція для скачування тільки діапазону
        def download_range_func(info_dict, ydl):
            return [{
                'start_time': cut_range[0],
                'end_time': cut_range[1]
            }]
        ydl_opts['download_ranges'] = download_range_func
        # Примусово зшиваємо в точних місцях
        ydl_opts['force_keyframes_at_cuts'] = True 

    try:
        loop = asyncio.get_event_loop()
        
        def download_task():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(user_url, download=True)
                return info

        info_dict = await loop.run_in_executor(None, download_task)
        file_id = info_dict.get('id')
        
        # Шукаємо файл (відео або mp3)
        files = glob.glob(f"downloads/{file_id}*")
        if not files:
            await status_msg.edit_text("❌ YouTube: Помилка файлу.")
            return

        file_path = files[0] # Беремо перший знайдений

        # Підпис
        caption_text = None
        if not clean_mode:
            title = info_dict.get('title', '')
            trans_title = await translate_text(title)
            author = info_dict.get('uploader', 'YouTube')
            caption_text = f"📺 <b>{author}</b>\n\n📝 {trans_title}\n\n🔗 <a href='{user_url}'>YouTube</a>"

        input_file = FSInputFile(file_path)

        # Відправка
        if file_path.endswith(".mp3"):
            await message.answer_audio(input_file, caption=caption_text, parse_mode="HTML")
        else:
            await message.answer_video(input_file, caption=caption_text, parse_mode="HTML")
            # Якщо хотіли аудіо в нарізці
            if audio_mode and cut_range:
                 audio_file = FSInputFile(file_path, filename="cut_audio.mp3")
                 await message.answer_audio(audio_file, caption="🎵 Звук з вирізки")

        await status_msg.delete()
        os.remove(file_path)

    except Exception as e:
        logging.error(f"YouTube Error: {e}")
        await status_msg.edit_text("❌ Занадто великий файл або помилка доступу.")
        # Чистимо сміття якщо є
        for f in glob.glob(f"downloads/*"):
            try: os.remove(f)
            except: pass

# === INSTAGRAM (INSTALOADER) ===
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
        await status_msg.edit_text("❌ Instagram: Не вдалося завантажити (можливо 18+ або логін).")

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
        should_download_music = audio_mode or has_images
        music_file = None
        if should_download_music:
            music_url = data.get('music')
            music_bytes = await download_content(music_url)
            music_info = data.get('music_info', {})
            music_name = f"{music_info.get('author','')} - {music_info.get('title','')}.mp3"
            if music_bytes: music_file = BufferedInputFile(music_bytes, filename=music_name)

        if has_images:
            await status_msg.edit_text("📸 TikTok: Качаю фото...")
            images_urls = data['images']
            chunk_size = 10
            first = True
            for i in range(0, len(images_urls), chunk_size):
                chunk_urls = images_urls[i:i + chunk_size]
                tasks = [download_content(url) for url in chunk_urls]
                downloaded_images = await asyncio.gather(*tasks)
                media_group = MediaGroupBuilder()
                images_added = 0
                for idx, img_bytes in enumerate(downloaded_images):
                    if img_bytes:
                        img_file = BufferedInputFile(img_bytes, filename=f"img_{i}_{idx}.jpg")
                        if first and images_added == 0 and caption_text: media_group.add_photo(media=img_file, caption=caption_text, parse_mode="HTML")
                        else: media_group.add_photo(media=img_file)
                        images_added += 1
                if images_added > 0:
                    await message.answer_media_group(media_group.build())
                    first = False
            if music_file: await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
            await status_msg.delete()
        else:
            await status_msg.edit_text("🎥 TikTok: Відео...")
            vid_url = data.get('hdplay') or data.get('play')
            cover_url = data.get('origin_cover') or data.get('cover')
            vid_bytes, cover_bytes = await asyncio.gather(download_content(vid_url), download_content(cover_url))
            if vid_bytes:
                vfile = BufferedInputFile(vid_bytes, filename=f"tk_{data['id']}.mp4")
                tfile = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
                w, h = 720, 1280
                try: w, h = int(data.get('width')), int(data.get('height'))
                except: pass
                if not w or not h: w, h = 720, 1280
                await message.answer_video(vfile, caption=caption_text, parse_mode="HTML", thumbnail=tfile, width=w, height=h, supports_streaming=True)
                if music_file: await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
                await status_msg.delete()
    except Exception as e:
        logging.error(f"TikTok Error: {e}")
        await status_msg.edit_text("❌ Помилка TikTok.")

# === TWITTER / X ===
@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    user_url, clean_mode, audio_mode, _ = parse_message_data(message.text)
    if not user_url: return

    status_msg = await message.reply("🐦 Twitter: Аналізую...")
    match = re.search(r"/status/(\d+)", user_url)
    if not match:
        await status_msg.edit_text("❌ Не знайдено ID.")
        return
    tweet_id = match.group(1)
    api_url = f"https://api.fxtwitter.com/status/{tweet_id}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    await status_msg.edit_text("❌ Твіт не знайдено.")
                    return
                json_data = await response.json()

        tweet = json_data.get('tweet', {})
        if not tweet:
            await status_msg.edit_text("❌ Помилка API.")
            return

        caption_text = None
        if not clean_mode:
            trans_text = await translate_text(tweet.get('text', ''))
            author = tweet.get('author', {})
            screen_name = author.get('screen_name', 'twitter')
            caption_text = format_caption(author.get('name', 'User'), screen_name, f"https://twitter.com/{screen_name}", trans_text, user_url)

        media_list = tweet.get('media', {}).get('all', [])
        if not media_list:
            if not clean_mode: await message.answer(caption_text, parse_mode="HTML", disable_web_page_preview=True)
            else: await message.answer("❌ Немає медіа.")
            await status_msg.delete()
            return

        has_video = any(m['type'] in ['video', 'gif'] for m in media_list)
        if has_video:
            await status_msg.edit_text("⬇️ Twitter: Відео...")
            vdata = next((m for m in media_list if m['type'] in ['video', 'gif']), None)
            if vdata:
                vbytes = await download_content(vdata['url'])
                if vbytes:
                    vfile = BufferedInputFile(vbytes, filename=f"tw_{tweet_id}.mp4")
                    w = vdata.get('width')
                    h = vdata.get('height')
                    await message.answer_video(vfile, caption=caption_text, parse_mode="HTML", width=w, height=h, supports_streaming=True)
                    if audio_mode:
                        afile = BufferedInputFile(vbytes, filename=f"tw_audio_{tweet_id}.mp3")
                        await message.answer_audio(afile, caption="🎵 Звук з твіта")
                    await status_msg.delete()
                    return
        else:
            await status_msg.edit_text("⬇️ Twitter: Фото...")
            if len(media_list) == 1:
                p_bytes = await download_content(media_list[0]['url'])
                if p_bytes:
                    p_file = BufferedInputFile(p_bytes, filename="photo.jpg")
                    await message.answer_photo(p_file, caption=caption_text, parse_mode="HTML")
            else:
                tasks = [download_content(m['url']) for m in media_list]
                results = await asyncio.gather(*tasks)
                media_group = MediaGroupBuilder()
                added = 0
                for idx, p_bytes in enumerate(results):
                    if p_bytes:
                        p_file = BufferedInputFile(p_bytes, filename=f"p_{idx}.jpg")
                        if added == 0 and caption_text: media_group.add_photo(media=p_file, caption=caption_text, parse_mode="HTML")
                        else: media_group.add_photo(media=p_file)
                        added += 1
                if added > 0: await message.answer_media_group(media_group.build())
            await status_msg.delete()
    except Exception as e:
        logging.error(f"Twitter Error: {e}")
        await status_msg.edit_text("❌ Помилка.")

async def main():
    dp.startup.register(lambda: asyncio.gather(start_web_server(), keep_alive_ping()))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
