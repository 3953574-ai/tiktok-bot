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

# --- РОЗУМНИЙ ПАРСИНГ ---

def parse_message_data(text):
    """
    Повертає: (url, clean_mode, audio_mode)
    """
    if not text:
        return None, False, False
        
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match:
        return None, False, False
    
    found_url = url_match.group(1)
    cmd_text = text.replace(found_url, "").lower()
    
    clean_mode = ('-' in cmd_text or '!' in cmd_text or 'clear' in cmd_text or 'video' in cmd_text)
    audio_mode = ('!a' in cmd_text or 'audio' in cmd_text or 'music' in cmd_text)
        
    return found_url, clean_mode, audio_mode

async def download_content(url):
    if not url: return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
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

# --- ОБРОБНИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привіт! Я качаю з TikTok та Twitter (X).\n\n"
        "⚙️ <b>Керування:</b>\n"
        "🔗 <b>Лінк</b> — пост з підписом.\n"
        "➖ <b>Лінк -</b> — чистий пост без тексту.\n"
        "🎵 <b>Лінк !a</b> — додати файл музики (для фото-слайдів TikTok додається автоматично).",
        parse_mode="HTML"
    )

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
        
        # Підпис
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

        # Перевіряємо, чи це слайдер (фото)
        is_slideshow = 'images' in data and data['images']

        # 🔥 АВТОМАТИЧНА МУЗИКА
        # Качаємо, якщо:
        # 1. Користувач попросив (!a)
        # 2. АБО це слайдер (is_slideshow)
        should_download_music = audio_mode or is_slideshow

        music_file = None
        if should_download_music:
            music_url = data.get('music')
            music_bytes = await download_content(music_url)
            music_info = data.get('music_info', {})
            music_name = f"{music_info.get('author','')} - {music_info.get('title','')}.mp3"
            if music_bytes:
                music_file = BufferedInputFile(music_bytes, filename=music_name)

        # 1. Фото (Слайдер)
        if is_slideshow:
            await status_msg.edit_text("📸 TikTok: Фото...")
            images = data['images']
            chunk_size = 10
            first = True
            for i in range(0, len(images), chunk_size):
                chunk = images[i:i + chunk_size]
                media_group = MediaGroupBuilder()
                for idx, img_url in enumerate(chunk):
                    if first and idx == 0 and caption_text:
                        media_group.add_photo(media=img_url, caption=caption_text, parse_mode="HTML")
                    else:
                        media_group.add_photo(media=img_url)
                await message.answer_media_group(media_group.build())
                first = False
            
            # Відправляємо музику (вона точно скачалась, бо is_slideshow=True)
            if music_file:
                await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
            await status_msg.delete()

        # 2. Відео
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
                # Тут музика відправиться ТІЛЬКИ якщо було audio_mode
                if music_file:
                    await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
                await status_msg.delete()

    except Exception as e:
        logging.error(f"TikTok Error: {e}")
        await status_msg.edit_text("❌ Помилка TikTok.")


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
                        await message.answer_audio(afile, caption="🎵 Звук" if not clean_mode else None)
                    
                    await status_msg.delete()
                    return
        else:
            await status_msg.edit_text("⬇️ Twitter: Фото...")
            if len(media_list) == 1:
                await message.answer_photo(media_list[0]['url'], caption=caption_text, parse_mode="HTML")
            else:
                media_group = MediaGroupBuilder()
                for i, m in enumerate(media_list):
                    if i == 0 and caption_text:
                        media_group.add_photo(media=m['url'], caption=caption_text, parse_mode="HTML")
                    else:
                        media_group.add_photo(media=m['url'])
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
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
