import os
import json
import hashlib
import asyncio
import logging
import feedparser
import httpx
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import io
import textwrap

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "900"))
SEEN_FILE          = "seen_tweets.json"

ACCOUNTS = [
    "BBCSport",
    "SkySportsNews",
    "FabrizioRomano",
    "OptaJoe",
    "beINSPORTS_EN",
]

NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
    "https://nitter.net",
]

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-500:], f)

async def fetch_tweets_for_account(client, account):
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{account}/rss"
            r = await client.get(url, timeout=10)
            feed = feedparser.parse(r.text)
            tweets = []
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                if title and not title.startswith("RT @"):
                    tweets.append({
                        "id":      hashlib.md5(entry.get("link","").encode()).hexdigest(),
                        "account": account,
                        "text":    title,
                        "link":    entry.get("link", ""),
                    })
            log.info(f"OK {account}: {len(tweets)} tweets from {instance}")
            return tweets
        except Exception as e:
            log.warning(f"FAIL {instance}/{account}: {e}")
    return []

async def fetch_all_tweets():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [fetch_tweets_for_account(client, acc) for acc in ACCOUNTS]
        results = await asyncio.gather(*tasks)
    return [t for sublist in results for t in sublist]

async def process_batch(new_tweets):
    if not new_tweets:
        return []

    tweets_text = "\n".join(
        f"{i+1}. [@{t['account']}]: {t['text']}"
        for i, t in enumerate(new_tweets)
    )

    prompt = (
        "You are an Arabic sports news editor for Snapchat.\n\n"
        "These are new tweets from sports accounts:\n"
        + tweets_text
        + "\n\nYour job:\n"
        "1. Remove duplicates - same news from different accounts, keep one only\n"
        "2. Remove non-news - opinions, ads, stats without events\n"
        "3. For important news: translate to Arabic and write as an attractive story\n\n"
        "Reply with JSON only, no text outside it:\n"
        '{"stories": [{"source_index": 1, "emoji": "soccer ball emoji", '
        '"headline": "Arabic headline 20-30 words", '
        '"details": "Arabic details 2-3 sentences", '
        '"category": "football"}]}\n\n'
        'If no worthy news: {"stories": []}'
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent?key=" + GEMINI_API_KEY
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    parsed = json.loads(text)
    results = []
    for story in parsed.get("stories", []):
        idx = story.get("source_index", 1) - 1
        if 0 <= idx < len(new_tweets):
            story["link"] = new_tweets[idx]["link"]
            story["source_account"] = new_tweets[idx]["account"]
        results.append(story)
    return results

def draw_story(story):
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    for y in range(H):
        t = y / H
        r = int(10 + 16 * t)
        g = int(10 + 16 * t)
        b = int(15 + 31 * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    accent = (245, 197, 24)
    white  = (255, 255, 255)
    muted  = (170, 170, 170)

    draw.rectangle([0, 0, W, 14], fill=accent)

    def get_font(size):
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except:
                pass
        return ImageFont.load_default()

    draw.text((W - 80, 80), "SnapSport", font=get_font(55), fill=accent, anchor="ra")
    draw.text((W - 80, 148), datetime.now().strftime("%d/%m/%Y"), font=get_font(38), fill=muted, anchor="ra")

    emoji = story.get("emoji", "")
    draw.text((W // 2, int(H * 0.35)), emoji, font=get_font(160), fill=white, anchor="mm")

    cat = story.get("category", "")
    draw.text((W // 2, int(H * 0.48)), cat, font=get_font(40), fill=accent, anchor="mm")

    draw.line([(100, int(H * 0.54)), (W - 100, int(H * 0.54))], fill=accent, width=3)

    headline = story.get("headline", "")
    wrapped_h = textwrap.fill(headline, width=20)
    draw.multiline_text(
        (W // 2, int(H * 0.62)),
        wrapped_h,
        font=get_font(62),
        fill=white,
        anchor="ma",
        align="center",
        spacing=14,
    )

    details = story.get("details", "")
    wrapped_d = textwrap.fill(details, width=28)
    draw.multiline_text(
        (W // 2, int(H * 0.79)),
        wrapped_d,
        font=get_font(44),
        fill=muted,
        anchor="ma",
        align="center",
        spacing=10,
    )

    draw.rectangle([0, H - 14, W, H], fill=accent)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

async def send_to_telegram(story, image_bytes, link):
    headline = story.get("headline", "")
    details  = story.get("details", "")
    emoji    = story.get("emoji", "")
    caption  = f"{emoji} {headline}\n\n{details}\n\nSource: {link}"

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={
                "chat_id":    TELEGRAM_CHAT_ID,
                "caption":    caption,
                "parse_mode": "Markdown",
            },
            files={"photo": ("story.png", image_bytes, "image/png")},
            timeout=30,
        )
    log.info(f"Sent: {headline[:50]}")

async def main():
    log.info("Bot started - Gemini version")
    seen = load_seen()

    while True:
        try:
            log.info(f"Checking {len(ACCOUNTS)} accounts...")
            tweets     = await fetch_all_tweets()
            new_tweets = [t for t in tweets if t["id"] not in seen]
            log.info(f"New tweets: {len(new_tweets)}")

            for t in new_tweets:
                seen.add(t["id"])

            if new_tweets:
                try:
                    log.info("Sending batch to Gemini...")
                    stories = await process_batch(new_tweets)
                    log.info(f"Stories after dedup: {len(stories)}")
                    for story in stories:
                        img = draw_story(story)
                        await send_to_telegram(story, img, story.get("link", ""))
                        await asyncio.sleep(2)
                except Exception as e:
                    log.error(f"Batch error: {e}")

            save_seen(seen)

        except Exception as e:
            log.error(f"Loop error: {e}")

        log.info(f"Sleeping {CHECK_INTERVAL // 60} minutes...")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
