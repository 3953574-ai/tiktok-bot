import logging
import os
import asyncio
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
from aiogram.utils.media_group import MediaGroupBuilder
import aiohttp
from aiohttp import web
from deep_translator import GoogleTranslator
from langdetect import detect

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
TIKTOK_API_URL = "https://www.tikwm.com/api/"

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN у змінних оточення!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
translator = GoogleTranslator(source='auto', target='uk')

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

async def download_content(url):
    """Скачує файл за посиланням"""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.read()
    except Exception as e:
        logging.error(f"Error downloading {url}: {e}")
    return None

async def translate_text(text):
    """Перекладає текст на українську, якщо він не англійською"""
    if not text or not text.strip():
        return ""
    try:
        lang = detect(text)
        if lang != 'en':
            return await asyncio.to_thread(translator.translate, text)
    except Exception as e:
        logging.error(f"Translation error: {e}")
    return text

def format_caption(nickname, username, profile_url, title, original_url):
    """Формує підпис з клікабельним нікнеймом"""
    # Нікнейм тепер є посиланням
    caption = f"👤 <b>{nickname}</b> (<a href='{profile_url}'>@{username}</a>)\n\n"
    
    if title:
        caption += f"📝 {title}\n\n"
    
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    
    if len(caption) > 1024:
        caption = caption[:1000] + "..."
    return caption

# --- ОБРОБНИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я качаю з TikTok та Twitter (X).")

# === TIKTOK ===
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    user_url = message.text.strip()
    status_msg = await message.reply("🎵 TikTok: Обробляю...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as response:
                result = await response.json()

        if result.get('code') != 0:
            await status_msg.edit_text("❌ TikTok: Не вдалося знайти.")
            return

        data = result['data']
        
        # Текст
        orig_desc = data.get('title', '')
        trans_desc = await translate_text(orig_desc)
        
        author = data.get('author', {})
        unique_id = author.get('unique_id', '') # Це нікнейм без @
        
        # Формуємо посилання на профіль TikTok
        profile_link = f"https://www.tiktok.com/@{unique_id}"
        
        caption_text = format_caption(
            nickname=author.get('nickname', 'User'),
            username=unique_id,
            profile_url=profile_link, # Передаємо посилання
            title=trans_desc,
            original_url=user_url
        )

        # Музика
        music_url = data.get('music')
        music_bytes = await download_content(music_url)
        music_info = data.get('music_info', {})
        music_name = f"{music_info.get('author','')} - {music_info.get('title','')}.mp3"
        music_file = BufferedInputFile(music_bytes, filename=music_name) if music_bytes else None

        # 1. Слайдер (Фото)
        if 'images' in data and data['images']:
            await status_msg.edit_text("📸 TikTok: Вантажу фото...")
            images = data['images']
            chunk_size = 10
            
            first_album = True
            for i in range(0, len(images), chunk_size):
                chunk = images[i:i + chunk_size]
                media_group = MediaGroupBuilder()
                for index, img_url in enumerate(chunk):
                    if first_album and index == 0:
                        media_group.add_photo(media=img_url, caption=caption_text, parse_mode="HTML")
                    else:
                        media_group.add_photo(media=img_url)
                
                await message.answer_media_group(media_group.build())
                first_album = False
            
            if music_file:
                await message.answer_audio(music_file, caption="🎵 Звук")
            await status_msg.delete()

        # 2. Відео
        else:
            await status_msg.edit_text("🎥 TikTok: Вантажу відео...")
            vid_url = data.get('hdplay') or data.get('play')
            cover_url = data.get('origin_cover') or data.get('cover')
            
            vid_bytes, cover_bytes = await asyncio.gather(
                download_content(vid_url),
                download_content(cover_url)
            )

            if vid_bytes:
                vfile = BufferedInputFile(vid_bytes, filename=f"tk_{data['id']}.mp4")
                tfile = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
                
                # Hard fix розмірів
                w, h = 720, 1280
                try:
                    w = int(data.get('width'))
                    h = int(data.get('height'))
                except: pass
                if not w or not h: w, h = 720, 1280

                await message.answer_video(
                    vfile, caption=caption_text, parse_mode="HTML",
                    thumbnail=tfile, width=w, height=h, supports_streaming=True
                )
                if music_file:
                    await message.answer_audio(music_file, caption="🎵 Звук")
                await status_msg.delete()

    except Exception as e:
        logging.error(f"TikTok Error: {e}")
        await status_msg.edit_text("❌ Помилка TikTok.")


# === TWITTER / X ===
@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    user_url = message.text.strip()
    status_msg = await message.reply("🐦 Twitter: Аналізую...")

    match = re.search(r"/status/(\d+)", user_url)
    if not match:
        await status_msg.edit_text("❌ Не можу знайти ID твіта в посиланні.")
        return
    tweet_id = match.group(1)

    api_url = f"https://api.fxtwitter.com/status/{tweet_id}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    await status_msg.edit_text("❌ Твіт не знайдено (можливо, приватний).")
                    return
                json_data = await response.json()

        tweet = json_data.get('tweet', {})
        if not tweet:
            await status_msg.edit_text("❌ Помилка API Twitter.")
            return

        # Текст
        text = tweet.get('text', '')
        trans_text = await translate_text(text)
        
        author = tweet.get('author', {})
        screen_name = author.get('screen_name', 'twitter')
        
        # Формуємо посилання на профіль Twitter
        profile_link = f"https://twitter.com/{screen_name}"

        caption_text = format_caption(
            nickname=author.get('name', 'User'),
            username=screen_name,
            profile_url=profile_link, # Передаємо посилання
            title=trans_text,
            original_url=user_url
        )

        media_list = tweet.get('media', {}).get('all', [])
        
        if not media_list:
            await message.answer(caption_text, parse_mode="HTML", disable_web_page_preview=True)
            await status_msg.delete()
            return

        has_video = any(m['type'] in ['video', 'gif'] for m in media_list)

        if has_video:
            await status_msg.edit_text("⬇️ Twitter: Вантажу відео...")
            video_data = next((m for m in media_list if m['type'] in ['video', 'gif']), None)
            
            if video_data:
                video_bytes = await download_content(video_data['url'])
                if video_bytes:
                    vfile = BufferedInputFile(video_bytes, filename=f"tw_{tweet_id}.mp4")
                    afile = BufferedInputFile(video_bytes, filename=f"tw_audio_{tweet_id}.mp3")
                    
                    w = video_data.get('width')
                    h = video_data.get('height')
                    
                    await message.answer_video(
                        vfile, caption=caption_text, parse_mode="HTML",
                        width=w, height=h, supports_streaming=True
                    )
                    await message.answer_audio(afile, caption="🎵 Звук з твіта")
                    await status_msg.delete()
                    return

        else:
            await status_msg.edit_text("⬇️ Twitter: Вантажу фото...")
            if len(media_list) == 1:
                photo_url = media_list[0]['url']
                await message.answer_photo(photo_url, caption=caption_text, parse_mode="HTML")
            else:
                media_group = MediaGroupBuilder()
                for i, m in enumerate(media_list):
                    if i == 0:
                        media_group.add_photo(media=m['url'], caption=caption_text, parse_mode="HTML")
                    else:
                        media_group.add_photo(media=m['url'])
                await message.answer_media_group(media_group.build())
            
            await status_msg.delete()

    except Exception as e:
        logging.error(f"Twitter Handler Error: {e}")
        await status_msg.edit_text("❌ Помилка завантаження.")

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
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
