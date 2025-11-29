import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
from aiogram.utils.media_group import MediaGroupBuilder
import aiohttp
from aiohttp import web

# --- КОНФІГУРАЦІЯ ---
# Токен беремо зі змінних оточення сервера
BOT_TOKEN = os.getenv('BOT_TOKEN') 
API_URL = "https://www.tikwm.com/api/"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ФУНКЦІЇ БОТА ---
async def download_content(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.read()
    return None

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Надішли мені посилання на TikTok.")

@dp.message(F.text.contains("tiktok.com"))
async def handle_tiktok_link(message: types.Message):
    user_url = message.text.strip()
    status_msg = await message.reply("⏳ Обробляю...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, data={'url': user_url, 'hd': 1}) as response:
                result = await response.json()

        if result.get('code') != 0:
            await status_msg.edit_text("❌ Не вдалося завантажити. Перевірте посилання.")
            return

        data = result['data']
        music_url = data.get('music')
        music_bytes = await download_content(music_url)
        music_file = BufferedInputFile(music_bytes, filename=f"music_{data['id']}.mp3")

        # Слайд-шоу
        if 'images' in data and data['images']:
            await status_msg.edit_text("📸 Завантажую фото...")
            images = data['images']
            chunk_size = 10
            for i in range(0, len(images), chunk_size):
                chunk = images[i:i + chunk_size]
                media_group = MediaGroupBuilder()
                for img_url in chunk:
                    media_group.add_photo(media=img_url)
                await message.answer_media_group(media_group.build())
            await message.answer_audio(music_file, caption="🎵 Звук")
            await status_msg.delete()

        # Відео
        else:
            await status_msg.edit_text("🎥 Завантажую відео...")
            video_url = data.get('play')
            video_bytes = await download_content(video_url)
            if video_bytes:
                video_file = BufferedInputFile(video_bytes, filename=f"video_{data['id']}.mp4")
                await message.answer_video(video_file, caption="🎬 Відео без знаку")
                await message.answer_audio(music_file, caption="🎵 Звук")
                await status_msg.delete()

    except Exception as e:
        logging.error(e)
        await status_msg.edit_text("❌ Помилка.")

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def health_check(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render дає порт через змінну оточення PORT, або використовуємо 8080
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def main():
    # Запускаємо і веб-сервер (щоб Render не сварився), і бота
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())