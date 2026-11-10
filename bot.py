"""
Wikipedia Telegram Bot (English only)
-------------------------------------
Send an article name -> get the full article text + all images on that page.

Setup:
    pip install "python-telegram-bot>=21" httpx
    export BOT_TOKEN="123456:ABC..."      # from @BotFather
    python wikipedia_bot.py
"""

import asyncio
import logging
import os
import re

import httpx
from telegram import InputMediaPhoto, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- Settings ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
LANG = "en"                  # English Wikipedia only
MAX_IMAGES = 50              # None = no limit, send every image
MIN_IMAGE_SIDE = 150         # skip tiny icons/logos
TEXT_CHUNK = 4000            # Telegram message limit is 4096
FILE_IF_LONGER = 30000       # longer article: first part in chat + full .txt file
USER_AGENT = "WikiTelegramBot/1.0 (https://t.me/your_bot; you@example.com)"  # put your contact here
ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
}
# ------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("wikibot")


# ---------- Wikipedia API ----------
async def api(client: httpx.AsyncClient, params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    r = await client.get(f"https://{LANG}.wikipedia.org/w/api.php", params=params)
    r.raise_for_status()
    return r.json()


async def get_page(client, title: str):
    """Full plain-text article + URL. Redirects are followed."""
    data = await api(client, {
        "action": "query",
        "titles": title,
        "redirects": 1,
        "prop": "extracts|info",
        "explaintext": 1,
        "exsectionformat": "wiki",
        "inprop": "url",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing") or pages[0].get("invalid"):
        return None
    return pages[0]


async def find_title(client, query: str):
    """If the exact title is not found, use the top search result."""
    data = await api(client, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": 1,
    })
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


async def get_images(client, title: str) -> list[dict]:
    """Every image on the page: URL (up to 1280px wide) + caption."""
    params = {
        "action": "query",
        "generator": "images",
        "titles": title,
        "gimlimit": "max",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": 1280,
    }
    found: dict[str, dict] = {}
    while True:
        data = await api(client, params)
        for p in data.get("query", {}).get("pages", []):
            info = (p.get("imageinfo") or [None])[0]
            if not info or info.get("mime") not in ALLOWED_MIME:
                continue
            if info.get("width", 0) < MIN_IMAGE_SIDE or info.get("height", 0) < MIN_IMAGE_SIDE:
                continue
            caption = re.sub(r"^[^:]+:|\.\w+$", "", p["title"]).replace("_", " ")
            found[p["title"]] = {
                "index": p.get("index", 0),
                "url": info.get("thumburl") or info["url"],
                "caption": caption,
            }
        if "continue" in data:
            params.update(data["continue"])
        else:
            break
    images = sorted(found.values(), key=lambda x: x["index"])
    return images[:MAX_IMAGES] if MAX_IMAGES else images


# ---------- Telegram helpers ----------
async def safe(fn, *args, **kwargs):
    """Retry after waiting if Telegram flood limit (RetryAfter) is hit."""
    for _ in range(5):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            await asyncio.sleep(wait + 1)
    return None


def split_text(text: str, limit: int = TEXT_CHUNK) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


async def download(client, url: str, sem: asyncio.Semaphore):
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url)
                if r.status_code == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                if len(r.content) > 10 * 1024 * 1024:  # Telegram photo limit
                    return None
                return r.content
            except httpx.HTTPError:
                await asyncio.sleep(1)
    return None


async def send_album(bot, chat_id: int, items: list[tuple[bytes, str]]):
    if len(items) >= 2:
        try:
            await safe(
                bot.send_media_group,
                chat_id,
                [InputMediaPhoto(b, caption=c[:200]) for b, c in items],
            )
            return
        except TelegramError as e:
            log.warning("album failed, sending one by one: %s", e)
    for b, c in items:
        try:
            await safe(bot.send_photo, chat_id, b, caption=c[:200])
        except TelegramError as e:
            log.warning("skipping photo (%s): %s", c, e)


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Send me the name of any Wikipedia article.\n"
        "I'll send the full article and all the images on that page.\n\n"
        "Example: Taj Mahal"
    )


async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    chat_id = update.effective_chat.id
    status = await update.message.reply_text(f"🔎 Searching for '{query}'...")

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
        ) as client:
            page = await get_page(client, query)
            if not page:
                title = await find_title(client, query)
                page = await get_page(client, title) if title else None
            if not page:
                await status.edit_text("❌ No article found with that name. Check the spelling and try again.")
                return

            title = page["title"]
            text = (page.get("extract") or "").strip()
            text = re.sub(r"^=+\s*(.*?)\s*=+$", r"\n📌 \1", text, flags=re.M)

            await status.edit_text(f"📖 {title}\n🔗 {page.get('fullurl', '')}")

            # --- Article text ---
            chunks = split_text(text)
            if len(text) > FILE_IF_LONGER:
                await safe(context.bot.send_message, chat_id, chunks[0])
                fname = re.sub(r'[\\/:*?"<>|]', "_", title) + ".txt"
                await safe(
                    context.bot.send_document, chat_id,
                    document=text.encode("utf-8"), filename=fname,
                    caption="📄 The full article is in this file",
                )
            else:
                for c in chunks:
                    await safe(context.bot.send_message, chat_id, c)
                    await asyncio.sleep(0.4)

            # --- Images ---
            images = await get_images(client, title)
            if not images:
                await context.bot.send_message(chat_id, "🖼 No images found on this page.")
                return
            await context.bot.send_message(chat_id, f"🖼 Sending {len(images)} images...")

            sem = asyncio.Semaphore(4)
            for i in range(0, len(images), 10):
                batch = images[i:i + 10]
                blobs = await asyncio.gather(*(download(client, im["url"], sem) for im in batch))
                items = [(b, im["caption"]) for b, im in zip(blobs, batch) if b]
                if items:
                    await send_album(context.bot, chat_id, items)
                await asyncio.sleep(1)

            await context.bot.send_message(chat_id, "✅ Done! Send another article name.")

    except httpx.HTTPError as e:
        log.exception("Wikipedia error")
        await context.bot.send_message(chat_id, f"⚠️ Couldn't reach Wikipedia: {e}")
    except Exception:
        log.exception("Unexpected error")
        await context.bot.send_message(chat_id, "⚠️ Something went wrong. Please try again in a bit.")


def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))
    port = os.environ.get("PORT")
    base_url = os.environ.get("RENDER_EXTERNAL_URL")  # set by Render on web services only
    if port and base_url:
        log.info("Webhook mode (Render web service)")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=BOT_TOKEN,
            webhook_url=f"{base_url}/{BOT_TOKEN}",
        )
    else:
        log.info("Polling mode (Render background worker / local)")
        app.run_polling()


if __name__ == "__main__":
    main()
