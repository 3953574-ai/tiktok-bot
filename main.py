import logging
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
import yt_dlp

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"
RENDER_URL = "https://tiktok-bot-z88j.onrender.com" 

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN у змінних оточення!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
translator = GoogleTranslator(source='auto', target='uk')

# --- РОЗУМНИЙ ПАРСИНГ ---
def parse_message_data(text):
    if not text: return None, False, False
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False, False
    
    found_url = url_match.group(1)
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text or 'video' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text or 'music' in cmd_text)
        
    return found_url, clean_mode, audio_mode

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

# --- САМО-ПІНГ ---
async def keep_alive_ping():
    while True:
        await asyncio.sleep(180)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL) as response:
                    logging.info(f"🔔 Ping sent to myself. Status: {response.status}")
        except Exception as e:
            logging.error(f"❌ Ping failed: {e}")

# --- ОБРОБНИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я качаю з TikTok, Twitter (X) та Instagram 📸.")

# === INSTAGRAM (YT-DLP EDITION) ===
@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    user_url, clean_mode, audio_mode = parse_message_data(message.text)
    if not user_url: return

    status_msg = await message.reply("📸 Instagram: Завантажую...")
    
    # Створюємо папку для завантажень, якщо немає
    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    # Налаштування yt-dlp
    ydl_opts = {
        'outtmpl': 'downloads/%(id)s.%(ext)s', # Куди зберігати
        'format': 'best', # Найкраща якість
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True, # Качати тільки одне відео/фото, не весь профіль
        # Обходимо блокування (іноді допомагає)
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }

    try:
        # Запускаємо yt-dlp в окремому потоці, щоб не блокувати бота
        loop = asyncio.get_event_loop()
        
        def download_task():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(user_url, download=True)
                return info

        info_dict = await loop.run_in_executor(None, download_task)
        
        # Знаходимо скачаний файл
        file_id = info_dict.get('id')
        files = glob.glob(f"downloads/{file_id}.*")
        
        if not files:
            await status_msg.edit_text("❌ Instagram: Файл завантажено, але не знайдено.")
            return

        file_path = files[0]
        
        # Готуємо підпис
        caption_text = None
        if not clean_mode:
            # yt-dlp іноді дає опис
            desc = info_dict.get('description') or info_dict.get('title') or ""
            # Обрізаємо зайве
            desc = desc.split('\n')[0] if desc else "" 
            trans_desc = await translate_text(desc)
            uploader = info_dict.get('uploader', 'Instagram User')
            caption_text = f"👤 <b>{uploader}</b>\n\n📝 {trans_desc}\n\n🔗 <a href='{user_url}'>Оригінал</a>"

        # Відправляємо файл
        input_file = FSInputFile(file_path)
        
        if file_path.endswith(".mp4") or file_path.endswith(".mkv"):
            await message.answer_video(input_file, caption=caption_text, parse_mode="HTML")
            
            # Якщо треба аудіо
            if audio_mode:
                # Відправляємо те саме відео як аудіо (Telegram сам розбереться) або конвертуємо
                # Для швидкості просто відправимо файл як аудіо
                audio_file = FSInputFile(file_path, filename="insta_audio.mp3")
                await message.answer_audio(audio_file, caption="🎵 Звук з Instagram")
                
        elif file_path.endswith(".jpg") or file_path.endswith(".png") or file_path.endswith(".webp"):
            await message.answer_photo(input_file, caption=caption_text, parse_mode="HTML")
        
        await status_msg.delete()

        # Видаляємо файл після відправки
        os.remove(file_path)

    except Exception as e:
        logging.error(f"Instagram yt-dlp Error: {e}")
        err_msg = str(e)
        if "Login required" in err_msg:
             await status_msg.edit_text("❌ Instagram: Цей пост доступний тільки для авторизованих користувачів (приватний або 18+).")
        else:
             await status_msg.edit_text("❌ Instagram: Не вдалося завантажити. Спробуйте пізніше.")

# === TIKTOK ===
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    user_url, clean_mode, audio_mode = parse_message_data(message.text)
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
            caption_text = format_caption(
                author.get('nickname', 'User'), unique_id,
                f"https://www.tiktok.com/@{unique_id}",
                trans_desc, user_url
            )

        has_images = 'images' in data and data['images']
        should_download_music = audio_mode or has_images

        music_file = None
        if should_download_music:
            music_url = data.get('music')
            music_bytes = await download_content(music_url)
            music_info = data.get('music_info', {})
            music_name = f"{music_info.get('author','')} - {music_info.get('title','')}.mp3"
            if music_bytes:
                music_file = BufferedInputFile(music_bytes, filename=music_name)

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
                        if first and images_added == 0 and caption_text:
                            media_group.add_photo(media=img_file, caption=caption_text, parse_mode="HTML")
                        else:
                            media_group.add_photo(media=img_file)
                        images_added += 1
                
                if images_added > 0:
                    await message.answer_media_group(media_group.build())
                    first = False
            
            if music_file:
                await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
            await status_msg.delete()

        else:
            await status_msg.edit_text("🎥 TikTok: Відео...")
            vid_url = data.get('hdplay') or data.get('play')
            cover_url = data.get('origin_cover') or data.get('cover')
            
            vid_bytes, cover_bytes = await asyncio.gather(
                download_content(vid_url),
                download_content(cover_url)
            )

            if vid_bytes:
                vfile = BufferedInputFile(vid_bytes, filename=f"tk_{data['id']}.mp4")
                tfile = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
                
                w, h = 720, 1280
                try: w, h = int(data.get('width')), int(data.get('height'))
                except: pass
                if not w or not h: w, h = 720, 1280

                await message.answer_video(
                    vfile, caption=caption_text, parse_mode="HTML",
                    thumbnail=tfile, width=w, height=h, supports_streaming=True
                )
                if music_file:
                    await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
                await status_msg.delete()

    except Exception as e:
        logging.error(f"TikTok Error: {e}")
        await status_msg.edit_text("❌ Помилка TikTok (не вдалося скачати медіа).")

# === TWITTER / X ===
@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    user_url, clean_mode, audio_mode = parse_message_data(message.text)
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
            caption_text = format_caption(
                author.get('name', 'User'), screen_name,
                f"https://twitter.com/{screen_name}",
                trans_text, user_url
            )

        media_list = tweet.get('media', {}).get('all', [])
        if not media_list:
            if not clean_mode:
                await message.answer(caption_text, parse_mode="HTML", disable_web_page_preview=True)
            else:
                await message.answer("❌ Немає медіа.")
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
                    
                    await message.answer_video(
                        vfile, caption=caption_text, parse_mode="HTML",
                        width=w, height=h, supports_streaming=True
                    )
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
                        if added == 0 and caption_text:
                            media_group.add_photo(media=p_file, caption=caption_text, parse_mode="HTML")
                        else:
                            media_group.add_photo(media=p_file)
                        added += 1
                if added > 0:
                    await message.answer_media_group(media_group.build())
            
            await status_msg.delete()

    except Exception as e:
        logging.error(f"Twitter Error: {e}")
        await status_msg.edit_text("❌ Помилка.")

# --- ВЕБ-СЕРВЕР ---
async def health_check(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(
        start_web_server(),
        keep_alive_ping(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
