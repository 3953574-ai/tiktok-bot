import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
from aiogram.utils.media_group import MediaGroupBuilder
import aiohttp
from aiohttp import web
from googletrans import Translator  # Підключаємо перекладач

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
API_URL = "https://www.tikwm.com/api/"

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise ValueError("Не знайдено BOT_TOKEN у змінних оточення!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
translator = Translator() # Створюємо об'єкт перекладача

# --- ФУНКЦІЇ БОТА ---
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

async def create_caption(data, original_url):
    """Створює гарний підпис з перекладом"""
    author = data.get('author', {})
    nickname = author.get('nickname', 'Unknown')
    unique_id = author.get('unique_id', '') 
    
    # Отримуємо оригінальний текст
    original_title = data.get('title', '')
    translated_title = original_title

    # --- ЛОГІКА ПЕРЕКЛАДУ ---
    if original_title:
        try:
            # Перекладаємо на українську (src='auto' означає автовизначення мови)
            # Виконуємо це в окремому потоці, щоб не гальмувати бота
            result = await asyncio.to_thread(translator.translate, original_title, dest='uk')
            translated_title = result.text
        except Exception as e:
            logging.error(f"Translation error: {e}")
            # Якщо переклад не вдався - залишаємо оригінал
            translated_title = original_title

    caption = (
        f"👤 <b>{nickname}</b> (@{unique_id})\n\n"
        f"📝 {translated_title}\n\n"
        f"🔗 <a href='{original_url}'>Оригінал в TikTok</a>"
    )
    
    if len(caption) > 1024:
        caption = caption[:1000] + "..."
    return caption

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Надішли мені посилання на TikTok, і я перекладу опис українською.")

@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok_link(message: types.Message):
    user_url = message.text.strip()
    status_msg = await message.reply("⏳ Обробляю та перекладаю...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, data={'url': user_url, 'hd': 1}) as response:
                result = await response.json()

        if result.get('code') != 0:
            await status_msg.edit_text("❌ Не вдалося завантажити. Перевірте посилання.")
            return

        data = result['data']
        
        # Генеруємо підпис (тепер це асинхронна функція через переклад)
        caption_text = await create_caption(data, user_url)
        
        # Музика
        music_url = data.get('music')
        music_bytes = await download_content(music_url)
        music_info = data.get('music_info', {})
        music_title = music_info.get('title', 'original sound')
        music_author = music_info.get('author', 'TikTok')
        music_filename = f"{music_author} - {music_title}.mp3"
        music_file = BufferedInputFile(music_bytes, filename=music_filename) if music_bytes else None

        # --- ФОТО ---
        if 'images' in data and data['images']:
            await status_msg.edit_text("📸 Завантажую фото...")
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
                await message.answer_audio(music_file, caption=f"🎵 {music_title}")
            await status_msg.delete()

        # --- ВІДЕО ---
        else:
            await status_msg.edit_text("🎥 Завантажую відео...")
            
            video_url = data.get('hdplay') or data.get('play')
            cover_url = data.get('origin_cover') or data.get('cover')

            video_bytes, cover_bytes = await asyncio.gather(
                download_content(video_url),
                download_content(cover_url)
            )

            if video_bytes:
                video_file = BufferedInputFile(video_bytes, filename=f"video_{data['id']}.mp4")
                
                thumbnail_file = None
                if cover_bytes:
                    thumbnail_file = BufferedInputFile(cover_bytes, filename="cover.jpg")
                
                await message.answer_video(
                    video_file,
                    caption=caption_text,
                    parse_mode="HTML",
                    thumbnail=thumbnail_file,
                    supports_streaming=True
                )
                
                if music_file:
                    await message.answer_audio(music_file, caption=f"🎵 {music_title}")
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ Помилка: файл відео не знайдено.")

    except Exception as e:
        logging.error(f"Main Loop Error: {e}")
        await status_msg.edit_text("❌ Сталася помилка.")

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
