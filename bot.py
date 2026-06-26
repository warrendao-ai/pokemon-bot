#!/usr/bin/env python3
"""
🎴 Pokemon Live Bidding Bot for Telegram

SETUP:
  BOT_TOKEN      = your bot token from @BotFather
  ADMIN_IDS      = your Telegram user ID (from @userinfobot)
  GROUP_CHAT_ID  = your group chat ID (run /testchat in the group to find it)

START AN AUCTION (from DM to bot):
  Send a photo with caption:
  /newauction Card Name | price | duration | inc1,inc2
  Duration: 5 = 5min, 1:30 = 1m30s, 1:00:00 = 1hr
  Example: /newauction Charizard Holo | 100 | 5 | 5,10

COMMANDS:
  /auction      - Show active auction
  /bid <amount> - Place a bid
  /myauctions   - Your bid history
  /endauction   - (admin) End early
  /listbids     - (admin) List all bids
  /setinc       - (admin) Change increments e.g. /setinc 10,25
  /testchat     - (admin) Diagnose chat IDs
  /mychatid     - Get your chat ID
"""

import os, json, time, threading, requests, logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS     = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0")) or None
DATA_FILE     = "/tmp/auction_data.json" if os.path.exists("/tmp") else "auction_data.json"
SGT           = timezone(timedelta(hours=8))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── In-memory store ───────────────────────────────────────────────────────────
_store: dict = {"auctions": {}, "active_auction": None, "next_id": 1}
data_lock = threading.Lock()

def _init_store():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
            _store.clear()
            _store.update(data)
            log.info(f"Loaded {len(_store.get('auctions', {}))} auctions from disk")
        except Exception as e:
            log.warning(f"Could not load from disk: {e}")

def load_data() -> dict:
    return _store

def save_data(data: dict):
    _store.clear()
    _store.update(data)
    snapshot = json.dumps(data, indent=2)
    def _flush():
        try:
            with open(DATA_FILE, "w") as f:
                f.write(snapshot)
        except Exception as e:
            log.warning(f"Disk flush failed: {e}")
    threading.Thread(target=_flush, daemon=True).start()

# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt(amount: float) -> str:
    return f"${int(amount):,}" if amount == int(amount) else f"${amount:,.2f}"

def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, SGT).strftime("%I:%M:%S %p")

def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def parse_duration(raw: str) -> float:
    raw = raw.strip()
    parts = raw.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0])*60 + int(parts[1])
        else:
            return float(raw) * 60
    except ValueError:
        raise ValueError(f"Invalid duration: {raw}")

def parse_increments(raw: str) -> list:
    result = []
    for p in raw.split(","):
        p = p.strip().replace("$", "")
        try:
            v = float(p)
            if v > 0:
                result.append(int(v) if v == int(v) else v)
        except ValueError:
            pass
    return result[:2] if result else [5, 10]

def get_user_display(user: dict) -> str:
    name = user.get("first_name", "Trainer")
    if user.get("last_name"):
        name += f" {user['last_name']}"
    return name

def dest_chat(fallback: int) -> int:
    return GROUP_CHAT_ID or fallback

# ── Telegram API ──────────────────────────────────────────────────────────────
def api(method: str, **kwargs) -> Optional[dict]:
    try:
        r = requests.post(f"{API}/{method}", json=kwargs, timeout=10)
        result = r.json()
        if not result.get("ok"):
            log.warning(f"API {method} error: {result}")
        return result
    except Exception as e:
        log.error(f"API {method} failed: {e}")
        return None

def send(chat_id, text, parse_mode="HTML", reply_markup=None) -> Optional[dict]:
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("sendMessage", **kwargs)

def send_photo(chat_id, photo_id, caption, parse_mode="HTML", reply_markup=None):
    kwargs = {"chat_id": chat_id, "photo": photo_id, "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("sendPhoto", **kwargs)

def send_video(chat_id, video_id, caption, parse_mode="HTML", reply_markup=None):
    kwargs = {"chat_id": chat_id, "video": video_id, "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("sendVideo", **kwargs)

def edit_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    kwargs = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return api("editMessageText", **kwargs)

def delete_message(chat_id, message_id):
    api("deleteMessage", chat_id=chat_id, message_id=message_id)

def remove_keyboard(chat_id, message_id):
    api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
        reply_markup={"inline_keyboard": []})

def answer_callback(callback_id, text="", alert=False):
    api("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)

# ── Keyboards ─────────────────────────────────────────────────────────────────
def bid_keyboard(increments: list) -> dict:
    buttons = [{"text": f"+{fmt(i)}", "callback_data": f"quickbid:{i}"}
               for i in increments[:2]]
    return {"inline_keyboard": [buttons]}

# ── Formatting ────────────────────────────────────────────────────────────────
def format_photo_caption(auction: dict) -> str:
    incs = auction.get("increments", [5, 10])
    inc_display = ", ".join(fmt(i) for i in incs)
    dur = fmt_duration(auction["ends_at"] - auction["starts_at"])
    return (
        f"🔔 <b>NEW AUCTION!</b>\n\n"
        f"🎴 <b>{auction['card_name']}</b>\n"
        f"💰 Starting Price: <b>{fmt(auction['start_price'])}</b>\n"
        f"🔼 Bid Increments: {inc_display}\n"
        f"⏱ Duration: {dur}"
    )

def format_timer_msg(auction: dict, time_left_override: float = None) -> str:
    time_left = max(0, int(time_left_override if time_left_override is not None
                           else auction["ends_at"] - time.time()))
    h, rem     = divmod(time_left, 3600)
    mins, secs = divmod(rem, 60)

    total    = auction["ends_at"] - auction["starts_at"]
    fraction = time_left / total if total > 0 else 0
    filled   = round(fraction * 10)
    bar      = "🟩" * filled + "⬜" * (10 - filled)

    if auction["bids"]:
        top      = auction["bids"][-1]
        bid_line = f"💰 <b>{fmt(top['amount'])}</b>  by {top['username']}"
    else:
        bid_line = f"💰 Starting at <b>{fmt(auction['start_price'])}</b>"

    if time_left > 0:
        countdown  = f"{h:02d}:{mins:02d}:{secs:02d}" if h > 0 else f"{mins:02d}:{secs:02d}"
        timer_line = f"⏱ <b>{countdown} remaining</b>"
    else:
        timer_line = "⛔ <b>AUCTION ENDED</b>"

    incs        = auction.get("increments", [5, 10])
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

def format_winner_msg(auction: dict) -> str:
    if auction["bids"]:
        w = auction["bids"][-1]
        return (
            f"🏆 <b>AUCTION ENDED!</b>\n\n"
            f"🎴 <b>{auction['card_name']}</b>\n"
            f"👑 Winner: <b>{w['username']}</b>\n"
            f"💰 Winning Bid: <b>{fmt(w['amount'])}</b>\n"
            f"📊 Total Bids: {len(auction['bids'])}\n\n"
            f"🎉 Congratulations!"
        )
    return f"⛔ <b>Auction ended — no bids placed.</b>\n🎴 {auction['card_name']} goes back to the vault."

# ── Cleanup ───────────────────────────────────────────────────────────────────
def cleanup_auction(auction: dict, gcid: int, timer_msg_id: int = None):
    mid  = auction.get("media_msg_id")
    dele = auction.get("media_msg_deletable", False)
    if mid:
        if dele: delete_message(gcid, mid)
        else: remove_keyboard(gcid, mid)
    if timer_msg_id:
        delete_message(gcid, timer_msg_id)
    for m in auction.get("extra_msg_ids", []):
        delete_message(gcid, m)

def end_auction(auction: dict, d: dict, gcid: int, timer_msg_id: int = None):
    auction["status"] = "ended"
    d["active_auction"] = None
    save_data(d)
    def _do():
        cleanup_auction(auction, gcid, timer_msg_id)
        send(gcid, format_winner_msg(auction))
    threading.Thread(target=_do, daemon=True).start()

# ── Live Timer ────────────────────────────────────────────────────────────────
def run_live_timer(gcid: int, auction_id: int, timer_msg_id: int):
    last_displayed = -1
    ends_at = _store["auctions"][str(auction_id)]["ends_at"]

    while True:
        time.sleep(0.25)
        now       = time.time()
        time_left = ends_at - now

        if time_left <= 0:
            with data_lock:
                d = load_data()
                auction = d["auctions"].get(str(auction_id))
                if auction and auction["status"] == "active":
                    end_auction(auction, d, gcid, timer_msg_id)
            return

        display_second = int(time_left)
        if display_second == last_displayed:
            continue
        last_displayed = display_second

        with data_lock:
            auction = load_data()["auctions"].get(str(auction_id))
            if not auction or auction["status"] != "active":
                return

        incs = auction.get("increments", [5, 10])
        edit_text(gcid, timer_msg_id,
                  format_timer_msg(auction, time_left_override=time_left),
                  reply_markup=bid_keyboard(incs))

# ── Media posting ─────────────────────────────────────────────────────────────
def post_auction_media(gcid: int, auction: dict):
    media = auction.get("media", [])
    incs  = auction.get("increments", [5, 10])
    cap   = format_photo_caption(auction)
    if not media:
        return None

    if len(media) == 1:
        first = media[0]
        if first["type"] == "video":
            return send_video(gcid, first["file_id"], cap, reply_markup=bid_keyboard(incs))
        else:
            return send_photo(gcid, first["file_id"], cap, reply_markup=bid_keyboard(incs))
    else:
        tg_media = []
        for i, m in enumerate(media[:10]):
            item = {"type": m["type"], "media": m["file_id"]}
            if i == 0:
                item["caption"] = cap
                item["parse_mode"] = "HTML"
            tg_media.append(item)
        api("sendMediaGroup", chat_id=gcid, media=tg_media)
        return None  # buttons go on timer message

# ── Album collector ───────────────────────────────────────────────────────────
_pending_albums: dict = {}
_album_lock = threading.Lock()

def flush_album(mgid: str):
    with _album_lock:
        bundle = _pending_albums.pop(mgid, None)
    if bundle:
        start_auction_from_media(bundle["msgs"])

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
            t = threading.Timer(2.0, flush_album, args=(mgid,))
            _pending_albums[mgid]["timer"] = t
            t.start()
        return

    if caption.lower().startswith("/newauction"):
        start_auction_from_media([msg])

def start_auction_from_media(msgs: list):
    main_msg = next((m for m in msgs
                     if (m.get("caption") or "").strip().lower().startswith("/newauction")), None)
    if not main_msg:
        return

    cid     = main_msg["chat"]["id"]
    caption = (main_msg.get("caption") or "").strip()
    body    = caption[len("/newauction"):].strip()
    parts   = [p.strip() for p in body.split("|")]

    if len(parts) < 3:
        send(cid,
             "❌ Wrong format:\n\n"
             "<code>/newauction Card Name | price | duration | inc1,inc2</code>\n\n"
             "Duration: <code>5</code>=5min  <code>1:30</code>=1m30s  <code>1:00:00</code>=1hr\n"
             "Increments optional — defaults to 5,10")
        return

    card_name = parts[0]
    try:
        start_price = float(parts[1].replace("$", "").replace(",", ""))
        duration    = parse_duration(parts[2])
    except ValueError:
        send(cid, "❌ Invalid price or duration.")
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

    gcid = dest_chat(cid)

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
            "increments":    increments,
            "bids":          [],
            "extra_msg_ids": [],
            "status":        "active",
            "starts_at":     time.time(),
            "ends_at":       time.time() + duration,
            "chat_id":       gcid,
        }
        d["auctions"][str(aid)] = auction
        d["active_auction"] = aid
        save_data(d)

    # Confirm to admin if posting to group
    if gcid != cid:
        send(cid, f"✅ Auction #{aid} starting in group!")

    # Post media to group
    media_result = post_auction_media(gcid, auction)
    if media_result and media_result.get("ok"):
        with data_lock:
            d = load_data()
            d["auctions"][str(aid)]["media_msg_id"] = media_result["result"]["message_id"]
            d["auctions"][str(aid)]["media_msg_deletable"] = (len(media_items) == 1)
            save_data(d)

    # Post timer + buttons
    incs         = auction.get("increments", [5, 10])
    timer_result = send(gcid, format_timer_msg(auction), reply_markup=bid_keyboard(incs))
    if timer_result and timer_result.get("ok"):
        timer_msg_id = timer_result["result"]["message_id"]
        with data_lock:
            d = load_data()
            d["auctions"][str(aid)]["timer_msg_id"] = timer_msg_id
            save_data(d)
        threading.Thread(target=run_live_timer, args=(gcid, aid, timer_msg_id), daemon=True).start()

    log.info(f"Auction #{aid} started: {card_name} @ {fmt(start_price)} for {fmt_duration(duration)}")

# ── Bid logic ─────────────────────────────────────────────────────────────────
def place_bid(cid: int, uid: int, uname: str, amount: float, msg_date: float = None):
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

        now = msg_date or time.time()
        if now > auction["ends_at"] + 2:
            send(cid, "⏰ Auction has already ended!")
            return False

        incs        = auction.get("increments", [5, 10])
        min_inc     = min(incs)
        min_bid     = (auction["bids"][-1]["amount"] + min_inc) if auction["bids"] else auction["start_price"]

        if amount < min_bid:
            send(cid, f"❌ Minimum bid is <b>{fmt(min_bid)}</b>")
            return False

        auction["bids"].append({"user_id": uid, "username": uname,
                                 "amount": amount, "time": now})
        save_data(d)

    gcid = auction.get("chat_id") or GROUP_CHAT_ID or cid
    ts   = fmt_time(now)
    text = (f"⚡ <b>New Bid!</b>\n\n"
            f"🎴 {auction['card_name']}\n"
            f"💰 {fmt(amount)} by <b>{uname}</b>\n"
            f"📊 Bid #{len(auction['bids'])}\n"
            f"🕐 {ts}")

    result = send(gcid, text, reply_markup=bid_keyboard(incs))
    if result and result.get("ok"):
        with data_lock:
            d = load_data()
            if str(aid) in d["auctions"]:
                d["auctions"][str(aid)].setdefault("extra_msg_ids", []).append(
                    result["result"]["message_id"])
                save_data(d)
    return True

# ── Commands ──────────────────────────────────────────────────────────────────
def cmd_start(msg: dict):
    name = get_user_display(msg["from"])
    send(msg["chat"]["id"],
         f"👋 Welcome, <b>{name}</b>!\n\n"
         f"🎴 <b>Pokémon Card Auction Bot</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━\n\n"
         f"<b>To bid:</b>\n"
         f"  /auction — See active auction\n"
         f"  /bid &lt;amount&gt; — Place a bid\n"
         f"  /myauctions — Your history\n\n"
         f"<b>Admin — start auction via DM:</b>\n"
         f"  Send a photo with caption:\n"
         f"  <code>/newauction Name | price | duration | inc1,inc2</code>\n\n"
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
        post_auction_media(cid, auction)
    send(cid, format_timer_msg(auction), reply_markup=bid_keyboard(incs))

def cmd_bid(msg: dict, args: list):
    cid   = msg["chat"]["id"]
    user  = msg["from"]
    mid   = msg["message_id"]
    delete_message(cid, mid)

    if not args:
        send(cid, f"❓ Usage: /bid &lt;amount&gt;  e.g. /bid 150")
        return
    try:
        amount = float(args[0].replace("$", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        send(cid, "❌ Invalid amount. Example: /bid 150")
        return

    place_bid(cid, user["id"], get_user_display(user), amount, msg_date=msg.get("date"))

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
            won = (auction["bids"] and auction["bids"][-1]["user_id"] == uid
                   and auction["status"] == "ended")
            my_bids.append({"card": auction["card_name"], "id": aid,
                             "top": top["amount"], "status": auction["status"], "won": won})
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
        gcid    = auction.get("chat_id") or GROUP_CHAT_ID or cid
        timer_msg_id = auction.get("timer_msg_id")
        end_auction(auction, d, gcid, timer_msg_id)
    if cid != gcid:
        send(cid, "✅ Auction ended.")

def cmd_listbids(msg: dict):
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
    auction = d["auctions"][str(aid)]
    if not auction["bids"]:
        send(cid, "No bids yet.")
        return
    lines = [f"<b>📋 Bids — {auction['card_name']}</b>\n"]
    for i, b in enumerate(reversed(auction["bids"]), 1):
        ts = datetime.fromtimestamp(b["time"], SGT).strftime("%I:%M:%S %p")
        lines.append(f"#{i}  {fmt(b['amount'])} — {b['username']}  [{ts}]")
    send(cid, "\n".join(lines))

def cmd_setinc(msg: dict, args: list):
    cid = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if uid not in ADMIN_IDS:
        send(cid, "⛔ Admin only.")
        return
    if not args:
        send(cid, "Usage: <code>/setinc 10,25</code>")
        return
    incs = parse_increments(args[0])
    with data_lock:
        d   = load_data()
        aid = d.get("active_auction")
        if not aid:
            send(cid, "No active auction.")
            return
        d["auctions"][str(aid)]["increments"] = incs
        save_data(d)
    send(cid, f"✅ Increments updated: {', '.join(fmt(i) for i in incs)}",
         reply_markup=bid_keyboard(incs))

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
    r = send(cid, "✅ Bot can send to this chat.")
    if GROUP_CHAT_ID and GROUP_CHAT_ID != cid:
        r2 = send(GROUP_CHAT_ID, f"✅ Test message from bot — GROUP_CHAT_ID works!")
        if r2 and r2.get("ok"):
            send(cid, "✅ Also sent to GROUP_CHAT_ID successfully.")
        else:
            send(cid, f"❌ GROUP_CHAT_ID send failed: {r2}")

# ── Callback handler ──────────────────────────────────────────────────────────
def handle_callback(cb: dict):
    query_id = cb["id"]
    user     = cb["from"]
    uid      = user["id"]
    uname    = get_user_display(user)
    cid      = cb["message"]["chat"]["id"]
    data     = cb.get("data", "")

    if data.startswith("quickbid:"):
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
            incs   = auction.get("increments", [5, 10])
            base   = auction["bids"][-1]["amount"] if auction["bids"] else auction["start_price"]
            amount = round(base + increment, 2)
            cb_date = cb.get("message", {}).get("date", time.time())
            auction["bids"].append({"user_id": uid, "username": uname,
                                     "amount": amount, "time": cb_date})
            save_data(d)

        answer_callback(query_id, f"✅ Bid placed: {fmt(amount)}")
        gcid = auction.get("chat_id") or GROUP_CHAT_ID or cid
        ts   = fmt_time(cb_date)
        text = (f"⚡ <b>New Bid!</b>\n\n"
                f"🎴 {auction['card_name']}\n"
                f"💰 {fmt(amount)} by <b>{uname}</b>\n"
                f"📊 Bid #{len(auction['bids'])}\n"
                f"🕐 {ts}")
        result = send(gcid, text, reply_markup=bid_keyboard(incs))
        if result and result.get("ok"):
            with data_lock:
                d = load_data()
                if str(aid) in d["auctions"]:
                    d["auctions"][str(aid)].setdefault("extra_msg_ids", []).append(
                        result["result"]["message_id"])
                    save_data(d)

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

    # Handle force-reply (custom bid)
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
                    place_bid(msg["chat"]["id"], msg["from"]["id"],
                              get_user_display(msg["from"]), amount,
                              msg_date=msg.get("date"))
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

# ── Polling ───────────────────────────────────────────────────────────────────
def run():
    log.info("🎴 Pokémon Bidding Bot starting…")
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("❌ Set BOT_TOKEN before running!")
        return
    _init_store()
    offset = 0
    log.info("✅ Polling for updates…")
    while True:
        try:
            resp   = requests.get(f"{API}/getUpdates",
                                  params={"offset": offset, "timeout": 30},
                                  timeout=35)
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
