#!/usr/bin/env python3
"""
🎴 Pokemon Live Bidding Bot — Flexible Increments Edition

HOW TO START A PHOTO AUCTION (admin only):
  Send a photo with caption:
    /newauction Card Name | start_price | minutes | inc1,inc2,inc3
  Example:
    /newauction Charizard Base Set Holo | 100 | 5 | 5,10,25,50
    /newauction Mewtwo 1st Edition | 500 | 10 | 20,50,100,200

  Increments are comma-separated — up to 4 values shown as quick-bid buttons.
  If you skip increments, defaults of 5,10,25,50 are used.

Commands:
  /auction     - Show active auction
  /bid <amt>   - Place any custom bid amount
  /myauctions  - Your bid history
  /endauction  - (admin) End early
  /listbids    - (admin) List all bids
  /setinc      - (admin) Change increments mid-auction
"""

import os
import json
import time
import threading
import requests
import logging
from datetime import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS      = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
GROUP_CHAT_ID  = int(os.getenv("GROUP_CHAT_ID", "0")) or None
DATA_FILE      = "auction_data.json"
POLL_TIMEOUT   = 30
TIMER_INTERVAL = 5
ANTISNIPE_SECS   = 30   # trigger if bid placed within this many seconds of end
ANTISNIPE_EXTEND = 30   # seconds added to timer when triggered

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Persistence ───────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"auctions": {}, "active_auction": None, "next_id": 1}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data_lock = threading.Lock()

# ── Telegram API helpers ──────────────────────────────────────────────────────
def api(method: str, **kwargs) -> Optional[dict]:
    try:
        r = requests.post(f"{API}/{method}", json=kwargs, timeout=10)
        result = r.json()
        if not result.get("ok"):
            log.warning(f"API error on {method}: {result}")
        return result
    except Exception as e:
        log.error(f"Request failed ({method}): {e}")
        return None

def group_chat(sender_cid: int) -> int:
    return GROUP_CHAT_ID if GROUP_CHAT_ID else sender_cid

def send(chat_id, text, parse_mode="HTML", reply_markup=None) -> Optional[dict]:
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("sendMessage", **kwargs)

def edit_text(chat_id, message_id, text, parse_mode="HTML"):
    api("editMessageText", chat_id=chat_id, message_id=message_id,
        text=text, parse_mode=parse_mode)

def send_photo(chat_id, photo_id, caption, parse_mode="HTML", reply_markup=None):
    kwargs = {"chat_id": chat_id, "photo": photo_id,
              "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("sendPhoto", **kwargs)

def send_video(chat_id, video_id, caption, parse_mode="HTML", reply_markup=None):
    kwargs = {"chat_id": chat_id, "video": video_id,
              "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("sendVideo", **kwargs)

def send_media_group(chat_id, media: list):
    tg_media = []
    for i, m in enumerate(media[:10]):
        item = {"type": m["type"], "media": m["file_id"]}
        if i == 0 and m.get("caption"):
            item["caption"]    = m["caption"]
            item["parse_mode"] = "HTML"
        tg_media.append(item)
    return api("sendMediaGroup", chat_id=chat_id, media=tg_media)

def delete_message(chat_id, message_id):
    api("deleteMessage", chat_id=chat_id, message_id=message_id)

def remove_keyboard(chat_id, message_id):
    api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
        reply_markup={"inline_keyboard": []})

def answer_callback(callback_id, text="", alert=False):
    api("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)

def get_user_display(user: dict) -> str:
    name = user.get("first_name", "Trainer")
    if user.get("last_name"):
        name += f" {user['last_name']}"
    return name

def fmt(amount: float) -> str:
    if amount == int(amount):
        return f"${int(amount):,}"
    return f"${amount:,.2f}"

# ── Keyboard builder ──────────────────────────────────────────────────────────
def bid_keyboard(increments: list) -> dict:
    inc_buttons = [
        {"text": f"+{fmt(i)}", "callback_data": f"quickbid:{i}"}
        for i in increments[:2]
    ]
    rows = []
    for i in range(0, len(inc_buttons), 2):
        rows.append(inc_buttons[i:i+2])
    return {"inline_keyboard": rows}

# ── Formatting ────────────────────────────────────────────────────────────────
def format_timer_msg(auction: dict) -> str:
    time_left = max(0, int(auction["ends_at"] - time.time()))
    mins, secs = divmod(time_left, 60)

    total    = auction["ends_at"] - auction["starts_at"]
    fraction = time_left / total if total > 0 else 0
    filled   = round(fraction * 10)
    bar      = "🟩" * filled + "⬜" * (10 - filled)

    if auction["bids"]:
        top      = auction["bids"][-1]
        bid_line = f"💰 <b>{fmt(top['amount'])}</b>  by {top['username']}"
    else:
        bid_line = f"💰 Starting at <b>{fmt(auction['start_price'])}</b>"

    timer_line = f"⏱ <b>{mins}m {secs}s remaining</b>" if time_left > 0 else "⛔ <b>AUCTION ENDED</b>"

    incs = auction.get("increments", [5, 10])
    inc_display = "  ".join(f"+{fmt(i)}" for i in incs)

    return (
        f"🎴 <b>{auction['card_name']}</b>  •  Auction #{auction['id']}\n"
        f"{bar}\n"
        f"{timer_line}\n"
        f"{bid_line}\n"
        f"📊 {len(auction['bids'])} bid(s) placed\n"
        f"🔼 Increments: {inc_display}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👇 Tap a button or use /bid &lt;amount&gt;"
    )

def format_photo_caption(auction: dict) -> str:
    incs = auction.get("increments", [5, 10])
    inc_display = ", ".join(fmt(i) for i in incs)
    return (
        f"🔔 <b>NEW AUCTION!</b>\n\n"
        f"🎴 <b>{auction['card_name']}</b>\n"
        f"💰 Starting Price: <b>{fmt(auction['start_price'])}</b>\n"
        f"🔼 Bid Increments: {inc_display}\n"
        f"⏱ Duration: {fmt_duration(auction['ends_at'] - auction['starts_at'])}"
    )

def format_winner_msg(auction: dict) -> str:
    if auction["bids"]:
        winner = auction["bids"][-1]
        return (
            f"🏆 <b>AUCTION ENDED!</b>\n\n"
            f"🎴 <b>{auction['card_name']}</b>\n"
            f"👑 Winner: <b>{winner['username']}</b>\n"
            f"💰 Winning Bid: <b>{fmt(winner['amount'])}</b>\n"
            f"📊 Total Bids: {len(auction['bids'])}\n\n"
            f"🎉 Congratulations!"
        )
    return (
        f"⛔ <b>Auction ended — no bids placed.</b>\n"
        f"🎴 {auction['card_name']} goes back to the vault."
    )

# ── Live timer thread ─────────────────────────────────────────────────────────
def run_live_timer(chat_id: int, auction_id: int, timer_msg_id: int):
    next_tick = time.time() + TIMER_INTERVAL
    while True:
        sleep_for = next_tick - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        next_tick += TIMER_INTERVAL

        with data_lock:
            d = load_data()
            if d.get("active_auction") != auction_id:
                return
            auction = d["auctions"].get(str(auction_id))
            if not auction:
                return

            time_left = auction["ends_at"] - time.time()

            if time_left <= 0:
                if auction["status"] == "active":
                    auction["status"] = "ended"
                    d["active_auction"] = None
                    save_data(d)
                    _cleanup_auction_messages(auction, chat_id, timer_msg_id)
                    send(chat_id, format_winner_msg(auction))
                return

        incs = auction.get("increments", [5, 10])
        api("editMessageText",
            chat_id=chat_id,
            message_id=timer_msg_id,
            text=format_timer_msg(auction),
            parse_mode="HTML",
            reply_markup=bid_keyboard(incs))

# ── Parse increments ──────────────────────────────────────────────────────────
def parse_increments(raw: str) -> list:
    result = []
    for part in raw.split(","):
        part = part.strip().replace("$", "")
        try:
            val = float(part)
            if val > 0:
                result.append(val if val != int(val) else int(val))
        except ValueError:
            pass
    return result[:2] if result else [5, 10]

# ── Duration parser ───────────────────────────────────────────────────────────
def parse_duration(raw: str) -> float:
    raw = raw.strip()
    parts = raw.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return m * 60 + s
        else:
            return float(raw) * 60
    except ValueError:
        raise ValueError(f"Invalid duration: {raw}")

def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m {s}s"
    elif m:
        return f"{m}m {s}s"
    else:
        return f"{s}s"

# ── Album collector ───────────────────────────────────────────────────────────
_pending_albums: dict = {}
_album_lock = threading.Lock()

def _flush_album(media_group_id: str):
    with _album_lock:
        bundle = _pending_albums.pop(media_group_id, None)
    if bundle:
        _start_auction_from_media(bundle["msgs"])

def handle_media_msg(msg: dict):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid not in ADMIN_IDS:
        return

    caption = (msg.get("caption") or "").strip()
    mgid    = msg.get("media_group_id")

    if mgid:
        with _album_lock:
            if mgid not in _pending_albums:
                _pending_albums[mgid] = {"msgs": [], "timer": None}
            _pending_albums[mgid]["msgs"].append(msg)
            if _pending_albums[mgid]["timer"]:
                _pending_albums[mgid]["timer"].cancel()
            t = threading.Timer(2.0, _flush_album, args=(mgid,))
            _pending_albums[mgid]["timer"] = t
            t.start()
        return

    if caption.lower().startswith("/newauction"):
        _start_auction_from_media([msg])

def _start_auction_from_media(msgs: list):
    main_msg = None
    for m in msgs:
        cap = (m.get("caption") or "").strip()
        if cap.lower().startswith("/newauction"):
            main_msg = m
            break
    if not main_msg:
        return

    cid     = main_msg["chat"]["id"]
    caption = (main_msg.get("caption") or "").strip()
    body    = caption[len("/newauction"):].strip()
    parts   = [p.strip() for p in body.split("|")]

    if len(parts) < 3:
        send(cid,
             "❌ Wrong format. Send media with caption:\n\n"
             "<code>/newauction Card Name | price | duration | increments</code>\n\n"
             "Examples:\n"
             "<code>/newauction Charizard Holo | 100 | 5 | 5,10</code>\n"
             "<code>/newauction Mewtwo 1st Ed | 500 | 10 | 20,50</code>\n\n"
             "Duration: <code>5</code>=5min  <code>1:30</code>=1m30s  <code>1:00:00</code>=1hr\n"
             "Increments optional — defaults to 5,10")
        return

    card_name = parts[0]
    try:
        start_price = float(parts[1].replace("$", "").replace(",", ""))
        duration    = parse_duration(parts[2])
    except ValueError:
        send(cid,
             "❌ Invalid price or duration.\n\n"
             "Duration formats:\n"
             "  <code>5</code> = 5 minutes\n"
             "  <code>1:30</code> = 1 min 30 sec\n"
             "  <code>1:30:00</code> = 1 hour 30 min")
        return

    increments = parse_increments(parts[3]) if len(parts) >= 4 else [5, 10]

    media_items = []
    for m in msgs:
        if "photo" in m:
            media_items.append({"type": "photo", "file_id": m["photo"][-1]["file_id"]})
        elif "video" in m:
            media_items.append({"type": "video", "file_id": m["video"]["file_id"]})

    if not media_items:
        send(cid, "❌ No photo or video detected.")
        return

    gcid = group_chat(cid)

    with data_lock:
        d = load_data()
        if d.get("active_auction"):
            send(cid, "⚠️ Auction already running! End it first with /endauction")
            return

        aid = d["next_id"]
        d["next_id"] += 1
        auction = {
            "id":            aid,
            "card_name":     card_name,
            "media":         media_items,
            "start_price":   start_price,
            "increments":    increments if increments else [5, 10],
            "bids":          [],
            "status":        "active",
            "starts_at":     time.time(),
            "ends_at":       time.time() + duration,
            "chat_id":       gcid,
            "extra_msg_ids": [],
        }
        d["auctions"][str(aid)] = auction
        d["active_auction"] = aid
        save_data(d)

    if gcid != cid:
        send(cid, f"✅ Auction starting in group!")

    media_result = _post_auction_media(gcid, auction)
    if media_result and media_result.get("ok"):
        with data_lock:
            d2 = load_data()
            if str(aid) in d2["auctions"]:
                d2["auctions"][str(aid)]["media_msg_id"] = media_result["result"]["message_id"]
                d2["auctions"][str(aid)]["media_msg_deletable"] = media_result.get("_deletable", False)
                d2["auctions"][str(aid)]["chat_id"] = gcid
                save_data(d2)

    incs_for_kbd = auction.get("increments", [5, 10])
    timer_result = send(gcid, format_timer_msg(auction), reply_markup=bid_keyboard(incs_for_kbd))
    if timer_result and timer_result.get("ok"):
        timer_msg_id = timer_result["result"]["message_id"]
        threading.Thread(target=run_live_timer, args=(gcid, aid, timer_msg_id), daemon=True).start()

    log.info(f"Auction #{aid} started: {card_name} @ ${start_price}, {len(media_items)} media item(s)")

def _post_auction_media(cid: int, auction: dict):
    media = auction.get("media", [])
    incs  = auction.get("increments", [5, 10])
    cap   = format_photo_caption(auction)

    if not media:
        return None

    if len(media) == 1:
        first = media[0]
        if first["type"] == "video":
            return send_video(cid, first["file_id"], cap, reply_markup=bid_keyboard(incs))
        else:
            return send_photo(cid, first["file_id"], cap, reply_markup=bid_keyboard(incs))
    else:
        tg_media = []
        for i, m in enumerate(media[:10]):
            item = {"type": m["type"], "media": m["file_id"]}
            if i == 0:
                item["caption"]    = cap
                item["parse_mode"] = "HTML"
            tg_media.append(item)
        api("sendMediaGroup", chat_id=cid, media=tg_media)
        return None

# ── Commands ──────────────────────────────────────────────────────────────────
def cmd_start(msg: dict):
    name = get_user_display(msg["from"])
    send(msg["chat"]["id"],
         f"👋 Welcome, <b>{name}</b>!\n\n"
         f"🎴 <b>Pokémon Card Auction Bot</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━\n\n"
         f"<b>To bid:</b>\n"
         f"  /auction — See active auction\n"
         f"  /bid &lt;amount&gt; — Place any bid\n"
         f"  /myauctions — Your history\n\n"
         f"<b>Admin — start auction via DM:</b>\n"
         f"  Send a photo with caption:\n"
         f"  <code>/newauction Name | price | duration | inc1,inc2</code>\n\n"
         f"<b>Admin — change increments mid-auction:</b>\n"
         f"  <code>/setinc 10,25</code>\n\n"
         f"<i>Good luck! 🌟</i>")

def cmd_auction(msg: dict):
    cid = msg["chat"]["id"]
    with data_lock:
        d = load_data()
    aid = d.get("active_auction")
    if not aid:
        send(cid, "😴 No active auction right now.")
        return
    auction = d["auctions"].get(str(aid))
    if not auction or auction["status"] != "active":
        send(cid, "😴 No active auction right now.")
        return
    incs = auction.get("increments", [5, 10])
    if auction.get("media"):
        _post_auction_media(cid, auction)
    send(cid, format_timer_msg(auction), reply_markup=bid_keyboard(incs))

def cmd_bid(msg: dict, args: list):
    cid   = msg["chat"]["id"]
    user  = msg["from"]
    uid   = user["id"]
    uname = get_user_display(user)
    mid   = msg["message_id"]

    delete_message(cid, mid)

    if not args:
        send(cid, f"❓ {uname}: use /bid &lt;amount&gt;  e.g. /bid 150")
        return
    try:
        amount = float(args[0].replace("$", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        send(cid, f"❌ {uname}: invalid amount. Example: /bid 150")
        return

    _place_bid(cid, uid, uname, amount)

def _cleanup_auction_messages(auction: dict, chat_id: int, timer_msg_id: int = None):
    media_msg_id = auction.get("media_msg_id")
    deletable    = auction.get("media_msg_deletable", False)
    if media_msg_id:
        if deletable:
            delete_message(chat_id, media_msg_id)
        else:
            remove_keyboard(chat_id, media_msg_id)
    if timer_msg_id:
        delete_message(chat_id, timer_msg_id)
    for mid in auction.get("extra_msg_ids", []):
        delete_message(chat_id, mid)

def _end_auction_now(auction: dict, d: dict, chat_id: int):
    auction["status"] = "ended"
    d["active_auction"] = None
    save_data(d)
    def _announce():
        gcid = GROUP_CHAT_ID or chat_id
        _cleanup_auction_messages(auction, gcid)
        send(gcid, format_winner_msg(auction))
    threading.Thread(target=_announce, daemon=True).start()

def _place_bid(cid: int, uid: int, uname: str, amount: float):
    with data_lock:
        d   = load_data()
        aid = d.get("active_auction")
        if not aid:
            send(cid, "😴 No active auction!")
            return False
        auction = d["auctions"].get(str(aid))
        if not auction or auction["status"] != "active":
            send(cid, "⛔ Auction is not active.")
            return False

        now = time.time()

        if now > auction["ends_at"] + 2:
            send(cid, "⏰ Auction has already ended!")
            return False

        incs        = auction.get("increments", [5, 10])
        min_inc     = min(incs)
        min_bid     = (auction["bids"][-1]["amount"] + min_inc) if auction["bids"] else auction["start_price"]

        if amount < min_bid:
            send(cid, f"❌ Minimum bid is <b>{fmt(min_bid)}</b>")
            return False

        auction["bids"].append({
            "user_id":  uid,
            "username": uname,
            "amount":   amount,
            "time":     now,
        })

        # ── Anti-snipe ────────────────────────────────────────────────────────
        antisnipe_triggered = False
        time_left = auction["ends_at"] - now
        if 0 < time_left <= ANTISNIPE_SECS:
            auction["ends_at"] += ANTISNIPE_EXTEND
            antisnipe_triggered = True
            log.info(f"Anti-snipe! Timer extended by {ANTISNIPE_EXTEND}s")

        save_data(d)

    gcid = GROUP_CHAT_ID or auction.get("chat_id") or cid
    ts   = datetime.fromtimestamp(now).strftime("%I:%M:%S %p")
    snipe_notice = f"\n⏰ <b>Anti-snipe! +{ANTISNIPE_EXTEND}s added!</b>" if antisnipe_triggered else ""
    bid_text = (f"⚡ <b>New Bid!</b>\n\n"
                f"🎴 {auction['card_name']}\n"
                f"💰 {fmt(amount)} by <b>{uname}</b>\n"
                f"📊 Bid #{len(auction['bids'])}\n"
                f"🕐 {ts}{snipe_notice}")
    bid_result = send(gcid, bid_text, reply_markup=bid_keyboard(incs))
    if bid_result and bid_result.get("ok"):
        with data_lock:
            d2 = load_data()
            aid2 = d2.get("active_auction")
            if aid2 and str(aid2) in d2["auctions"]:
                d2["auctions"][str(aid2)].setdefault("extra_msg_ids", []).append(
                    bid_result["result"]["message_id"])
                save_data(d2)
    return True

def cmd_setinc(msg: dict, args: list):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid not in ADMIN_IDS:
        send(cid, "⛔ Admin only.")
        return
    if not args:
        send(cid, "Usage: <code>/setinc 10,25</code>")
        return
    increments = parse_increments(args[0])
    if not increments:
        send(cid, "❌ Invalid increments. Example: <code>/setinc 10,25</code>")
        return
    with data_lock:
        d   = load_data()
        aid = d.get("active_auction")
        if not aid:
            send(cid, "No active auction.")
            return
        auction = d["auctions"].get(str(aid))
        auction["increments"] = increments
        save_data(d)
    inc_display = "  ".join(fmt(i) for i in increments)
    send(cid,
         f"✅ <b>Bid increments updated!</b>\n\nNew buttons: {inc_display}",
         reply_markup=bid_keyboard(increments))

def cmd_myauctions(msg: dict):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    with data_lock:
        d = load_data()
    my_bids = []
    for aid, auction in d["auctions"].items():
        user_bids = [b for b in auction["bids"] if b["user_id"] == uid]
        if user_bids:
            top = max(user_bids, key=lambda b: b["amount"])
            is_winner = (
                auction["bids"] and
                auction["bids"][-1]["user_id"] == uid and
                auction["status"] == "ended"
            )
            my_bids.append({
                "card": auction["card_name"], "id": aid,
                "top": top["amount"], "status": auction["status"], "won": is_winner,
            })
    if not my_bids:
        send(cid, "📭 You haven't placed any bids yet!")
        return
    lines = ["<b>📋 Your Bid History</b>\n━━━━━━━━━━━━━━━"]
    for b in sorted(my_bids, key=lambda x: x["id"], reverse=True):
        icon  = "🏆" if b["won"] else ("🟡" if b["status"] == "active" else "🔴")
        label = "WINNER!" if b["won"] else b["status"].upper()
        lines.append(f"{icon} #{b['id']} {b['card']} — {fmt(b['top'])} [{label}]")
    send(cid, "\n".join(lines))

def cmd_endauction(msg: dict):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid not in ADMIN_IDS:
        send(cid, "⛔ Admin only.")
        return
    with data_lock:
        d   = load_data()
        aid = d.get("active_auction")
        if not aid:
            send(cid, "No active auction.")
            return
        auction = d["auctions"].get(str(aid))
        gcid    = GROUP_CHAT_ID or auction.get("chat_id") or cid
        auction["status"] = "ended"
        d["active_auction"] = None
        save_data(d)
    _cleanup_auction_messages(auction, gcid)
    send(gcid, format_winner_msg(auction))
    if cid != gcid:
        send(cid, "✅ Auction ended.")

def cmd_listbids(msg: dict):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid not in ADMIN_IDS:
        send(cid, "⛔ Admin only.")
        return
    with data_lock:
        d = load_data()
    aid = d.get("active_auction")
    if not aid:
        send(cid, "No active auction.")
        return
    auction = d["auctions"][str(aid)]
    if not auction["bids"]:
        send(cid, "No bids yet.")
        return
    lines = [f"<b>📋 Bids — {auction['card_name']}</b>\n"]
    for i, b in enumerate(reversed(auction["bids"]), 1):
        ts = datetime.fromtimestamp(b["time"]).strftime("%I:%M:%S %p")
        lines.append(f"#{i}  {fmt(b['amount'])} — {b['username']}  [{ts}]")
    send(cid, "\n".join(lines))

def cmd_mychatid(msg: dict):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    send(cid,
         f"🆔 This chat ID: <code>{cid}</code>\n"
         f"Your user ID: <code>{uid}</code>\n\n"
         f"Run /testchat in your GROUP to get the group chat ID.")

def cmd_testchat(msg: dict):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid not in ADMIN_IDS:
        send(cid, "⛔ Admin only.")
        return
    lines = [
        f"<b>🔍 Chat Diagnostic</b>",
        f"This chat ID: <code>{cid}</code>",
        f"Your user ID: <code>{uid}</code>",
        f"GROUP_CHAT_ID: <code>{GROUP_CHAT_ID or 'NOT SET'}</code>",
        f"ADMIN_IDS: <code>{list(ADMIN_IDS)}</code>",
    ]
    send(cid, "\n".join(lines))
    if GROUP_CHAT_ID and GROUP_CHAT_ID != cid:
        r2 = send(GROUP_CHAT_ID, "✅ Test message — GROUP_CHAT_ID works!")
        if r2 and r2.get("ok"):
            send(cid, "✅ GROUP_CHAT_ID works!")
        else:
            send(cid, f"❌ GROUP_CHAT_ID failed: {r2}")

# ── Callback handler ──────────────────────────────────────────────────────────
def handle_callback(cb: dict):
    query_id = cb["id"]
    user     = cb["from"]
    uid      = user["id"]
    uname    = get_user_display(user)
    cid      = cb["message"]["chat"]["id"]
    data     = cb.get("data", "")

    if data == "custombid":
        with data_lock:
            d = load_data()
        aid = d.get("active_auction")
        if not aid:
            answer_callback(query_id, "⛔ Auction has ended!", alert=True)
            return
        auction = d["auctions"].get(str(aid))
        if not auction or auction["status"] != "active" or time.time() > auction["ends_at"]:
            answer_callback(query_id, "⛔ Auction has ended!", alert=True)
            return
        answer_callback(query_id)
        result = api("sendMessage",
            chat_id=cid,
            text=".",
            reply_markup={"force_reply": True, "selective": True,
                          "input_field_placeholder": "/bid "})
        if result and result.get("ok"):
            delete_message(cid, result["result"]["message_id"])

    elif data.startswith("quickbid:"):
        increment = float(data.split(":")[1])
        with data_lock:
            d   = load_data()
            aid = d.get("active_auction")
            if not aid:
                answer_callback(query_id, "No active auction!", alert=True)
                return
            auction = d["auctions"].get(str(aid))
            if not auction or auction["status"] != "active" or time.time() > auction["ends_at"]:
                answer_callback(query_id, "Auction ended!", alert=True)
                return

            base   = auction["bids"][-1]["amount"] if auction["bids"] else auction["start_price"]
            amount = round(base + increment, 2)
            incs   = auction.get("increments", [5, 10])
            now    = time.time()

            auction["bids"].append({
                "user_id": uid, "username": uname,
                "amount": amount, "time": now,
            })

            # ── Anti-snipe ────────────────────────────────────────────────────
            antisnipe_triggered = False
            time_left = auction["ends_at"] - now
            if 0 < time_left <= ANTISNIPE_SECS:
                auction["ends_at"] += ANTISNIPE_EXTEND
                antisnipe_triggered = True
                log.info(f"Anti-snipe on quickbid! Timer extended by {ANTISNIPE_EXTEND}s")

            save_data(d)

        answer_callback(query_id, f"✅ Bid placed: {fmt(amount)}")
        gcid = GROUP_CHAT_ID or auction.get("chat_id") or cid
        ts   = datetime.fromtimestamp(now).strftime("%I:%M:%S %p")
        snipe_notice = f"\n⏰ <b>Anti-snipe! +{ANTISNIPE_EXTEND}s added!</b>" if antisnipe_triggered else ""
        qtext = (f"⚡ <b>New Bid!</b>\n\n"
                 f"🎴 {auction['card_name']}\n"
                 f"💰 {fmt(amount)} by <b>{uname}</b>\n"
                 f"📊 Bid #{len(auction['bids'])}\n"
                 f"🕐 {ts}{snipe_notice}")
        qresult = send(gcid, qtext, reply_markup=bid_keyboard(incs))
        if qresult and qresult.get("ok"):
            with data_lock:
                d3 = load_data()
                if str(aid) in d3["auctions"]:
                    d3["auctions"][str(aid)].setdefault("extra_msg_ids", []).append(
                        qresult["result"]["message_id"])
                    save_data(d3)

# ── Dispatcher ────────────────────────────────────────────────────────────────
def dispatch(update: dict):
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return

    msg = update.get("message")
    if not msg:
        return

    if "photo" in msg or "video" in msg:
        handle_media_msg(msg)
        return

    if "text" not in msg:
        return

    text = msg["text"].strip()

    # Handle force-reply custom bid
    reply_to = msg.get("reply_to_message", {})
    if reply_to.get("from", {}).get("is_bot"):
        raw = text.strip()
        if raw.lower().startswith("/bid"):
            raw = raw[4:].strip()
        if raw and not raw.startswith("/"):
            try:
                amount = float(raw.replace("$", "").replace(",", ""))
                if amount > 0:
                    delete_message(msg["chat"]["id"], msg["message_id"])
                    _place_bid(msg["chat"]["id"], msg["from"]["id"],
                               get_user_display(msg["from"]), amount)
                    return
            except ValueError:
                pass

    parts = text.split()
    cmd   = parts[0].lower().split("@")[0]
    args  = parts[1:]

    handlers = {
        "/start":      lambda: cmd_start(msg),
        "/auction":    lambda: cmd_auction(msg),
        "/bid":        lambda: cmd_bid(msg, args),
        "/myauctions": lambda: cmd_myauctions(msg),
        "/endauction": lambda: cmd_endauction(msg),
        "/listbids":   lambda: cmd_listbids(msg),
        "/setinc":     lambda: cmd_setinc(msg, args),
        "/mychatid":   lambda: cmd_mychatid(msg),
        "/testchat":   lambda: cmd_testchat(msg),
    }

    handler = handlers.get(cmd)
    if handler:
        try:
            handler()
        except Exception as e:
            log.error(f"Error in {cmd}: {e}", exc_info=True)

# ── Polling loop ──────────────────────────────────────────────────────────────
def run():
    log.info("🎴 Pokémon Bidding Bot starting…")
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("❌ Set BOT_TOKEN before running!")
        return

    offset = 0
    log.info("✅ Polling for updates…")

    while True:
        try:
            resp = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT},
                timeout=POLL_TIMEOUT + 5,
            )
            result = resp.json()
            if not result.get("ok"):
                log.warning(f"getUpdates error: {result}")
                time.sleep(3)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                threading.Thread(target=dispatch, args=(update,), daemon=True).start()

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.error(f"Polling error: {e}", exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    run()
