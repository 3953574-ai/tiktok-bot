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

# ==========================================================
# 🧠 УНІВЕРСАЛЬНИЙ МОЗОК БОТА
# ==========================================================

def parse_message_data(text):
    """
    1. Знаходить посилання.
    2. Перевіряє наявність мінуса/знаку оклику (Clean Mode).
    Повертає: (url, clean_mode)
    """
    if not text: return None, False
    
    url_match = re.search(r'(https?://[^\s]+)', text)
    if not url_match: return None, False
    
    found_url = url_match.group(1)
    text_without_url = text.replace(found_url, "")
    
    clean_mode = False
    if '-' in text_without_url or '!' in text_without_url:
        clean_mode = True
        
    return found_url, clean_mode

async def generate_smart_caption(title, author_name, author_id, profile_url, original_url, clean_mode):
    """
    Єдина функція для всіх сервісів.
    Якщо clean_mode=True -> повертає None (без підпису).
    Якщо clean_mode=False -> перекладає текст і формує красивий підпис.
    """
    # 🔥 ГОЛОВНА ПЕРЕВІРКА: Якщо чистий режим - нічого не робимо
    if clean_mode:
        return None

    # 1. Переклад тексту
    final_title = ""
    if title and title.strip():
        try:
            lang = detect(title)
            if lang != 'en': # Англійську не чіпаємо
                final_title = await asyncio.to_thread(translator.translate, title)
            else:
                final_title = title
        except:
            final_title = title
    
    # 2. Формування підпису
    caption = f"👤 <b>{author_name}</b> (<a href='{profile_url}'>@{author_id}</a>)\n\n"
    if final_title:
        caption += f"📝 {final_title}\n\n"
    caption += f"🔗 <a href='{original_url}'>Оригінал</a>"
    
    # 3. Ліміт Телеграм (1024 символи)
    if len(caption) > 1024:
        caption = caption[:1000] + "..."
        
    return caption

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

# ==========================================================
# 🎮 ОБРОБНИКИ (HANDLERS)
# ==========================================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привіт! Я універсальний завантажувач (TikTok, Twitter/X).\n\n"
        "✨ <b>Чистий режим:</b>\n"
        "Додай знак <b>-</b> (мінус) будь-де в повідомленні, щоб отримати чисте відео без тексту.",
        parse_mode="HTML"
    )

# --- TIKTOK HANDLER ---
@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok(message: types.Message):
    # 1. Парсинг (працює для всіх)
    user_url, clean_mode = parse_message_data(message.text)
    if not user_url: return

    status_msg = await message.reply("🎵 TikTok: Обробляю...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TIKTOK_API_URL, data={'url': user_url, 'hd': 1}) as response:
                result = await response.json()

        if result.get('code') != 0:
            await status_msg.edit_text("❌ Не знайдено.")
            return

        data = result['data']
        author = data.get('author', {})
        unique_id = author.get('unique_id', '')

        # 2. Генеруємо підпис через "Універсальний мозок"
        caption_text = await generate_smart_caption(
            title=data.get('title', ''),
            author_name=author.get('nickname', 'User'),
            author_id=unique_id,
            profile_url=f"https://www.tiktok.com/@{unique_id}",
            original_url=user_url,
            clean_mode=clean_mode  # <-- Передаємо прапорець сюди
        )

        music_url = data.get('music')
        music_bytes = await download_content(music_url)
        music_file = BufferedInputFile(music_bytes, filename="audio.mp3") if music_bytes else None

        if 'images' in data and data['images']:
            await status_msg.edit_text("📸 TikTok: Фото...")
            images = data['images']
            first = True
            for i in range(0, len(images), 10):
                chunk = images[i:i + 10]
                media_group = MediaGroupBuilder()
                for idx, img_url in enumerate(chunk):
                    if first and idx == 0 and caption_text:
                        media_group.add_photo(media=img_url, caption=caption_text, parse_mode="HTML")
                    else:
                        media_group.add_photo(media=img_url)
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

                await message.answer_video(
                    vfile, caption=caption_text, parse_mode="HTML",
                    thumbnail=tfile, width=w, height=h, supports_streaming=True
                )
                if music_file: await message.answer_audio(music_file, caption="🎵 Звук" if not clean_mode else None)
                await status_msg.delete()

    except Exception as e:
        logging.error(f"TikTok Error: {e}")
        await status_msg.edit_text("❌ Помилка.")


# --- TWITTER / X HANDLER ---
@dp.message(F.text.contains("twitter.com") | F.text.contains("x.com"))
async def handle_twitter(message: types.Message):
    # 1. Парсинг
    user_url, clean_mode = parse_message_data(message.text)
    if not user_url: return

    status_msg = await message.reply("🐦 Twitter: Аналізую...")

    match = re.search(r"/status/(\d+)", user_url)
    if not match:
        await status_msg.edit_text("❌ Не знайдено ID.")
        return
    tweet_id = match.group(1)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.fxtwitter.com/status/{tweet_id}") as response:
                if response.status != 200:
                    await status_msg.edit_text("❌ Твіт не знайдено.")
                    return
                json_data = await response.json()

        tweet = json_data.get('tweet', {})
        if not tweet:
            await status_msg.edit_text("❌ Помилка API.")
            return

        author = tweet.get('author', {})
        screen_name = author.get('screen_name', 'twitter')

        # 2. Генеруємо підпис через "Універсальний мозок"
        caption_text = await generate_smart_caption(
            title=tweet.get('text', ''),
            author_name=author.get('name', 'User'),
            author_id=screen_name,
            profile_url=f"https://twitter.com/{screen_name}",
            original_url=user_url,
            clean_mode=clean_mode  # <-- Передаємо прапорець сюди
        )

        media_list = tweet.get('media', {}).get('all', [])
        if not media_list:
            if caption_text: await message.answer(caption_text, parse_mode="HTML", disable_web_page_preview=True)
            else: await message.answer("❌ Без медіа.")
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
                    afile = BufferedInputFile(vbytes, filename="audio.mp3")
                    w = vdata.get('width')
                    h = vdata.get('height')
                    
                    await message.answer_video(
                        vfile, caption=caption_text, parse_mode="HTML",
                        width=w, height=h, supports_streaming=True
                    )
                    await message.answer_audio(afile, caption="🎵 Звук" if not clean_mode else None)
                    await status_msg.delete()
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

# --- INSTAGRAM / YOUTUBE (Місце для майбутніх сервісів) ---
# Коли будемо додавати, просто викличемо:
# caption = await generate_smart_caption(..., clean_mode=clean_mode)
# І все запрацює автоматично!

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
