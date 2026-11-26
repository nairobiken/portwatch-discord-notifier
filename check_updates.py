import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

TRACKER_URL = "https://portwatch-vi.xyz/data/tracker.json"
PINNED_URL = "https://portwatch-vi.xyz/data/pinned.json"
NEWS_RSS_URL = "https://portwatch-vi.xyz/news/feed.xml"
SITE_URL = "https://portwatch-vi.xyz/"
STATE_FILE = "state.json"

USER_AGENT = "PortWatchDiscordNotifier/1.0 (+https://portwatch-vi.xyz/)"

# Keys that typically change every 3 hours when the scout runs even if no real progress happened
IGNORE_KEYS = {
    "updated", "updated_at", "updatedat", "last_updated", "lastupdated",
    "checked_at", "checkedat", "last_checked", "lastchecked",
    "generated_at", "generatedat", "fetched_at", "fetchedat",
    "last_run", "lastrun", "scout_status", "timestamp", "etag",
}

# Common identifier keys used to match items inside JSON lists (e.g., leaderboard projects)
ID_KEYS = ("id", "slug", "repo", "name", "title", "url")


def fetch_url(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"[WARN] HTTP {e.code} fetching {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[WARN] Failed fetching {url}: {e}", file=sys.stderr)
        return None


def fetch_json(url: str):
    raw = fetch_url(url)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"[WARN] Invalid JSON from {url}: {e}", file=sys.stderr)
        return None


def strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_rss(xml_bytes: bytes | None) -> list[dict]:
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"[WARN] Invalid RSS XML: {e}", file=sys.stderr)
        return []

    items = []
    # Support RSS 2.0 (<item>) and Atom (<entry>)
    for elem in root.findall(".//item"):
        title = (elem.findtext("title") or "Untitled").strip()
        link = (elem.findtext("link") or SITE_URL).strip()
        guid = (elem.findtext("guid") or link or title).strip()
        desc = strip_html(elem.findtext("description") or "")
        pub_date = (elem.findtext("pubDate") or "").strip()
        items.append({
            "id": guid,
            "title": title,
            "link": link,
            "description": desc,
            "date": pub_date,
        })

    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for elem in root.findall(".//atom:entry", ns):
            title = (elem.findtext("atom:title", "", ns) or "Untitled").strip()
            link_el = elem.find("atom:link", ns)
            link = link_el.attrib.get("href", SITE_URL) if link_el is not None else SITE_URL
            guid = (elem.findtext("atom:id", "", ns) or link or title).strip()
            desc = strip_html(
                elem.findtext("atom:summary", "", ns)
                or elem.findtext("atom:content", "", ns)
                or ""
            )
            pub_date = (elem.findtext("atom:updated", "", ns) or "").strip()
            items.append({
                "id": guid,
                "title": title,
                "link": link,
                "description": desc,
                "date": pub_date,
            })

    return items


def find_id_key(lst: list) -> str | None:
    if not lst or not all(isinstance(x, dict) for x in lst):
        return None
    for key in ID_KEYS:
        vals = [x.get(key) for x in lst]
        if all(isinstance(v, (str, int)) and v != "" for v in vals) and len(set(vals)) == len(vals):
            return key
    return None


def summarize_item(item) -> str:
    if isinstance(item, dict):
        label = next((str(item[k]) for k in ID_KEYS if item.get(k)), None)
        extras = []
        for k in ("score", "progress", "stage", "status", "summary", "text", "message"):
            if k in item and item[k] not in (None, ""):
                extras.append(f"{k}: {item[k]}")
        if label and extras:
            return truncate(f"{label} ({', '.join(extras)})", 160)
        if label:
            return truncate(label, 160)
        if extras:
            return truncate(", ".join(extras), 160)
        return truncate(json.dumps(item, ensure_ascii=False), 160)
    return truncate(str(item), 160)


def diff_json(old, new, path: str = "") -> list[str]:
    changes = []

    if type(old) is not type(new):
        changes.append(f"**{path or 'root'}** changed: `{truncate(old, 60)}` → `{truncate(new, 60)}`")
        return changes

    if isinstance(new, dict):
        all_keys = sorted(set(old.keys()) | set(new.keys()))
        for k in all_keys:
            if k.lower() in IGNORE_KEYS:
                continue
            sub_path = f"{path}.{k}" if path else k
            if k not in old:
                changes.append(f"➕ Added **{sub_path}**: {summarize_item(new[k])}")
            elif k not in new:
                changes.append(f"➖ Removed **{sub_path}**")
            else:
                changes.extend(diff_json(old[k], new[k], sub_path))
        return changes

    if isinstance(new, list):
        id_key = find_id_key(old) or find_id_key(new)
        if id_key and all(isinstance(x, dict) and id_key in x for x in old + new):
            old_map = {str(x[id_key]): x for x in old}
            new_map = {str(x[id_key]): x for x in new}
            for item_id, item in new_map.items():
                label = f"{path} › {item_id}" if path else item_id
                if item_id not in old_map:
                    changes.append(f"🆕 **{label}**: {summarize_item(item)}")
                else:
                    changes.extend(diff_json(old_map[item_id], item, label))
            for item_id in old_map:
                if item_id not in new_map:
                    label = f"{path} › {item_id}" if path else item_id
                    changes.append(f"🗑️ Removed **{label}**")
        else:
            # Match unkeyed lists (e.g., changelog entries) by content hash
            old_hashes = {hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest() for x in old}
            for item in new:
                h = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                if h not in old_hashes:
                    prefix = f"**{path}**: " if path else ""
                    changes.append(f"🆕 {prefix}{summarize_item(item)}")
        return changes

    if old != new:
        changes.append(f"🔄 **{path}**: `{truncate(old, 70)}` → `{truncate(new, 70)}`")

    return changes


def extract_pinned_text(pinned) -> str:
    if not pinned:
        return ""
    if isinstance(pinned, str):
        return pinned.strip()
    if isinstance(pinned, dict):
        for key in ("text", "message", "content", "body"):
            val = pinned.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def send_discord(embeds: list[dict]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()

    if not webhook_url and not (bot_token and channel_id):
        print(
            "[ERROR] Set DISCORD_WEBHOOK_URL or both DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Discord allows up to 10 embeds per message
    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        if webhook_url:
            url = webhook_url
            payload = {"username": "PortWatch VI", "embeds": batch}
            headers = {
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
        else:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            payload = {"embeds": batch}
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bot {bot_token}",
                "User-Agent": USER_AGENT,
            }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                print(f"[OK] Sent {len(batch)} embed(s) to Discord (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[ERROR] Discord API HTTP {e.code}: {body}", file=sys.stderr)
            sys.exit(1)

        if i + 10 < len(embeds):
            time.sleep(1.5)


def load_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def main() -> None:
    old_state = load_state()

    tracker = fetch_json(TRACKER_URL)
    pinned = fetch_json(PINNED_URL)
    news_items = parse_rss(fetch_url(NEWS_RSS_URL))

    if tracker is None and pinned is None and not news_items:
        print("[WARN] Could not fetch any endpoints; keeping previous state.", file=sys.stderr)
        sys.exit(0)

    now_iso = datetime.now(timezone.utc).isoformat()
    new_state = {
        "tracker": tracker if tracker is not None else (old_state or {}).get("tracker"),
        "pinned": pinned if pinned is not None else (old_state or {}).get("pinned"),
        "news_ids": [item["id"] for item in news_items] if news_items else (old_state or {}).get("news_ids", []),
    }

    # First run: save baseline snapshot and send a single confirmation embed
    if old_state is None:
        save_state(new_state)
        send_discord([{
            "title": "✅ PortWatch VI Tracker Connected",
            "url": SITE_URL,
            "description": (
                "Now watching **PortWatch VI** for:\n"
                "• Leaderboard & progress changes (`tracker.json`)\n"
                "• News posts (`news/feed.xml`)\n"
                "• Pinned announcements (`pinned.json`)"
            ),
            "color": 0x7A5CFF,
            "timestamp": now_iso,
        }])
        return

    embeds = []

    # 1. Check news RSS feed
    seen_news = set(old_state.get("news_ids", []))
    fresh_news = [item for item in news_items if item["id"] not in seen_news]
    for item in reversed(fresh_news[:5]):
        embeds.append({
            "title": f"📰 {truncate(item['title'], 240)}",
            "url": item["link"] or SITE_URL,
            "description": truncate(item["description"] or "New article posted on PortWatch VI.", 500),
            "color": 0xFF2E7E,
            "footer": {"text": f"PortWatch VI News {('• ' + item['date']) if item['date'] else ''}"},
            "timestamp": now_iso,
        })

    # 2. Check creator's pinned message
    old_pin = extract_pinned_text(old_state.get("pinned"))
    new_pin = extract_pinned_text(new_state.get("pinned"))
    if new_pin and new_pin != old_pin:
        embeds.append({
            "title": "📌 New Pinned Message on PortWatch VI",
            "url": SITE_URL,
            "description": truncate(new_pin, 1500),
            "color": 0xFF8A3D,
            "timestamp": now_iso,
        })

    # 3. Check tracker.json for real progress changes
    if old_state.get("tracker") is not None and tracker is not None:
        diffs = diff_json(old_state["tracker"], tracker)
        if diffs:
            shown = diffs[:20]
            extra = f"\n…and {len(diffs) - 20} more changes." if len(diffs) > 20 else ""
            description = truncate("\n".join(shown) + extra, 3900)
            embeds.append({
                "title": "🚀 GTA VI PC Port Progress Updated!",
                "url": SITE_URL,
                "description": description,
                "color": 0x7A5CFF,
                "footer": {"text": "Source: portwatch-vi.xyz/data/tracker.json"},
                "timestamp": now_iso,
            })

    if embeds:
        send_discord(embeds)
        save_state(new_state)
    else:
        print("[OK] No updates detected.")


if __name__ == "__main__":
    main()
