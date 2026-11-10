"""
Wikipedia Telegram Bot - advanced (English only)
------------------------------------------------
Send an article name and the bot replies with:

  1. <Article>.txt          full article text + REFERENCES + other external
                            links + IMAGE CREDITS (author / license / source)
  2. <Article>_images_partN.zip
                            every image on the page, plus CREDITS.txt.
                            Split into several zips if the images are bigger
                            than Telegram's 50 MB bot upload limit.

If the name is ambiguous (e.g. "Mercury") or not an exact title, the bot shows
tappable suggestions instead of guessing.

Setup:
    pip install "python-telegram-bot[webhooks]>=21" httpx beautifulsoup4
    export BOT_TOKEN="123456:ABC..."      # from @BotFather
    python bot.py
"""

import asyncio
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- Settings ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
LANG = "en"                       # English Wikipedia only
IMAGE_WIDTH = 1920                # max image width in the zip (standard Wikimedia size)
MAX_IMAGES = None                 # None = every image; or e.g. 100 to cap it
MIN_IMAGE_SIDE = 100              # skip tiny icons/flags smaller than this (pixels)
ZIP_PART_LIMIT = 45 * 1024 * 1024 # Telegram bots can upload up to 50 MB per file
MAX_IMAGE_BYTES = 40 * 1024 * 1024
USER_AGENT = "WikiTelegramBot/2.0 (https://t.me/your_bot; you@example.com)"  # put your contact here
ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
}
SKIP_NAME = re.compile(
    r"(Commons-logo|Wiktionary-logo|Wikiquote-logo|Wikisource-logo|Wikibooks-logo|"
    r"Wikinews-logo|Wikiversity-logo|Wikivoyage-logo|Question_book|Ambox|Padlock|"
    r"Edit-clear|Increase2|Decrease2|Portal-puzzle|Folder_Hexagonal|OOjs_UI_icon)",
    re.I,
)
WIKI_DOMAINS = re.compile(
    r"^https?://([\w-]+\.)*(wikipedia|wikimedia|wikidata|mediawiki|wikisource|wiktionary|"
    r"wikibooks|wikiquote|wikinews|wikiversity|wikivoyage)\.org",
    re.I,
)
# ------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("wikibot")


# ---------- Small helpers ----------
def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_") or "article"


def strip_html(s: str) -> str:
    return " ".join(BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True).split())


def retry_wait(resp: httpx.Response, attempt: int) -> int:
    v = resp.headers.get("Retry-After", "")
    return min(int(v), 30) if v.isdigit() else 2 * (attempt + 1)


async def edit(msg, text: str):
    try:
        await msg.edit_text(text)
    except TelegramError:
        pass


async def safe(fn, *args, **kwargs):
    """Retry after waiting if Telegram's flood limit (RetryAfter) is hit."""
    for _ in range(5):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            await asyncio.sleep(wait + 1)
    return None


# ---------- Wikipedia API ----------
async def api(client: httpx.AsyncClient, params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    r = await client.get(f"https://{LANG}.wikipedia.org/w/api.php", params=params)
    r.raise_for_status()
    return r.json()


async def get_page(client, *, title: str = None, pageid: int = None):
    """Full plain-text article + metadata. Redirects are followed."""
    params = {
        "action": "query",
        "redirects": 1,
        "prop": "extracts|info|pageprops",
        "explaintext": 1,
        "exsectionformat": "wiki",
        "inprop": "url",
        "ppprop": "disambiguation",
    }
    if pageid:
        params["pageids"] = pageid
    else:
        params["titles"] = title
    data = await api(client, params)
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing") or pages[0].get("invalid"):
        return None
    return pages[0]


async def search(client, query: str, limit: int = 5) -> list[dict]:
    data = await api(client, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": limit,
    })
    return data.get("query", {}).get("search", [])


def parse_references(html: str):
    """Return ([(group_name, [reference text, ...]), ...], set_of_reference_urls)."""
    soup = BeautifulSoup(html or "", "html.parser")
    groups, ref_urls = [], set()
    for ol in soup.select("ol.references"):
        heading = ol.find_previous(["h2", "h3"])
        name = heading.get_text(" ", strip=True) if heading else "References"
        items = []
        for li in ol.find_all("li", recursive=False):
            for junk in li.select(".mw-cite-backlink, .cite-accessibility-label"):
                junk.decompose()
            node = li.select_one(".reference-text") or li
            text = " ".join(node.get_text(" ", strip=True).split())
            urls = []
            for a in node.select("a[href]"):
                href = a["href"]
                if href.startswith("//"):
                    href = "https:" + href
                if href.startswith("http") and href not in urls:
                    urls.append(href)
            ref_urls.update(urls)
            extra = [u for u in urls if u not in text]
            if extra:
                text += " | " + " ; ".join(extra)
            if text:
                items.append(text)
        if items:
            groups.append((name, items))
    return groups, ref_urls


async def get_references(client, pageid: int):
    """Citations from the article's reference list + every other external link."""
    data = await api(client, {
        "action": "parse",
        "pageid": pageid,
        "prop": "text|externallinks",
        "disableeditsection": 1,
        "disablelimitreport": 1,
        "redirects": 1,
    })
    parsed = data.get("parse", {})
    groups, ref_urls = parse_references(parsed.get("text", ""))
    other, seen = [], set()
    for u in parsed.get("externallinks", []):
        if u.startswith("//"):
            u = "https:" + u
        if u in seen or u in ref_urls or WIKI_DOMAINS.match(u):
            continue
        seen.add(u)
        other.append(u)
    return groups, other


async def get_images(client, title: str) -> list[dict]:
    """Every image on the page with URL, author, license and source page."""
    params = {
        "action": "query",
        "generator": "images",
        "titles": title,
        "redirects": 1,
        "gimlimit": "max",
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
        "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl",
        "iiurlwidth": IMAGE_WIDTH,
    }
    found: dict[str, dict] = {}
    while True:
        data = await api(client, params)
        for p in data.get("query", {}).get("pages", []):
            info = (p.get("imageinfo") or [None])[0]
            if not info or info.get("mime") not in ALLOWED_MIME:
                continue
            if SKIP_NAME.search(p["title"]):
                continue
            if info.get("width", 0) < MIN_IMAGE_SIDE or info.get("height", 0) < MIN_IMAGE_SIDE:
                continue
            meta = info.get("extmetadata") or {}
            fname = safe_name(p["title"].split(":", 1)[-1])
            if info["mime"] == "image/svg+xml":
                fname += ".png"  # SVGs are delivered as PNG renders
            found[p["title"]] = {
                "index": p.get("index", 0),
                "title": p["title"],
                "file": fname,
                "url": info.get("thumburl") or info["url"],
                "page_url": info.get("descriptionurl", ""),
                "author": strip_html(meta.get("Artist", {}).get("value", "")),
                "license": strip_html(meta.get("LicenseShortName", {}).get("value", "")),
                "license_url": meta.get("LicenseUrl", {}).get("value", ""),
            }
        if "continue" in data:
            params.update(data["continue"])
        else:
            break
    images = sorted(found.values(), key=lambda x: x["index"])
    if MAX_IMAGES:
        images = images[:MAX_IMAGES]
    for n, im in enumerate(images, 1):
        im["num"] = n
        im["zip_name"] = f"{n:03d}_{im['file']}"
    return images


# ---------- Text file + credits ----------
def credit_block(im: dict) -> str:
    lic = im["license"] or "See source page"
    if im["license_url"]:
        lic += f" ({im['license_url']})"
    return (
        f"{im['zip_name']}\n"
        f"  Title:   {im['title']}\n"
        f"  Author:  {im['author'] or 'Unknown (see source page)'}\n"
        f"  License: {lic}\n"
        f"  Source:  {im['page_url']}\n"
    )


def build_text_file(page: dict, groups, other_links, images, now: datetime) -> str:
    title = page["title"]
    slug = quote(title.replace(" ", "_"))
    permalink = f"https://{LANG}.wikipedia.org/w/index.php?title={slug}&oldid={page.get('lastrevid', '')}"
    retrieved = now.strftime("%Y-%m-%d")

    text = (page.get("extract") or "").strip()
    out = [
        title,
        "=" * len(title),
        "",
        f"Source:     {page.get('fullurl', '')}",
        f"Permalink:  {permalink}",
        f"Retrieved:  {now:%Y-%m-%d %H:%M} UTC",
        "License:    Text from Wikipedia, CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/).",
        "            Images have their own licenses - see IMAGE CREDITS below.",
        f'Cite as:    "{title}". Wikipedia, The Free Encyclopedia. Retrieved {retrieved}. {permalink}',
        "",
        "-" * 60,
        "",
        text,
        "",
        "-" * 60,
        "REFERENCES",
        "-" * 60,
        "",
    ]
    if groups:
        n = 0
        for name, items in groups:
            if len(groups) > 1:
                out += [f"[{name}]", ""]
            for item in items:
                n += 1
                out.append(f"[{n}] {item}")
            out.append("")
    else:
        out += ["(No reference list found on this page.)", ""]

    if other_links:
        out += ["-" * 60, "OTHER EXTERNAL LINKS ON THIS PAGE", "-" * 60, ""]
        out += [f"- {u}" for u in other_links] + [""]

    out += ["-" * 60, f"IMAGE CREDITS ({len(images)} images)", "-" * 60, ""]
    if images:
        out += [credit_block(im) for im in images]
    else:
        out += ["(No images on this page.)", ""]
    return "\n".join(out)


# ---------- Images -> zip parts ----------
async def download(client, url: str, sem: asyncio.Semaphore):
    async with sem:
        for attempt in range(4):
            try:
                r = await client.get(url)
                if r.status_code == 429:
                    await asyncio.sleep(retry_wait(r, attempt))
                    continue
                r.raise_for_status()
                if len(r.content) > MAX_IMAGE_BYTES:
                    return None
                return r.content
            except httpx.HTTPError:
                await asyncio.sleep(1 + attempt)
    return None


async def zip_parts(client, images: list[dict], progress=None, failed: list = None):
    """Async generator: yields (zip_bytes, [images_in_zip]) each time a part is full."""
    sem = asyncio.Semaphore(4)

    def new_part():
        buf = io.BytesIO()
        return buf, zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED), [], 0

    def finish(buf, zf, members):
        zf.writestr("CREDITS.txt", "\n".join(credit_block(m) for m in members))
        zf.close()
        return buf.getvalue()

    buf, zf, members, size = new_part()
    done = 0
    for i in range(0, len(images), 6):
        batch = images[i:i + 6]
        blobs = await asyncio.gather(*(download(client, im["url"], sem) for im in batch))
        for im, blob in zip(batch, blobs):
            done += 1
            if not blob:
                if failed is not None:
                    failed.append(im)
                continue
            if members and size + len(blob) > ZIP_PART_LIMIT:
                yield finish(buf, zf, members), members
                buf, zf, members, size = new_part()
            zf.writestr(im["zip_name"], blob)
            members.append(im)
            size += len(blob)
        if progress:
            await progress(done, len(images))
    if members:
        yield finish(buf, zf, members), members


# ---------- Main job ----------
async def deliver(context, chat_id: int, client, page: dict, status):
    title, pageid = page["title"], page["pageid"]
    base = safe_name(title)
    await edit(status, f"📖 {title}\n⏳ Collecting text, references and image list...")

    refs_res, imgs_res = await asyncio.gather(
        get_references(client, pageid), get_images(client, title), return_exceptions=True
    )
    if isinstance(refs_res, Exception):
        log.warning("references failed: %s", refs_res)
        groups, other = [], []
    else:
        groups, other = refs_res
    if isinstance(imgs_res, Exception):
        log.warning("images failed: %s", imgs_res)
        images = []
    else:
        images = imgs_res

    # 1) Text file
    text = build_text_file(page, groups, other, images, datetime.now(timezone.utc))
    n_refs = sum(len(items) for _, items in groups)
    await safe(
        context.bot.send_document, chat_id,
        document=text.encode("utf-8"), filename=f"{base}.txt",
        caption=f"📄 {title}\nFull text + {n_refs} references + image credits\n🔗 {page.get('fullurl', '')}",
    )

    # 2) Image zips
    if not images:
        await edit(status, f"📖 {title}\n🖼 No images found on this page.")
        await context.bot.send_message(chat_id, "✅ Done! Send another article name.")
        return

    async def progress(done, total):
        await edit(status, f"📖 {title}\n⏳ Downloading images {done}/{total}...")

    failed, parts, sent = [], 0, 0
    async for data, members in zip_parts(client, images, progress, failed):
        parts += 1
        sent += len(members)
        await edit(status, f"📖 {title}\n📤 Uploading zip part {parts}...")
        await safe(
            context.bot.send_document, chat_id,
            document=data, filename=f"{base}_images_part{parts}.zip",
            caption=f"🗜 Images part {parts} ({len(members)} images, with CREDITS.txt)",
        )

    summary = f"✅ Done! 1 text file, {parts} zip file(s), {sent} images."
    if failed:
        summary += f"\n⚠️ {len(failed)} image(s) could not be downloaded."
    await edit(status, f"📖 {title}\n{summary}")
    await context.bot.send_message(chat_id, "Send another article name any time.")


async def serve(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None, pageid: int = None):
    chat_id = update.effective_chat.id
    if context.user_data.get("busy"):
        await context.bot.send_message(chat_id, "⏳ Please wait, I'm still working on your previous request.")
        return
    context.user_data["busy"] = True
    status = await context.bot.send_message(chat_id, "🔎 Searching Wikipedia...")
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True
        ) as client:
            if pageid:
                page = await get_page(client, pageid=pageid)
            else:
                page = await get_page(client, title=text)
                if page and "disambiguation" in (page.get("pageprops") or {}):
                    page = None  # let the user pick a specific article
                if not page:
                    hits = await search(client, text, 5)
                    if not hits:
                        await edit(status, "❌ No article found with that name. Check the spelling and try again.")
                        return
                    buttons = [
                        [InlineKeyboardButton(h["title"][:60], callback_data=f"pick:{h['pageid']}")]
                        for h in hits
                    ]
                    await status.edit_text(
                        f"🤔 Which one do you mean by '{text}'?",
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                    return
            if not page:
                await edit(status, "❌ That article could not be loaded.")
                return
            await deliver(context, chat_id, client, page, status)
    except httpx.HTTPError as e:
        log.exception("Wikipedia error")
        await context.bot.send_message(chat_id, f"⚠️ Couldn't reach Wikipedia: {e}")
    except Exception:
        log.exception("Unexpected error")
        await context.bot.send_message(chat_id, "⚠️ Something went wrong. Please try again in a bit.")
    finally:
        context.user_data["busy"] = False


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Send me the name of any Wikipedia article.\n\n"
        "You'll get:\n"
        "📄 a .txt file with the full article, references and image credits\n"
        "🗜 a .zip file with all the images on that page\n\n"
        "Example: Taj Mahal"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > 200:
        await update.message.reply_text("Please send just the article name (under 200 characters).")
        return
    await serve(update, context, text=text)


async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await serve(update, context, pageid=int(query.data.split(":")[1]))


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .media_write_timeout(180)   # big zip uploads
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(handle_pick, pattern=r"^pick:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    port = os.environ.get("PORT")
    base_url = os.environ.get("RENDER_EXTERNAL_URL")  # set by Render on web services only
    if port and base_url:
        log.info("Webhook mode (Render web service)")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=BOT_TOKEN,
            webhook_url=f"{base_url}/{BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        log.info("Polling mode (Render background worker / local)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
