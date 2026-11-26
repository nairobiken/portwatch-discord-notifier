import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TRACKER_URL = "https://portwatch-vi.xyz/data/tracker.json"
PINNED_URL = "https://portwatch-vi.xyz/data/pinned.json"
SITE_URL = "https://portwatch-vi.xyz/"
STATE_FILE = "state.json"

USER_AGENT = "PortWatchDiscordNotifier/2.0 (+https://portwatch-vi.xyz/)"

STAGE_LABELS = [
    ("playable", "Playable"),
    ("world", "In the world"),
    ("menu", "Main menu"),
    ("boots", "First frame"),
    ("loads", "Exe runs"),
]

CATEGORY_COLORS = {
    "Port": 0x7A5CFF,
    "Takedown": 0xE74C3C,
    "Leak": 0xF39C12,
    "Safety": 0xE67E22,
    "Security": 0xE74C3C,
    "Legal": 0x3498DB,
}


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"[WARN] HTTP {e.code} fetching {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[WARN] Failed fetching {url}: {e}", file=sys.stderr)
        return None


def truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compute_score(effort: dict) -> float:
    gta = effort.get("gta") or {}
    ladder = 0.0
    for key in ("loads", "boots", "menu", "world", "playable"):
        val = float(gta.get(key) or 0)
        ladder += 10.0 * val
        if val < 1.0:
            break

    if ladder == 0.0:
        gw = effort.get("groundwork") or {}
        vals = [
            float(gw.get("loader") or 0),
            float(gw.get("shaders") or 0),
            float(gw.get("graphics") or 0),
            float(gw.get("proven") or 0),
        ]
        return round((sum(vals) / 4.0) * 10.0, 1)

    if float(gta.get("playable") or 0) >= 1.0:
        story = float(gta.get("story") or 0)
        render = float(gta.get("render") or 0)
        fps = min(float(gta.get("fps") or 0) / 30.0, 1.0)
        stab_raw = gta.get("stability")
        stability = float(stab_raw) if isinstance(stab_raw, (int, float)) else 0.0
        bonus = 0.5 * (35.0 * story + 25.0 * render + 20.0 * fps + 20.0 * stability)
        return round(50.0 + bonus, 1)

    return round(ladder, 1)


def get_stage_name(effort: dict) -> str:
    gta = effort.get("gta") or {}
    for key, label in STAGE_LABELS:
        val = float(gta.get(key) or 0)
        if val >= 1.0:
            return label
        if val > 0:
            return f"{label} (partial)"
    return "Groundwork"


def make_bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, round((pct / 100.0) * width)))
    return "█" * filled + "░" * (width - filled)


def effort_url(effort: dict) -> str:
    repo = effort.get("repo")
    if repo:
        return f"https://github.com/{repo}"
    proof = effort.get("proof") or {}
    if isinstance(proof, dict) and proof.get("url"):
        return proof["url"]
    link = effort.get("link")
    if link:
        return link if link.startswith("http") else f"{SITE_URL}{link.lstrip('/')}"
    return SITE_URL


def snapshot_effort(effort: dict) -> dict:
    return {
        "name": effort.get("name") or effort.get("id"),
        "tier": effort.get("tier"),
        "repo": effort.get("repo"),
        "gpu": effort.get("gpu") or [],
        "verification": effort.get("verification"),
        "nuked": bool(effort.get("nuked")),
        "nuked_note": (effort.get("nuked") or {}).get("note", "") if isinstance(effort.get("nuked"), dict) else "",
        "gta": effort.get("gta") or {},
        "groundwork": effort.get("groundwork") or {},
        "score": compute_score(effort),
        "stage": get_stage_name(effort),
        "summary": effort.get("summary") or "",
        "url": effort_url(effort),
    }


def change_hash(entry: dict) -> str:
    raw = f"{entry.get('date', '')}|{entry.get('kind', '')}|{entry.get('text', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_editorial_change(entry: dict) -> bool:
    text = (entry.get("text") or "").strip()
    if not text:
        return False
    # Filter out routine unreviewed GitHub scout discoveries
    if entry.get("kind") == "new" and text.startswith("Scout spotted "):
        return False
    return True


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


def get_leaders(efforts_map: dict[str, dict]) -> tuple[tuple[str, float] | None, tuple[str, float] | None]:
    active = [
        (eid, e) for eid, e in efforts_map.items()
        if not e.get("nuked") and e.get("tier") == "tracked"
    ]
    overall = max(active, key=lambda kv: kv[1]["score"], default=None)
    public_only = [(eid, e) for eid, e in active if e.get("repo")]
    public_leader = max(public_only, key=lambda kv: kv[1]["score"], default=None)
    return (
        (overall[0], overall[1]["score"]) if overall else None,
        (public_leader[0], public_leader[1]["score"]) if public_leader else None,
    )


def build_state(tracker: dict, pinned) -> dict:
    efforts = {
        e["id"]: snapshot_effort(e)
        for e in (tracker.get("efforts") or [])
        if isinstance(e, dict) and e.get("id")
    }
    news_ids = [
        n["id"] for n in (tracker.get("news") or [])
        if isinstance(n, dict) and n.get("id")
    ]
    dmca_paths = [
        d["path"] for d in (tracker.get("dmca") or [])
        if isinstance(d, dict) and d.get("path")
    ]
    editorial_hashes = [
        change_hash(c) for c in (tracker.get("changes") or [])
        if isinstance(c, dict) and is_editorial_change(c)
    ]
    return {
        "schema_version": 2,
        "updated": tracker.get("updated"),
        "efforts": efforts,
        "news_ids": news_ids,
        "dmca_paths": dmca_paths,
        "editorial_hashes": editorial_hashes,
        "pinned_text": extract_pinned_text(pinned),
    }


def load_old_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Seamlessly migrate from v1 state.json if present
    if data.get("schema_version") != 2:
        old_tracker = data.get("tracker")
        if isinstance(old_tracker, dict):
            return build_state(old_tracker, data.get("pinned"))
        return None
    return data


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


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

    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        if webhook_url:
            url = webhook_url
            payload = {"username": "PortWatch VI", "embeds": batch}
            headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        else:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            payload = {"embeds": batch}
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bot {bot_token}",
                "User-Agent": USER_AGENT,
            }

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                print(f"[OK] Sent {len(batch)} embed(s) to Discord (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[ERROR] Discord API HTTP {e.code}: {body}", file=sys.stderr)
            sys.exit(1)

        if i + 10 < len(embeds):
            time.sleep(1.5)


def format_gta_deltas(old_gta: dict, new_gta: dict) -> list[str]:
    lines = []
    if old_gta.get("fps") != new_gta.get("fps"):
        lines.append(f"• **FPS:** `{old_gta.get('fps', 0)}` → **`{new_gta.get('fps', 0)} fps`**")
    if old_gta.get("story") != new_gta.get("story"):
        o = int(round(float(old_gta.get("story") or 0) * 100))
        n = int(round(float(new_gta.get("story") or 0) * 100))
        lines.append(f"• **Story:** `{o}%` → **`{n}%`**")
    if old_gta.get("render") != new_gta.get("render"):
        lines.append(f"• **Rendering:** `{old_gta.get('render', 0)}` → **`{new_gta.get('render', 0)}`**")
    if old_gta.get("stability") != new_gta.get("stability"):
        lines.append(f"• **Stability:** `{old_gta.get('stability', 'unknown')}` → **`{new_gta.get('stability', 'unknown')}`**")
    return lines


def main() -> None:
    tracker = fetch_json(TRACKER_URL)
    if not isinstance(tracker, dict):
        print("[WARN] Could not fetch tracker.json; skipping run.", file=sys.stderr)
        sys.exit(0)

    pinned = fetch_json(PINNED_URL)
    old_state = load_old_state()
    new_state = build_state(tracker, pinned)
    now_iso = datetime.now(timezone.utc).isoformat()

    overall_leader, public_leader = get_leaders(new_state["efforts"])
    overall_str = (
        f"{new_state['efforts'][overall_leader[0]]['name']} ({overall_leader[1]}%)"
        if overall_leader else "N/A"
    )
    public_str = (
        f"{new_state['efforts'][public_leader[0]]['name']} ({public_leader[1]}%)"
        if public_leader else "N/A"
    )

    if old_state is None:
        save_state(new_state)
        send_discord([{
            "title": "✅ PortWatch VI Tracker Connected",
            "url": SITE_URL,
            "description": (
                f"**Overall Leader:** {overall_str}\n"
                f"**Public Race #1:** {public_str}\n\n"
                "Watching for progress jumps, new builds, takedowns/DMCAs, news articles, and pinned announcements."
            ),
            "color": 0x7A5CFF,
            "timestamp": now_iso,
        }])
        return

    embeds = []

    # 1. Pinned announcement
    if new_state["pinned_text"] and new_state["pinned_text"] != old_state.get("pinned_text", ""):
        embeds.append({
            "title": "📌 New Pinned Announcement on PortWatch VI",
            "url": SITE_URL,
            "description": truncate(new_state["pinned_text"], 1500),
            "color": 0xFF8A3D,
            "timestamp": now_iso,
        })

    # 2. News articles (from tracker.json)
    seen_news = set(old_state.get("news_ids") or [])
    fresh_news = [
        n for n in (tracker.get("news") or [])
        if isinstance(n, dict) and n.get("id") and n["id"] not in seen_news
    ]
    for item in reversed(fresh_news[:5]):
        cat = item.get("category") or "News"
        article_url = f"{SITE_URL}news/{item['id']}.html"
        embed = {
            "title": f"📰 [{cat}] {truncate(item.get('title', 'New Article'), 220)}",
            "url": article_url,
            "description": truncate(item.get("dek") or "", 500),
            "color": CATEGORY_COLORS.get(cat, 0xFF2E7E),
            "footer": {"text": f"PortWatch VI News • {item.get('date', '')}"},
            "timestamp": now_iso,
        }
        img = item.get("image")
        if isinstance(img, dict) and isinstance(img.get("url"), str) and img["url"].startswith("https://"):
            embed["image"] = {"url": img["url"]}
        embeds.append(embed)

    # 3. New DMCA notices
    seen_dmca = set(old_state.get("dmca_paths") or [])
    for d in (tracker.get("dmca") or []):
        if isinstance(d, dict) and d.get("path") and d["path"] not in seen_dmca:
            affected = ", ".join(d.get("tracked") or []) or "See notice"
            embeds.append({
                "title": "⚖️ New DMCA Notice Logged",
                "url": d.get("url") or SITE_URL,
                "description": (
                    f"**Date:** {d.get('date', 'Unknown')}\n"
                    f"**Affected tracked projects:** {affected}\n"
                    f"**GTA VI related:** {'Yes' if d.get('gta6') else 'No'}"
                ),
                "color": 0xE74C3C,
                "timestamp": now_iso,
            })

    # 4. Effort updates (new projects, progress jumps, nukes, public/private changes, tier promotions)
    old_efforts = old_state.get("efforts") or {}
    new_efforts = new_state["efforts"]

    for eid, cur in new_efforts.items():
        gpu_tag = f" • {'/'.join(cur['gpu'])}" if cur["gpu"] else ""
        visibility = "Public" if cur["repo"] else "Private"

        if eid not in old_efforts:
            # Brand new effort added to the board
            embeds.append({
                "title": f"🆕 New {visibility} Build Added: {cur['name']} ({cur['score']}%)",
                "url": cur["url"],
                "description": (
                    f"`{make_bar(cur['score'])}` **{cur['score']}%** — *{cur['stage']}*{gpu_tag}\n\n"
                    f"{truncate(cur['summary'], 400)}"
                ),
                "color": 0x2ECC71 if cur["tier"] == "tracked" else 0x3498DB,
                "footer": {"text": f"Tier: {cur['tier']} • {cur['repo'] or 'Private code'}"},
                "timestamp": now_iso,
            })
            continue

        prev = old_efforts[eid]
        notes = []

        # Nuked / taken down
        if cur["nuked"] and not prev.get("nuked"):
            embeds.append({
                "title": f"💀 Project Nuked: {cur['name']}",
                "url": SITE_URL,
                "description": truncate(cur["nuked_note"] or "Marked as nuked on PortWatch VI.", 500),
                "color": 0xE74C3C,
                "timestamp": now_iso,
            })
            continue

        # Tier promotion (watchlist -> tracked)
        if prev.get("tier") != cur["tier"]:
            notes.append(f"• **Tier:** `{prev.get('tier')}` → **`{cur['tier']}`**")

        # Public <-> Private change
        if bool(prev.get("repo")) != bool(cur["repo"]):
            if cur["repo"]:
                notes.append(f"• 🔓 **Code went PUBLIC:** [`{cur['repo']}`](https://github.com/{cur['repo']})")
            else:
                notes.append("• 🔒 **Code went PRIVATE / offline**")

        # Stage or Score change
        score_changed = abs(cur["score"] - float(prev.get("score") or 0)) >= 0.1
        stage_changed = cur["stage"] != prev.get("stage")
        gta_deltas = format_gta_deltas(prev.get("gta") or {}, cur.get("gta") or {})

        if stage_changed:
            notes.append(f"• **Milestone:** `{prev.get('stage')}` → **`{cur['stage']}`**")
        notes.extend(gta_deltas)

        if score_changed or notes:
            diff = round(cur["score"] - float(prev.get("score") or 0), 1)
            arrow = f"+{diff}%" if diff > 0 else f"{diff}%"
            score_line = (
                f"`{make_bar(cur['score'])}` **{prev.get('score')}% → {cur['score']}%** (`{arrow}`)"
                if score_changed
                else f"`{make_bar(cur['score'])}` **{cur['score']}%**"
            )
            color = 0x2ECC71 if diff > 0 else (0xE67E22 if diff < 0 else 0x7A5CFF)
            body = f"{score_line} — *{cur['stage']}*{gpu_tag}\n"
            if notes:
                body += "\n" + "\n".join(notes)
            embeds.append({
                "title": f"🚀 {cur['name']} Updated ({visibility})",
                "url": cur["url"],
                "description": truncate(body, 1500),
                "color": color,
                "footer": {"text": f"Tier: {cur['tier']} • {cur['repo'] or 'Private build'}"},
                "timestamp": now_iso,
            })

    # 5. New #1 Leader callout
    old_overall, old_public = get_leaders(old_efforts)
    leader_notes = []
    if overall_leader and (not old_overall or overall_leader[0] != old_overall[0]):
        leader_notes.append(f"👑 **New Overall Leader:** **{overall_str}**")
    if public_leader and (not old_public or public_leader[0] != old_public[0]):
        leader_notes.append(f"🏁 **New #1 in Public Race:** **{public_str}**")
    if leader_notes:
        embeds.insert(0, {
            "title": "🏆 Leaderboard Shake-Up!",
            "url": f"{SITE_URL}#race",
            "description": "\n".join(leader_notes),
            "color": 0xF1C40F,
            "timestamp": now_iso,
        })

    # 6. Editorial changelog notes (non-scout entries)
    seen_hashes = set(old_state.get("editorial_hashes") or [])
    fresh_editorial = [
        c for c in (tracker.get("changes") or [])
        if isinstance(c, dict) and is_editorial_change(c) and change_hash(c) not in seen_hashes
    ]
    if fresh_editorial:
        icons = {"up": "📈", "down": "📉", "flag": "🚩", "removed": "🗑️", "claim": "💬", "new": "✨"}
        lines = [
            f"{icons.get(c.get('kind'), '•')} {truncate(c.get('text', ''), 220)}"
            for c in fresh_editorial[-10:]
        ]
        embeds.append({
            "title": "📋 What Changed on PortWatch VI",
            "url": f"{SITE_URL}#changes",
            "description": truncate("\n".join(lines), 3500),
            "color": 0x7A5CFF,
            "timestamp": now_iso,
        })

    if embeds:
        send_discord(embeds)
        save_state(new_state)
    else:
        # Still save migrated v2 state cleanly on first run after upgrading
        if not os.path.exists(STATE_FILE) or (
            json.load(open(STATE_FILE, encoding="utf-8")).get("schema_version") != 2
        ):
            save_state(new_state)
        print(
            f"[OK] Checked PortWatch VI (site updated {tracker.get('updated')}) | "
            f"Overall #1: {overall_str} | Public #1: {public_str} | No new updates."
        )


if __name__ == "__main__":
    main()
