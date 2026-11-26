import os
import asyncio
import io
import json
import logging
import re
import time
from datetime import datetime
from html import escape as html_escape
from aiohttp import web

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError,
    AuthKeyUnregisteredError, AuthKeyInvalidError,
    UserAlreadyParticipantError, SendCodeUnavailableError,
    PhoneNumberInvalidError, PhoneNumberBannedError
)
from telethon.tl.functions.auth import ResendCodeRequest, CancelCodeRequest
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest, GetFullChatRequest
)
from telethon.tl.functions.phone import (
    LeaveGroupCallRequest, EditGroupCallParticipantRequest
)
from telethon.tl.types import (
    InputPeerSelf, UpdateGroupCallParticipants,
    Channel, Chat as TelegramChat
)

from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.exceptions import (
    NoActiveGroupCall, NotInCallError, MTProtoClientNotConnected
)


# ── Styled Inline Button (Bot API 9.4+) ──────────────────────────────────────
class Btn(InlineKeyboardButton):
    """InlineKeyboardButton with style & icon_custom_emoji_id support (Bot API 9.4+)."""
    def __init__(self, *args, style: str = "", icon_custom_emoji_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._style = style
        self._icon_custom_emoji_id = icon_custom_emoji_id

    def to_dict(self, *args, **kwargs):
        d = super().to_dict(*args, **kwargs)
        if getattr(self, "_style", ""):
            d["style"] = self._style
        if getattr(self, "_icon_custom_emoji_id", ""):
            d["icon_custom_emoji_id"] = str(self._icon_custom_emoji_id)
        return d


# ── Styled Reply Keyboard Button (Bot API 9.4+) ──────────────────────────────
class SBtn(KeyboardButton):
    """KeyboardButton with style & icon_custom_emoji_id support (Bot API 9.4+)."""
    def __init__(self, *args, style: str = "", icon_custom_emoji_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._style = style
        self._icon_custom_emoji_id = icon_custom_emoji_id

    def to_dict(self, *args, **kwargs):
        d = super().to_dict(*args, **kwargs)
        if getattr(self, "_style", ""):
            d["style"] = self._style
        if getattr(self, "_icon_custom_emoji_id", ""):
            d["icon_custom_emoji_id"] = str(self._icon_custom_emoji_id)
        return d


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
OWNER_ID           = int(os.environ.get("OWNER_ID", "0"))
API_ID             = int(os.environ.get("API_ID", "0"))
API_HASH           = os.environ.get("API_HASH", "")
ACCOUNTS_FILE      = "accounts.json"
ADMIN_FILE         = "admins.json"
PORT               = int(os.environ.get("PORT", "10000"))
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL", "4"))

# ─── STATES ───────────────────────────────────────────────────────────────────
(
    MAIN_MENU,
    WAIT_PHONE,
    WAIT_CODE,
    WAIT_2FA,
    WAIT_LINK,
    WAIT_COUNT,
    WAIT_IMPORT,
    WAIT_CH_LINK,
    WAIT_CH_COUNT,
    WAIT_ADMIN_ID,
) = range(10)

# ─── BUTTON LABELS (100% matching BA VC JOINER screenshot) ────────────────────
BTN_JOIN_LIVE   = "🔴 JOIN LIVE"
BTN_JOIN_CH     = "🟢 JOIN CH/GRP"
BTN_ADD_ACCOUNT = "➕ ADD ACCOUNT"
BTN_ALL_ACCOUNT = "🗄️ ALL ACCOUNT"
BTN_ADD_ADMIN   = "➕ ADD ADMIN"
BTN_ALL_ADMIN   = "🗄️ ALL ADMIN"
BTN_BOT_IMPORT  = "📥 BOT IMPORT"
BTN_BOT_EXPORT  = "📤 BOT EXPORT"

ALL_MENU_KEYWORDS = (
    "JOIN LIVE",
    "JOIN CH/GRP",
    "ADD ACCOUNT",
    "ALL ACCOUNT",
    "ADD ADMIN",
    "ALL ADMIN",
    "BOT IMPORT",
    "BOT EXPORT",
)


def is_menu_button(text: str) -> bool:
    t = (text or "").upper()
    return any(k in t for k in ALL_MENU_KEYWORDS)


# ─── STORAGE ──────────────────────────────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return {}


def save_accounts(data):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_admins():
    if os.path.exists(ADMIN_FILE):
        try:
            with open(ADMIN_FILE) as f:
                return set(int(x) for x in json.load(f))
        except Exception:
            return set()
    return set()


def save_admins(ids):
    with open(ADMIN_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


admins = load_admins()


def parse_import_data(data):
    """Normalize exported accounts data to {phone: {name, session}}."""
    entries = {}
    invalid = 0
    items = []
    if isinstance(data, dict):
        if "session" in data and "phone" in data:
            items = [(data.get("phone"), data)]
        else:
            items = list(data.items())
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "phone" in item:
                items.append((item.get("phone"), item))
            else:
                invalid += 1
    else:
        raise ValueError("File format not recognized")

    for phone_raw, info in items:
        if not isinstance(info, dict):
            invalid += 1
            continue
        session = str(info.get("session") or "").strip()
        phone = str(phone_raw or info.get("phone") or "").strip().replace(" ", "")
        if not phone or len(session) < 20:
            invalid += 1
            continue
        name = str(info.get("name") or phone).strip()
        entries[phone] = {"name": name, "session": session}
    return entries, invalid


pending_clients = {}
active_clients  = {}
live_sessions   = {}
live_handlers   = {}
call_engines    = {}
watchdog_tasks  = {}
live_state      = {}


# ─── PREMIUM / CUSTOM EMOJI IDS (Bot API 9.4+ / Telegram Premium) ────────────
# বট ওনারের Telegram Premium অথবা Fragment Username থাকলে নিচের ID গুলোতে
# বাটন আইকন ও মেসেজে প্রিমিয়াম ইমোজি দেখাবে। বটে /emojiid লিখে ইমোজি পাঠালে ID পাবেন।
CUSTOM_EMOJI_IDS = {
    "join_live":   os.environ.get("EMOJI_JOIN_LIVE", "").strip(),
    "join_ch":     os.environ.get("EMOJI_JOIN_CH", "").strip(),
    "add_account": os.environ.get("EMOJI_ADD_ACCOUNT", "").strip(),
    "all_account": os.environ.get("EMOJI_ALL_ACCOUNT", "").strip(),
    "add_admin":   os.environ.get("EMOJI_ADD_ADMIN", "").strip(),
    "all_admin":   os.environ.get("EMOJI_ALL_ADMIN", "").strip(),
    "bot_import":  os.environ.get("EMOJI_BOT_IMPORT", "").strip(),
    "bot_export":  os.environ.get("EMOJI_BOT_EXPORT", "").strip(),
    "hand_raise":  os.environ.get("EMOJI_HAND_RAISE", "").strip(),
    "leave_live":  os.environ.get("EMOJI_LEAVE_LIVE", "").strip(),
    "bolt":        os.environ.get("EMOJI_BOLT", "").strip(),
    "cam":         os.environ.get("EMOJI_CAM", "").strip(),
    "ok":          os.environ.get("EMOJI_OK", "").strip(),
    "party":       os.environ.get("EMOJI_PARTY", "").strip(),
}


def ce(key: str, fallback: str) -> str:
    """Return <tg-emoji> HTML if custom emoji ID is configured, else fallback emoji."""
    eid = CUSTOM_EMOJI_IDS.get(key, "")
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


def _menu_btn(label_plain: str, label_unicode: str, emoji_key: str, style: str = "danger") -> SBtn:
    eid = CUSTOM_EMOJI_IDS.get(emoji_key, "")
    if eid:
        return SBtn(label_plain, style=style, icon_custom_emoji_id=eid)
    return SBtn(label_unicode, style=style)


# ─── KEYBOARDS & HELPERS ──────────────────────────────────────────────────────
def main_kb():
    """Red 4x2 reply keyboard matching BA VC JOINER UI."""
    return ReplyKeyboardMarkup(
        [
            [
                _menu_btn("JOIN LIVE",   BTN_JOIN_LIVE,   "join_live",   style="danger"),
                _menu_btn("JOIN CH/GRP", BTN_JOIN_CH,     "join_ch",     style="danger"),
            ],
            [
                _menu_btn("ADD ACCOUNT", BTN_ADD_ACCOUNT, "add_account", style="danger"),
                _menu_btn("ALL ACCOUNT", BTN_ALL_ACCOUNT, "all_account", style="danger"),
            ],
            [
                _menu_btn("ADD ADMIN",   BTN_ADD_ADMIN,   "add_admin",   style="danger"),
                _menu_btn("ALL ADMIN",   BTN_ALL_ADMIN,   "all_admin",   style="danger"),
            ],
            [
                _menu_btn("BOT IMPORT",  BTN_BOT_IMPORT,  "bot_import",  style="danger"),
                _menu_btn("BOT EXPORT",  BTN_BOT_EXPORT,  "bot_export",  style="danger"),
            ],
        ],
        resize_keyboard=True,
    )


def _kb_for_chat(chat_id=None):
    return main_kb()


def live_inline_kb():
    hr_id = CUSTOM_EMOJI_IDS.get("hand_raise", "")
    lv_id = CUSTOM_EMOJI_IDS.get("leave_live", "")
    return InlineKeyboardMarkup([
        [
            Btn("Hand Raise All" if hr_id else "✋ Hand Raise All", callback_data="live_hand_raise", style="success", icon_custom_emoji_id=hr_id),
            Btn("Leave Live"     if lv_id else "🔴 Leave Live",     callback_data="live_leave",      style="danger",  icon_custom_emoji_id=lv_id),
        ]
    ])


def _progress_bar(done: int, total: int) -> tuple[str, int]:
    if total <= 0:
        return "▭" * 10, 0
    pct = min(100, int((done / total) * 100))
    filled = min(10, round((done / total) * 10))
    bar = "▬" * filled + "▭" * (10 - filled)
    return bar, pct


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


def is_admin_user(uid: int) -> bool:
    return uid == OWNER_ID or uid in admins


def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and is_admin_user(u.id))


async def _drop_client(phone: str):
    client = active_clients.pop(phone, None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_client(phone: str, session_str: str) -> TelegramClient:
    c = active_clients.get(phone)
    if c is not None:
        try:
            if not c.is_connected():
                await c.connect()
            if await c.is_user_authorized():
                return c
        except (AuthKeyUnregisteredError, AuthKeyInvalidError):
            pass
        except Exception:
            pass
        await _drop_client(phone)
        call_engines.pop(phone, None)

    try:
        c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await c.connect()
    except (AuthKeyUnregisteredError, AuthKeyInvalidError):
        raise RuntimeError(
            f"{phone}: Session expired / logged out — "
            "add this account again with OTP."
        )

    try:
        if not await c.is_user_authorized():
            try:
                await c.disconnect()
            except Exception:
                pass
            raise RuntimeError(
                f"{phone}: Session expired / logged out — "
                "add this account again with OTP."
            )
    except RuntimeError:
        raise
    except (AuthKeyUnregisteredError, AuthKeyInvalidError):
        raise RuntimeError(
            f"{phone}: Session expired / logged out — "
            "add this account again with OTP."
        )

    active_clients[phone] = c
    return c


# ─── /start & MAIN MENU ───────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Only owner/admin allowed.")
        return ConversationHandler.END

    accs = load_accounts()
    admins_text = ", ".join(f"<code>{a}</code>" for a in sorted(admins)) or "None"

    def _start_text():
        return (
            f"{ce('bolt', '⚡')} <b>BA VC JOINER</b>\n\n"
            f"👑 <b>Owner</b> : <code>{OWNER_ID}</code>\n"
            f"🛡️ <b>Admins</b> : {admins_text}\n"
            f"👥 <b>Accounts</b> : {len(accs)}\n\n"
            f"<b>Now You Can Use This Bot....</b>"
        )

    try:
        await update.message.reply_text(
            _start_text(),
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
    except Exception as e:
        if "custom emoji" in str(e).lower() or "custom_emoji" in str(e).lower():
            logging.warning(f"[CUSTOM_EMOJI] Disabled due to Telegram API error: {e}")
            for k in CUSTOM_EMOJI_IDS:
                CUSTOM_EMOJI_IDS[k] = ""
            await update.message.reply_text(
                _start_text(),
                parse_mode="HTML",
                reply_markup=main_kb(),
            )
        else:
            raise
    return MAIN_MENU


async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END

    t = (update.message.text or "").strip()
    upper = t.upper()

    if "JOIN LIVE" in upper:
        return await live_start(update, ctx)
    elif "JOIN CH/GRP" in upper:
        return await ch_join_start(update, ctx)
    elif "ADD ACCOUNT" in upper:
        return await add_start(update, ctx)
    elif "ALL ACCOUNT" in upper:
        return await show_list(update, ctx)
    elif "ADD ADMIN" in upper:
        return await add_admin_start(update, ctx)
    elif "ALL ADMIN" in upper:
        return await show_admins(update, ctx)
    elif "BOT IMPORT" in upper or "IMPORT" in upper:
        return await import_start(update, ctx)
    elif "BOT EXPORT" in upper or "EXPORT" in upper:
        return await export_accounts(update, ctx)

    await update.message.reply_text("Please use the buttons 👇", reply_markup=main_kb())
    return MAIN_MENU


# ─── ADD ACCOUNT ──────────────────────────────────────────────────────────────
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = len(load_accounts())
    await update.message.reply_text(
        f"➕ <b>ADD ACCOUNT</b>\n\n"
        f"Already saved : <b>{n}</b> accounts\n\n"
        f"Send your phone number 👇\n"
        f"<b><i>Format</i></b> : <code>+8801XXXXXXXXX</code>\n\n"
        f"<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_PHONE


async def _cleanup_pending_login(ctx: ContextTypes.DEFAULT_TYPE, target_phone: str = None):
    phones_to_clean = set()
    prev_phone = ctx.user_data.pop("phone", None)
    if prev_phone:
        phones_to_clean.add(prev_phone)
    if target_phone:
        phones_to_clean.add(target_phone)
    prev_hash = ctx.user_data.pop("code_hash", None)
    ctx.user_data.pop("code_timeout", None)
    ctx.user_data.pop("code_sent_at", None)
    for ph in phones_to_clean:
        old = pending_clients.pop(ph, None)
        if old is not None:
            if prev_hash and ph == prev_phone:
                try:
                    if old.is_connected():
                        await asyncio.wait_for(
                            old(CancelCodeRequest(ph, prev_hash)), timeout=5
                        )
                except Exception:
                    pass
            try:
                await old.disconnect()
            except Exception:
                pass


def _describe_sent_code(sent_code) -> tuple[str, str]:
    t = getattr(sent_code, "type", None)
    tname = type(t).__name__ if t is not None else "Unknown"
    if tname == "SentCodeTypeApp":
        where = "Telegram App (777000)"
    elif tname == "SentCodeTypeSms":
        where = "SMS"
    elif tname == "SentCodeTypeCall":
        where = "Phone Call"
    elif tname in ("SentCodeTypeFlashCall", "SentCodeTypeMissedCall"):
        where = "Missed / Flash Call"
    elif tname == "SentCodeTypeEmailCode":
        pattern = getattr(t, "email_pattern", "registered email")
        where = f"Email ({pattern})"
    elif tname == "SentCodeTypeFragmentSms":
        where = "Fragment (Anonymous Number)"
    elif tname in ("SentCodeTypeFirebaseAndroid", "SentCodeTypeFirebaseIos"):
        where = "App / Firebase (নিচে Resend চাপুন)"
    elif tname == "SentCodeTypeSetUpEmailRequired":
        where = "Email Setup Required"
    else:
        where = tname

    nt = getattr(sent_code, "next_type", None)
    nt_name = type(nt).__name__ if nt is not None else ""
    return where, nt_name


def _resend_btn_label(next_t: str) -> str:
    if next_t == "SentCodeTypeSms":
        return "🔄 Resend Code (SMS)"
    if next_t == "SentCodeTypeCall":
        return "📞 Resend Code (Call)"
    if next_t in ("SentCodeTypeFlashCall", "SentCodeTypeMissedCall"):
        return "📲 Resend Code (Missed Call)"
    return "🔄 Resend Code"


def _otp_inline_kb(can_resend: bool = True, next_t: str = "SentCodeTypeSms"):
    row = []
    if can_resend:
        row.append(Btn(_resend_btn_label(next_t), callback_data="resend_otp", style="success"))
    row.append(Btn("❌ Cancel", callback_data="cancel_otp", style="danger"))
    return InlineKeyboardMarkup([row])


async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if is_menu_button(text):
        await _cleanup_pending_login(ctx)
        return await menu_handler(update, ctx)

    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "❌ <b>API_ID / API_HASH missing!</b>\n"
            "Environment Variables-এ সঠিক <code>API_ID</code> এবং <code>API_HASH</code> সেট করুন।",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    digits_and_plus = re.sub(r"[^\d+]", "", text)
    if digits_and_plus.startswith("00"):
        phone = "+" + re.sub(r"\D", "", digits_and_plus[2:])
    else:
        phone = "+" + re.sub(r"\D", "", digits_and_plus)

    if len(re.sub(r"\D", "", phone)) < 7:
        await update.message.reply_text(
            "⚠️ সঠিক ফরম্যাটে নাম্বার দিন 👇\n"
            "<b><i>Format</i></b> : <code>+8801XXXXXXXXX</code>\n\n"
            "<b><i>Cancel</i></b> : /cancel",
            parse_mode="HTML",
        )
        return WAIT_PHONE

    if phone in load_accounts():
        await update.message.reply_text("⚠️ This number is already saved!", reply_markup=main_kb())
        return MAIN_MENU

    await _cleanup_pending_login(ctx, phone)
    ctx.user_data["phone"] = phone

    c = TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="5.6.3 x64",
        lang_code="en",
        system_lang_code="en-US",
        connection_retries=5,
        retry_delay=1,
        timeout=30,
    )
    try:
        await asyncio.wait_for(c.connect(), timeout=35)
        pending_clients[phone] = c
        r = await asyncio.wait_for(c.send_code_request(phone), timeout=60)
        ctx.user_data["code_hash"] = r.phone_code_hash
        timeout_sec = getattr(r, "timeout", 60) or 60
        ctx.user_data["code_timeout"] = timeout_sec
        ctx.user_data["code_sent_at"] = time.monotonic()

        where, next_t = _describe_sent_code(r)
        tname = type(getattr(r, "type", None)).__name__
        logging.info(f"[OTP] {phone} → type={tname}, next={next_t}, timeout={timeout_sec}")

        extra = ""
        if tname == "SentCodeTypeApp":
            if next_t:
                extra = (
                    f"\n💡 <i>কোডটি আপনার Telegram অ্যাপের <b>Telegram (777000)</b> চ্যাটে গেছে। "
                    f"সেখানে না পেলে {timeout_sec} সেকেন্ড পর নিচের <b>Resend</b> বাটনে চাপুন।</i>\n"
                )
            else:
                extra = (
                    "\n⚠️ <i>কোডটি শুধুমাত্র Telegram অ্যাপের <b>Telegram (777000)</b> চ্যাটে পাঠানো হয়েছে "
                    "(SMS fallback চালু নেই)। নাম্বারটি Telegram অ্যাপে লগইন থাকলে সেখান থেকে কোড নিন, "
                    "নতুবা <b>BOT IMPORT</b> ব্যবহার করুন।</i>\n"
                )
        elif tname == "SentCodeTypeSms":
            extra = (
                "\n💡 <i>Telegram থেকে SMS রিকোয়েস্ট পাঠানো হয়েছে। বিদেশি নাম্বারে সার্ভার IP / 3rd-party API-এর কারণে "
                "SMS না এলে নাম্বারটি আগে Telegram অ্যাপে লগইন করে সেখান থেকে কোড নিন।</i>\n"
            )
        elif tname in ("SentCodeTypeFlashCall", "SentCodeTypeMissedCall"):
            extra = (
                "\n📞 <i>Telegram আপনার নাম্বারে একটি <b>Missed Call</b> দিয়েছে। "
                "কল আসা নাম্বারটির শেষের ডিজিটগুলোই (সাধারণত শেষ ৫টি সংখ্যা) আপনার কোড।</i>\n"
            )
        elif tname == "SentCodeTypeCall":
            extra = (
                "\n📞 <i>Telegram থেকে আপনার নাম্বারে <b>Phone Call</b> যাবে — কল রিসিভ করে কোডটি শুনুন।</i>\n"
            )
        elif tname in ("SentCodeTypeFirebaseAndroid", "SentCodeTypeFirebaseIos"):
            extra = (
                "\n⚠️ <i>Telegram মোবাইল অ্যাপ ভেরিফিকেশন (Firebase) চেয়েছে। "
                "নিচের <b>Resend</b> বাটন কাজ না করলে নাম্বারটি আগে অফিশিয়াল অ্যাপে লগইন করুন।</i>\n"
            )
        elif tname == "SentCodeTypeSetUpEmailRequired":
            extra = (
                "\n⚠️ <i>Telegram এই নাম্বারের জন্য প্রথমে অফিশিয়াল অ্যাপে Email সেটআপ চায়।</i>\n"
            )

        await update.message.reply_text(
            f"📨 <b>OTP SENT!</b>\n\n"
            f"📍 <b>Delivery</b> : <code>{html_escape(where)}</code>\n"
            f"{extra}\n"
            f"Enter the code below 👇\n"
            f"<i>(টিপস: টেলিগ্রামে কোড লিখলে যেন এক্সপায়ার না হয়, চাইলে স্পেস দিয়ে লিখুন যেমন <code>1 2 3 4 5</code>)</i>\n\n"
            f"<b><i>Cancel</i></b> : /cancel",
            parse_mode="HTML",
            reply_markup=_otp_inline_kb(can_resend=bool(next_t), next_t=next_t),
        )
        return WAIT_CODE
    except PhoneNumberInvalidError:
        await update.message.reply_text(
            "❌ নাম্বারটি সঠিক নয় (<code>PHONE_NUMBER_INVALID</code>)। কান্ট্রি কোডসহ সঠিক নাম্বার দিন।",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except PhoneNumberBannedError:
        await update.message.reply_text(
            "🚫 এই নাম্বারটি Telegram থেকে Ban করা হয়েছে (<code>PHONE_NUMBER_BANNED</code>)।",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except SendCodeUnavailableError:
        await update.message.reply_text(
            "⚠️ Telegram এই নাম্বারের জন্য এই সার্ভার/API থেকে কোড পাঠানো বন্ধ রেখেছে (<code>SEND_CODE_UNAVAILABLE</code>)।\n\n"
            "💡 আগে অফিশিয়াল Telegram অ্যাপে লগইন করে আবার চেষ্টা করুন অথবা <b>BOT IMPORT</b> ব্যবহার করুন।",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except FloodWaitError as e:
        await update.message.reply_text(
            f"⏳ Please wait {e.seconds} seconds and try again.",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except asyncio.TimeoutError:
        logging.warning(f"[OTP] send_code_request timed out for {phone}")
        await update.message.reply_text(
            "⏳ Telegram সার্ভারের সাথে কানেক্ট হতে সময় বেশি লাগছে (Timeout)। কিছুক্ষণ পর আবার চেষ্টা করুন।",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except Exception as e:
        logging.exception(f"[OTP] send_code_request failed for {phone}")
        err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        await update.message.reply_text(
            f"❌ Error: <code>{html_escape(err_msg)}</code>",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU


async def resend_otp_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return WAIT_CODE

    phone = ctx.user_data.get("phone")
    code_hash = ctx.user_data.get("code_hash")
    client = pending_clients.get(phone)
    if not phone or not code_hash or not client:
        await q.answer("❌ Session expired. Please try Add Account again.", show_alert=True)
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU

    timeout = ctx.user_data.get("code_timeout", 60)
    elapsed = time.monotonic() - ctx.user_data.get("code_sent_at", 0)
    if elapsed < timeout:
        wait_left = int(timeout - elapsed) + 1
        await q.answer(f"⏳ অনুগ্রহ করে আর {wait_left} সেকেন্ড অপেক্ষা করে আবার চাপুন।", show_alert=True)
        return WAIT_CODE

    await q.answer("🔄 Resending OTP...")
    try:
        if not client.is_connected():
            await asyncio.wait_for(client.connect(), timeout=30)
        r = await asyncio.wait_for(client(ResendCodeRequest(phone, code_hash)), timeout=45)
        ctx.user_data["code_hash"] = r.phone_code_hash
        ctx.user_data["code_timeout"] = getattr(r, "timeout", 60) or 60
        ctx.user_data["code_sent_at"] = time.monotonic()

        where, next_t = _describe_sent_code(r)
        tname = type(getattr(r, "type", None)).__name__
        logging.info(f"[OTP-RESEND] {phone} → type={tname}, next={next_t}")

        extra = ""
        if tname in ("SentCodeTypeFlashCall", "SentCodeTypeMissedCall"):
            extra = (
                "\n📞 <i>Missed Call আসা নাম্বারের শেষের ডিজিটগুলো (সাধারণত শেষ ৫টি সংখ্যা) কোড হিসেবে দিন।</i>\n"
            )
        elif tname == "SentCodeTypeCall":
            extra = "\n📞 <i>কল রিসিভ করে ভয়েস কোডটি শুনুন।</i>\n"

        await q.message.reply_text(
            f"🔄 <b>OTP RESENT!</b>\n\n"
            f"📍 <b>Delivery</b> : <code>{html_escape(where)}</code>\n"
            f"{extra}\n"
            f"Enter the code below 👇\n"
            f"<i>(স্পেস দিয়ে লিখতে পারেন, যেমন <code>1 2 3 4 5</code>)</i>",
            parse_mode="HTML",
            reply_markup=_otp_inline_kb(can_resend=bool(next_t), next_t=next_t),
        )
        return WAIT_CODE
    except SendCodeUnavailableError:
        await q.message.reply_text(
            "⚠️ Telegram এই নাম্বারের জন্য SMS/Call রিসেন্ড অপশন দেয়নি (<code>SEND_CODE_UNAVAILABLE</code>)।\n"
            "যদি Telegram অ্যাপে (777000) কোড এসে থাকে, তবে এখনও কোডটি এখানে দিতে পারবেন।",
            parse_mode="HTML",
            reply_markup=_otp_inline_kb(can_resend=False),
        )
        return WAIT_CODE
    except PhoneCodeExpiredError:
        await q.message.reply_text(
            "❌ কোডটির মেয়াদ শেষ হয়ে গেছে। আবার <b>➕ ADD ACCOUNT</b> থেকে চেষ্টা করুন।",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except FloodWaitError as e:
        await q.message.reply_text(
            f"⏳ Telegram FloodWait: {e.seconds} সেকেন্ড পর আবার চেষ্টা করুন।",
            reply_markup=_otp_inline_kb(can_resend=True),
        )
        return WAIT_CODE
    except Exception as e:
        logging.exception(f"[OTP-RESEND] failed for {phone}")
        err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        await q.message.reply_text(
            f"❌ Resend failed: <code>{html_escape(err_msg)}</code>",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU


async def cancel_otp_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Cancelled")
    await _cleanup_pending_login(ctx)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text("❌ Cancelled.", reply_markup=main_kb())
    return MAIN_MENU


async def got_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if is_menu_button(text):
        await _cleanup_pending_login(ctx)
        return await menu_handler(update, ctx)

    phone = ctx.user_data.get("phone")
    client = pending_clients.get(phone)
    if not client:
        await update.message.reply_text("❌ Session expired. Please /start again.", reply_markup=main_kb())
        return MAIN_MENU

    code = re.sub(r"\D", "", text)
    if not code:
        await update.message.reply_text(
            "⚠️ শুধুমাত্র সংখ্যার কোড দিন (যেমন <code>1 2 3 4 5</code>)।",
            parse_mode="HTML",
        )
        return WAIT_CODE

    try:
        if not client.is_connected():
            await client.connect()
        await client.sign_in(
            phone,
            code,
            phone_code_hash=ctx.user_data["code_hash"],
        )
        return await _save_account(update, ctx, client, phone)
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 <b>TWO-FACTOR AUTH</b>\n\n"
            "Your account has 2FA enabled.\n\n"
            "Enter your password below 👇\n\n"
            "<b><i>Cancel</i></b> : /cancel",
            parse_mode="HTML",
        )
        return WAIT_2FA
    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "❌ কোডটি ভুল হয়েছে। সঠিক কোডটি আবার লিখুন 👇",
            reply_markup=_otp_inline_kb(can_resend=True),
        )
        return WAIT_CODE
    except PhoneCodeExpiredError:
        await update.message.reply_text("❌ Code is expired. Please try again.", reply_markup=main_kb())
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_kb())
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU


async def got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if is_menu_button(text):
        await _cleanup_pending_login(ctx)
        return await menu_handler(update, ctx)

    phone = ctx.user_data.get("phone")
    client = pending_clients.get(phone)
    if not client:
        await update.message.reply_text("❌ Session expired.", reply_markup=main_kb())
        return MAIN_MENU
    try:
        await client.sign_in(password=text)
        return await _save_account(update, ctx, client, phone)
    except Exception as e:
        await update.message.reply_text(f"❌ Wrong password: {e}", reply_markup=main_kb())
        await _cleanup_pending_login(ctx, phone)
        return MAIN_MENU


async def _save_account(update, ctx, client, phone):
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
    accs = load_accounts()
    accs[phone] = {"name": name, "session": client.session.save()}
    save_accounts(accs)
    active_clients[phone] = client
    pending_clients.pop(phone, None)
    ctx.user_data.pop("phone", None)
    ctx.user_data.pop("code_hash", None)
    ctx.user_data.pop("code_timeout", None)
    ctx.user_data.pop("code_sent_at", None)
    await update.message.reply_text(
        f"✅ <b>ACCOUNT SAVED!</b>\n\n"
        f"👤 <b>Name</b> : {html_escape(name)}\n"
        f"📱 <b>Phone</b> : <code>{html_escape(phone)}</code>\n"
        f"📊 <b>Total Accounts</b> : {len(accs)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


# ─── ALL ACCOUNT ──────────────────────────────────────────────────────────────
async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accs = load_accounts()
    if not accs:
        await update.message.reply_text("🗄️ No accounts saved yet.", reply_markup=main_kb())
        return MAIN_MENU

    lines = ["🗄️ <b>ALL ACCOUNTS</b>\n"]
    for i, (ph, info) in enumerate(accs.items(), 1):
        status = " 🟢 <i>(Live)</i>" if ph in live_state else ""
        lines.append(
            f"<b>#{i}</b> 👤 {html_escape(info['name'])}{status}\n"
            f"     📱 <code>{html_escape(ph)}</code>\n"
        )
    lines.append(f"📊 <b>Total</b> : {len(accs)} Accounts")

    buttons = [
        [Btn(f"🗑️ Remove {ph}", callback_data=f"remove:{ph}", style="danger")]
        for ph in accs
    ]
    buttons.append([Btn("🗑️ Remove All Accounts", callback_data="remove_all", style="danger")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return MAIN_MENU


# ─── BOT EXPORT / BOT IMPORT ──────────────────────────────────────────────────
async def export_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    accs = load_accounts()
    if not accs:
        await update.message.reply_text(
            "📤 No accounts saved yet to export.",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    data = json.dumps(accs, indent=2, ensure_ascii=False).encode("utf-8")
    filename = (
        f"ba-vc-joiner-accounts-{len(accs)}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    )
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=filename,
        caption=(
            f"📤 Exported <b>{len(accs)}</b> accounts.\n"
            "⚠️ This file contains session strings — keep it private."
        ),
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


async def import_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    n = len(load_accounts())
    await update.message.reply_text(
        "📥 <b>BOT IMPORT</b>\n\n"
        f"Already saved : <b>{n}</b> accounts\n\n"
        "Send the exported <code>.json</code> file here 👇\n"
        "(Export button থেকে পাওয়া ফাইল)\n\n"
        "<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_IMPORT


async def got_import_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    if update.message and update.message.text and is_menu_button(update.message.text):
        return await menu_handler(update, ctx)

    doc = update.message.document if update.message else None
    if not doc:
        await update.message.reply_text(
            "⚠️ Please upload the exported .json file, or /cancel to go back."
        )
        return WAIT_IMPORT

    try:
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        data = json.loads(buf.getvalue().decode("utf-8"))
        entries, invalid = parse_import_data(data)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not read that file: {e}\n"
            "Please upload the file exported from BOT EXPORT.",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    if not entries:
        await update.message.reply_text(
            "⚠️ No valid accounts found in this file.",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    accs = load_accounts()
    added = 0
    skipped = 0
    for phone, info in entries.items():
        if phone in accs:
            skipped += 1
            continue
        accs[phone] = info
        added += 1

    if added:
        save_accounts(accs)

    await update.message.reply_text(
        "✅ <b>BOT IMPORT DONE</b>\n\n"
        f"✅ <b>Added</b> : {added} accounts\n"
        f"⏭️ <b>Already saved</b> : {skipped} accounts\n"
        f"❌ <b>Invalid</b> : {invalid} accounts\n"
        f"📊 <b>Total now</b> : {len(accs)} accounts",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


# ─── JOIN LIVE FLOW (100% matching screenshot) ────────────────────────────────
async def live_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not load_accounts():
        await update.message.reply_text("❌ Please add an account first.", reply_markup=main_kb())
        return MAIN_MENU

    await update.message.reply_text(
        f"{ce('cam', '🎥')} <b>Send Live Link</b>\n\n"
        "Live link বা @username দিন\n\n"
        "<b><i>Public</i></b> : @channelname or\n"
        "<u>channelname</u>\n"
        "<b><i>Private</i></b> : t.me/+xxxxxxxx\n\n"
        "<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_LINK


async def got_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    if is_menu_button(link):
        return await menu_handler(update, ctx)

    ctx.user_data["live_link"] = link
    accs = load_accounts()
    if not accs:
        await update.message.reply_text("❌ No accounts saved.", reply_markup=main_kb())
        return MAIN_MENU

    status_msg = await update.message.reply_text("⏳ Checking & joining group...")
    joined_phones = []
    resolved_entities = {}
    errors = []

    for phone, info in accs.items():
        try:
            client = await get_client(phone, info["session"])
            entity = await _resolve_and_join(client, link)
            if entity is None:
                errors.append(f"{phone}: Invalid link or no access")
                continue
            joined_phones.append(phone)
            resolved_entities[phone] = entity
        except FloodWaitError as e:
            errors.append(f"{phone}: FloodWait {e.seconds}s")
        except Exception as e:
            errors.append(f"{phone}: {type(e).__name__}: {e}")

    if not joined_phones:
        detail = "\n".join(f"• {html_escape(x)}" for x in dict.fromkeys(errors)[:5]) or "• Unknown error"
        await status_msg.edit_text(
            f"❌ কোনো account group এ join করতে পারেনি।\n\n🔎 <b>Reason:</b>\n{detail}",
            parse_mode="HTML",
        )
        return MAIN_MENU

    ctx.user_data["live_group_phones"] = joined_phones
    ctx.user_data["live_entities"] = resolved_entities

    await status_msg.edit_text(
        f"{ce('ok', '✅')} {len(joined_phones)} account group এ join হয়েছে।\n\n"
        f"কতটি দিয়ে VC-তে Join করাবেন? (1-{len(joined_phones)})\n\n"
        f"<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_COUNT


async def got_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if is_menu_button(text):
        return await menu_handler(update, ctx)

    group_phones = ctx.user_data.get("live_group_phones") or list(load_accounts().keys())
    max_n = len(group_phones)
    try:
        n = int(text)
        if not 1 <= n <= max_n:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"⚠️ সঠিক সংখ্যা লিখুন (1-{max_n})\n\n<b><i>Cancel</i></b> : /cancel",
            parse_mode="HTML",
        )
        return WAIT_COUNT

    ctx.user_data["join_count"] = n
    return await _do_join(update, ctx)


async def _do_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link = ctx.user_data["live_link"]
    count = ctx.user_data["join_count"]
    accs = load_accounts()
    group_phones = ctx.user_data.get("live_group_phones") or list(accs.keys())
    entities_map = ctx.user_data.get("live_entities") or {}
    phones = group_phones[:count]
    chat_id = update.effective_chat.id

    bar0, pct0 = _progress_bar(0, count)
    msg = await update.message.reply_text(
        f"⏳ <b>Joining Live VC...</b>\n\n"
        f"<b>Joined</b> : 0 / {count}\n\n"
        f"{bar0} {pct0}%",
        parse_mode="HTML",
    )

    joined = []
    calls_map = {}
    errors = []

    for phone in phones:
        info = accs.get(phone)
        if not info:
            continue
        try:
            client = await get_client(phone, info["session"])
            entity = entities_map.get(phone) or await _resolve_and_join(client, link)
            if entity is None:
                errors.append(f"{phone}: Could not resolve link/invite")
                continue

            call = None
            try:
                if isinstance(entity, Channel):
                    full = await client(GetFullChannelRequest(entity))
                else:
                    full = await client(GetFullChatRequest(entity.id))
                call = getattr(full.full_chat, "call", None)
            except Exception:
                try:
                    full = await client(GetFullChatRequest(entity.id))
                    call = getattr(full.full_chat, "call", None)
                except Exception:
                    call = None

            if call is None:
                errors.append("No active live/voice chat found — please start the live first")
                continue

            me = await client.get_me()
            engine = await _get_engine(phone, client)
            vchat = get_peer_id(entity)
            await _engine_join(phone, engine, vchat, call, me, client)

            handler = _make_remute_handler(client, call, me.id, phone)
            client.add_event_handler(handler, events.Raw(UpdateGroupCallParticipants))
            live_handlers.setdefault(phone, []).append(handler)

            state = {
                "engine": engine,
                "chat_id": vchat,
                "call": call,
                "client": client,
                "me": me,
            }
            live_state[phone] = state
            _start_watchdog(phone, state)
            calls_map[phone] = call
            joined.append(phone)
            logging.info(f"[{phone}] ✅ Joined live via REAL UDP engine")

            # Live progress update
            if len(joined) < count:
                bar_i, pct_i = _progress_bar(len(joined), count)
                try:
                    await msg.edit_text(
                        f"⏳ <b>Joining Live VC...</b>\n\n"
                        f"<b>Joined</b> : {len(joined)} / {count}\n\n"
                        f"{bar_i} {pct_i}%",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        except NoActiveGroupCall:
            errors.append(f"{phone}: No active live call found")
        except MTProtoClientNotConnected:
            _drop_engine(phone)
            await _drop_client(phone)
            errors.append(f"{phone}: MTProto disconnected — try Join Live again")
        except Exception as e:
            logging.exception(f"[{phone}] Live join failed")
            errors.append(f"{phone}: {type(e).__name__}: {e}")

    if not joined:
        detail = "\n".join(f"• {html_escape(x)}" for x in dict.fromkeys(errors)) or "• Unknown reason"
        await msg.edit_text(
            f"❌ Failed to join live.\n\n🔎 <b>Reason:</b>\n{detail}",
            parse_mode="HTML",
        )
        return MAIN_MENU

    live_sessions[chat_id] = {"phones": joined, "calls": calls_map, "link": link}
    bar, pct = _progress_bar(len(joined), count)
    party = f" {ce('party', '🎉')}" if pct == 100 else ""

    await msg.edit_text(
        f"{ce('ok', '✅')} <b>You Live Join Req Success</b>\n\n"
        f"<b>Your Target Members Live Jooin....</b>\n\n"
        f"<b>Joined</b> : {len(joined)} / {count}\n\n"
        f"{bar} {pct}%{party}",
        parse_mode="HTML",
        reply_markup=live_inline_kb(),
    )

    if count > len(joined) and errors:
        uniq = list(dict.fromkeys(errors))[:4]
        await update.message.reply_text(
            "⚠️ কিছু account join হতে পারেনি:\n" + "\n".join(f"• {x}" for x in uniq),
            reply_markup=main_kb(),
        )
    return MAIN_MENU


# ─── JOIN CH/GRP FLOW (New button from screenshot) ────────────────────────────
async def ch_join_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not load_accounts():
        await update.message.reply_text("❌ Please add an account first.", reply_markup=main_kb())
        return MAIN_MENU

    await update.message.reply_text(
        "🟢 <b>Send Channel / Group Link</b>\n\n"
        "Channel বা Group এর link বা @username দিন\n\n"
        "<b><i>Public</i></b> : @channelname or\n"
        "<u>channelname</u>\n"
        "<b><i>Private</i></b> : t.me/+xxxxxxxx\n\n"
        "<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_CH_LINK


async def got_ch_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    if is_menu_button(link):
        return await menu_handler(update, ctx)

    ctx.user_data["ch_link"] = link
    total = len(load_accounts())
    if total == 0:
        await update.message.reply_text("❌ No accounts saved.", reply_markup=main_kb())
        return MAIN_MENU

    await update.message.reply_text(
        f"👥 <b>SELECT ACCOUNT COUNT</b>\n\n"
        f"Total accounts : <b>{total}</b>\n\n"
        f"কতটি account দিয়ে Channel/Group এ Join করাবেন? (1-{total})\n\n"
        f"<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_CH_COUNT


async def got_ch_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if is_menu_button(text):
        return await menu_handler(update, ctx)

    accs = load_accounts()
    total = len(accs)
    try:
        n = int(text)
        if not 1 <= n <= total:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"⚠️ সঠিক সংখ্যা লিখুন (1-{total})\n\n<b><i>Cancel</i></b> : /cancel",
            parse_mode="HTML",
        )
        return WAIT_CH_COUNT

    link = ctx.user_data["ch_link"]
    phones = list(accs.keys())[:n]
    bar0, pct0 = _progress_bar(0, n)
    msg = await update.message.reply_text(
        f"⏳ <b>Joining Channel / Group...</b>\n\n"
        f"<b>Joined</b> : 0 / {n}\n\n"
        f"{bar0} {pct0}%",
        parse_mode="HTML",
    )

    joined = 0
    errors = []
    for phone in phones:
        info = accs.get(phone)
        if not info:
            continue
        try:
            client = await get_client(phone, info["session"])
            entity = await _resolve_and_join(client, link)
            if entity is None:
                errors.append(f"{phone}: Invalid link or no access")
                continue
            joined += 1
            if joined < n:
                bar_i, pct_i = _progress_bar(joined, n)
                try:
                    await msg.edit_text(
                        f"⏳ <b>Joining Channel / Group...</b>\n\n"
                        f"<b>Joined</b> : {joined} / {n}\n\n"
                        f"{bar_i} {pct_i}%",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except FloodWaitError as e:
            errors.append(f"{phone}: FloodWait {e.seconds}s")
        except Exception as e:
            errors.append(f"{phone}: {type(e).__name__}: {e}")

    bar, pct = _progress_bar(joined, n)
    party = " 🎉" if pct == 100 else ""
    if joined == 0:
        detail = "\n".join(f"• {html_escape(x)}" for x in dict.fromkeys(errors)[:5]) or "• Unknown error"
        await msg.edit_text(
            f"❌ Failed to join channel/group.\n\n🔎 <b>Reason:</b>\n{detail}",
            parse_mode="HTML",
        )
        return MAIN_MENU

    await msg.edit_text(
        f"✅ <b>Channel / Group Join Success</b>\n\n"
        f"<b>Joined</b> : {joined} / {n}\n\n"
        f"{bar} {pct}%{party}",
        parse_mode="HTML",
    )
    if errors:
        uniq = list(dict.fromkeys(errors))[:4]
        await update.message.reply_text(
            "⚠️ কিছু account join হতে পারেনি:\n" + "\n".join(f"• {x}" for x in uniq),
            reply_markup=main_kb(),
        )
    return MAIN_MENU


# ─── ADD ADMIN & ALL ADMIN (New buttons from screenshot) ──────────────────────
async def add_admin_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ This feature is for the Owner only.", reply_markup=main_kb())
        return MAIN_MENU

    await update.message.reply_text(
        "➕ <b>ADD NEW ADMIN</b>\n\n"
        "যাকে Admin করতে চান তার Telegram User ID দিন 👇\n\n"
        "<b><i>Example</i></b> : <code>123456789</code>\n"
        "<b><i>Cancel</i></b> : /cancel",
        parse_mode="HTML",
    )
    return WAIT_ADMIN_ID


async def got_admin_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ Owner only.", reply_markup=main_kb())
        return MAIN_MENU
    text = (update.message.text or "").strip()
    if is_menu_button(text):
        return await menu_handler(update, ctx)

    try:
        uid = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ user_id must be a number.\n\n<b><i>Cancel</i></b> : /cancel",
            parse_mode="HTML",
        )
        return WAIT_ADMIN_ID

    if uid == OWNER_ID:
        await update.message.reply_text("⚠️ This is your own owner ID.", reply_markup=main_kb())
        return MAIN_MENU
    if uid in admins:
        await update.message.reply_text(
            f"⚠️ <code>{uid}</code> is already an admin.",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    admins.add(uid)
    save_admins(admins)
    await update.message.reply_text(
        f"✅ <b>Admin Added Successfully!</b>\n\n"
        f"🆔 <b>User ID</b> : <code>{uid}</code>\n"
        f"🛡️ <b>Total Admins</b> : {len(admins)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


async def show_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END

    owner_line = f"👑 <b>Owner</b> : <code>{OWNER_ID}</code>\n" if OWNER_ID else "👑 <b>Owner</b> : (not set)\n"
    if admins:
        admin_lines = "\n".join(f"  • <code>{a}</code>" for a in sorted(admins))
    else:
        admin_lines = "  <i>(no admins)</i>"

    kb = None
    if is_owner(update) and admins:
        buttons = [
            [Btn(f"🗑️ Remove {a}", callback_data=f"rm_admin:{a}", style="danger")]
            for a in sorted(admins)
        ]
        buttons.append([Btn("🗑️ Remove All Admins", callback_data="rm_all_admins", style="danger")])
        kb = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(
        f"🗄️ <b>ALL ADMINS</b>\n\n"
        f"{owner_line}\n"
        f"🛡️ <b>Admins ({len(admins)})</b> :\n{admin_lines}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return MAIN_MENU


# ─── TELETHON / PYTGCALLS ENGINE ──────────────────────────────────────────────
async def _resolve_and_join(client: TelegramClient, link: str):
    try:
        clean_link = link.strip()
        if "t.me/+" in clean_link or "joinchat/" in clean_link:
            raw_hash = (
                clean_link.split("/+")[-1] if "/+" in clean_link
                else clean_link.split("joinchat/")[-1]
            )
            invite_hash = raw_hash.split("?")[0].strip().strip("/")
            try:
                check = await client(CheckChatInviteRequest(invite_hash))
                if hasattr(check, "chat"):
                    return check.chat
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
            except UserAlreadyParticipantError:
                check = await client(CheckChatInviteRequest(invite_hash))
                return getattr(check, "chat", None)
            except Exception:
                try:
                    result = await client(ImportChatInviteRequest(invite_hash))
                    return result.chats[0] if result.chats else None
                except UserAlreadyParticipantError:
                    check = await client(CheckChatInviteRequest(invite_hash))
                    return getattr(check, "chat", None)
        else:
            username = (
                clean_link.replace("https://", "")
                .replace("http://", "")
                .replace("t.me/", "")
                .replace("@", "")
                .split("?")[0]
                .strip()
                .split("/")[0]
            )
            entity = await client.get_entity(username)
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass
            return entity
    except Exception as e:
        logging.error(f"[RESOLVE] {link} → {e}")
        return None


_last_mute_at: dict = {}


def _make_remute_handler(client: TelegramClient, call, me_id: int, phone: str = None):
    async def handler(ev):
        for p in ev.participants:
            uid = getattr(p.peer, "user_id", None)
            if uid != me_id:
                continue
            if getattr(p, "left", False):
                logging.warning(f"[KICKED] {phone} live থেকে বের হয়েছে → watchdog ফিরে ঢুকাবে")
                continue
            if not p.muted:
                now = time.monotonic()
                if now - _last_mute_at.get(me_id, 0) < 60:
                    continue
                _last_mute_at[me_id] = now
                try:
                    await client(EditGroupCallParticipantRequest(
                        call=call, participant=InputPeerSelf(), muted=True,
                    ))
                    logging.info(f"[AUTO-MUTE] Re-muted user {me_id}")
                except Exception as ex:
                    logging.warning(f"[AUTO-MUTE] Failed for {me_id}: {ex}")
    return handler


SILENCE_FILE = "silence.wav"


def _ensure_silence() -> str:
    try:
        if not os.path.exists(SILENCE_FILE):
            import wave
            with wave.open(SILENCE_FILE, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000 * 60)
    except Exception as e:
        logging.warning(f"[SILENCE] {e}")
    return SILENCE_FILE


def _silence_stream():
    from pytgcalls.types.raw import Stream, AudioStream, AudioParameters
    from ntgcalls import MediaSource
    return Stream(
        microphone=AudioStream(
            MediaSource.FILE,
            _ensure_silence(),
            AudioParameters(bitrate=48000, channels=1),
        )
    )


def _tg_filter_stream_end():
    from pytgcalls import filters as tg_filters
    return tg_filters.stream_end()


def _register_stream_loop(engine: PyTgCalls, phone: str):
    @engine.on_update(_tg_filter_stream_end())
    async def _on_stream_end(c, update):
        try:
            await asyncio.sleep(2)
            await c.play(
                update.chat_id,
                _silence_stream(),
                config=GroupCallConfig(auto_start=False),
            )
            logging.info(f"[{phone}] 🔄 silent stream আবার চালু")
        except Exception as e:
            logging.warning(f"[{phone}] stream replay: {e}")


def _drop_engine(phone: str):
    call_engines.pop(phone, None)


async def _get_engine(phone: str, client: TelegramClient) -> PyTgCalls:
    eng = call_engines.get(phone)
    if eng is not None:
        bound = getattr(eng, "mtproto_client", None)
        if bound is not client or not client.is_connected():
            _drop_engine(phone)
            eng = None
    if eng is None:
        eng = PyTgCalls(client, workers=2)
        call_engines[phone] = eng
        _register_stream_loop(eng, phone)
    if not eng._is_running:
        await eng.start()
    return eng


async def _engine_join(
    phone: str,
    engine: PyTgCalls,
    chat_id: int,
    call,
    me,
    client: TelegramClient,
):
    try:
        from telethon.tl.types import InputPeerUser
        join_as = InputPeerUser(user_id=me.id, access_hash=me.access_hash)
    except Exception:
        join_as = me
    try:
        await engine.play(
            chat_id,
            _silence_stream(),
            config=GroupCallConfig(join_as=join_as, auto_start=False),
        )
    except Exception as e:
        logging.warning(f"[{phone}] silent stream failed ({e}) → media ছাড়া join")
        await engine.play(
            chat_id,
            None,
            config=GroupCallConfig(join_as=join_as, auto_start=False),
        )
    try:
        await engine.mute(chat_id)
    except Exception as e:
        logging.warning(f"[{phone}] engine mute: {e}")
    try:
        await client(EditGroupCallParticipantRequest(
            call=call, participant=InputPeerSelf(), muted=True
        ))
    except Exception as e:
        logging.warning(f"[{phone}] mtproto mute: {e}")
    logging.info(f"[{phone}] ✅ ঢুকেছে REAL UDP call-এ (muted)")


async def _conn_state(engine: PyTgCalls, chat_id: int):
    try:
        calls = await engine.calls
        if chat_id not in calls:
            return False, "NOT_IN_CALL"
        return True, "OK"
    except Exception:
        return True, "UNKNOWN"


async def _watchdog_loop(phone: str, state: dict):
    checks = 0
    fails = 0
    logging.info(f"[WATCHDOG] {phone} 🟢 started (every {KEEPALIVE_INTERVAL}s)")
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            in_call, _ = await _conn_state(state["engine"], state["chat_id"])
            if in_call:
                checks += 1
                if checks % 15 == 0:
                    logging.info(f"[WATCHDOG] {phone} ✅ live-এ আছে ({checks} checks)")
                continue
            logging.warning(f"[WATCHDOG] {phone} 🔄 connection গেছে → আবার join হচ্ছে")
            await _engine_join(
                phone,
                state["engine"],
                state["chat_id"],
                state["call"],
                state["me"],
                state["client"],
            )
            fails = 0
        except asyncio.CancelledError:
            logging.info(f"[WATCHDOG] {phone} ⛔ stopped")
            raise
        except NoActiveGroupCall:
            logging.warning(f"[WATCHDOG] {phone} ⛔ live শেষ → watchdog বন্ধ")
            break
        except Exception:
            fails += 1
            logging.exception(f"[WATCHDOG] {phone} ⚠️ rejoin failed ({fails}x)")
            try:
                await asyncio.sleep(min(KEEPALIVE_INTERVAL * (2 ** fails), 60))
            except asyncio.CancelledError:
                raise


def _start_watchdog(phone: str, state: dict):
    _stop_watchdog(phone)
    watchdog_tasks[phone] = asyncio.create_task(_watchdog_loop(phone, state))


def _stop_watchdog(phone: str):
    task = watchdog_tasks.pop(phone, None)
    if task and not task.done():
        task.cancel()


async def _do_hand_raise(chat_id: int, accs: dict) -> tuple:
    session = live_sessions.get(chat_id)
    if not session:
        return None, 0, 0
    phones = session["phones"]
    calls_map = session["calls"]
    success = 0
    fail = 0
    for phone in phones:
        info = accs.get(phone)
        if not info:
            continue
        call = calls_map.get(phone)
        if not call:
            fail += 1
            continue
        try:
            client = await get_client(phone, info["session"])
            await client(EditGroupCallParticipantRequest(
                call=call,
                participant=InputPeerSelf(),
                muted=True,
                raise_hand=True,
            ))
            logging.info(f"[HAND_RAISE] {phone} ✅ hand raised")
            success += 1
        except Exception as e:
            logging.warning(f"[HAND_RAISE] {phone} ❌ {e}")
            fail += 1
    return session, success, fail


async def _do_leave(chat_id: int, accs: dict) -> int:
    session = live_sessions.get(chat_id)
    if not session:
        return -1
    left = 0
    for phone in session["phones"]:
        _stop_watchdog(phone)
        st = live_state.pop(phone, {})
        engine = st.get("engine")
        vchat = st.get("chat_id")
        call = session["calls"].get(phone)
        info = accs.get(phone)
        if not info:
            continue
        try:
            client = await get_client(phone, info["session"])
            for h in live_handlers.pop(phone, []):
                try:
                    client.remove_event_handler(h)
                except Exception:
                    pass
            if engine is not None and vchat is not None:
                try:
                    await engine.leave_call(vchat)
                except NotInCallError:
                    pass
            elif call:
                await client(LeaveGroupCallRequest(call=call, source=0))
            left += 1
        except Exception as e:
            logging.error(f"[LEAVE] {phone}: {e}")
    del live_sessions[chat_id]
    return left


# ─── INLINE LIVE CONTROLS & COMMANDS ──────────────────────────────────────────
async def live_hand_raise_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    await q.answer("✋ Raising hands...")
    chat_id = q.message.chat_id
    accs = load_accounts()
    session, success, fail = await _do_hand_raise(chat_id, accs)
    if session is None:
        await q.message.reply_text("⚠️ No active live session found.", reply_markup=main_kb())
        return
    await q.message.reply_text(
        f"✋ <b>HAND RAISE DONE</b>\n\n"
        f"✅ <b>Raised</b> : {success} accounts\n"
        f"❌ <b>Failed</b> : {fail} accounts",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


async def live_leave_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    await q.answer("🔴 Leaving live...")
    chat_id = q.message.chat_id
    accs = load_accounts()
    left = await _do_leave(chat_id, accs)
    if left == -1:
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("⚠️ No active live session found.", reply_markup=main_kb())
        return
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(
        f"👋 <b>SESSION ENDED</b>\n\n"
        f"✅ Left Successfully\n"
        f"📊 <b>Accounts Left</b> : {left}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


async def cmd_raise(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    chat_id = update.effective_chat.id
    session, success, fail = await _do_hand_raise(chat_id, load_accounts())
    if session is None:
        await update.message.reply_text("⚠️ No active live session found.", reply_markup=main_kb())
        return
    await update.message.reply_text(
        f"✋ <b>HAND RAISE DONE</b>\n\n"
        f"✅ <b>Raised</b> : {success}\n"
        f"❌ <b>Failed</b> : {fail}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    chat_id = update.effective_chat.id
    left = await _do_leave(chat_id, load_accounts())
    if left == -1:
        await update.message.reply_text("⚠️ No active live session found.", reply_markup=main_kb())
        return
    await update.message.reply_text(
        f"👋 <b>SESSION ENDED</b>\n\n"
        f"✅ Left Successfully\n"
        f"📊 <b>Accounts Left</b> : {left}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


# ─── REMOVE ACCOUNT / ADMIN CALLBACKS ─────────────────────────────────────────
async def remove_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    await q.answer()
    phone = q.data.split(":", 1)[1]
    accs = load_accounts()
    info = accs.get(phone)
    if not info:
        await q.edit_message_text("⚠️ Account not found.")
        return
    name = info.get("name", phone)
    await q.edit_message_text(
        f"⚠️ <b>CONFIRM REMOVE</b>\n\n"
        f"👤 {html_escape(name)}\n"
        f"📱 <code>{html_escape(phone)}</code>\n\n"
        f"Remove this account?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                Btn("✅ Yes, Remove", callback_data=f"confirm_remove:{phone}", style="success"),
                Btn("❌ Cancel",      callback_data="cancel_remove",           style="danger"),
            ]
        ]),
    )


async def remove_all_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    await q.answer()
    n = len(load_accounts())
    await q.edit_message_text(
        f"⚠️ <b>CONFIRM REMOVE ALL</b>\n\n"
        f"This will remove all <b>{n}</b> accounts permanently.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                Btn("✅ Yes, Remove All", callback_data="confirm_remove_all", style="success"),
                Btn("❌ Cancel",          callback_data="cancel_remove",      style="danger"),
            ]
        ]),
    )


async def confirm_remove_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    await q.answer()
    phone = q.data.split(":", 1)[1]
    accs = load_accounts()
    info = accs.pop(phone, None)
    if info is None:
        await q.edit_message_text("⚠️ Account not found.")
        return
    name = info.get("name", phone)
    client = active_clients.pop(phone, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    _stop_watchdog(phone)
    _drop_engine(phone)
    live_state.pop(phone, None)
    live_handlers.pop(phone, None)
    save_accounts(accs)
    await q.edit_message_text(
        f"✅ <b>ACCOUNT REMOVED</b>\n\n"
        f"👤 {html_escape(name)} has been removed.\n"
        f"📊 <b>Remaining</b> : {len(accs)} accounts",
        parse_mode="HTML",
    )


async def confirm_remove_all_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return
    await q.answer()
    accs = load_accounts()
    for phone in list(accs.keys()):
        _stop_watchdog(phone)
        _drop_engine(phone)
        live_state.pop(phone, None)
        live_handlers.pop(phone, None)
        client = active_clients.pop(phone, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
    save_accounts({})
    await q.edit_message_text(
        "✅ <b>ALL ACCOUNTS REMOVED</b>\n\n"
        "All accounts have been removed successfully.",
        parse_mode="HTML",
    )


async def cancel_remove_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("❌ Cancelled.")


async def rm_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("⛔ Owner only!", show_alert=True)
        return
    await q.answer()
    try:
        uid = int(q.data.split(":", 1)[1])
    except ValueError:
        return
    if uid not in admins:
        await q.edit_message_text("⚠️ Admin not found.")
        return
    admins.discard(uid)
    save_admins(admins)
    await q.edit_message_text(
        f"🗑️ Admin <code>{uid}</code> removed successfully.",
        parse_mode="HTML",
    )


async def rm_all_admins_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("⛔ Owner only!", show_alert=True)
        return
    await q.answer()
    admins.clear()
    save_admins(admins)
    await q.edit_message_text("🗑️ All admins removed successfully.")


# ─── SLASH ADMIN & CANCEL COMMANDS ────────────────────────────────────────────
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ This command is for the Owner only.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "📖 <b>Usage:</b> <code>/add user_id</code>\nExample: <code>/add 123456789</code>",
            parse_mode="HTML",
        )
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return
    if uid == OWNER_ID:
        await update.message.reply_text("⚠️ This is your own owner ID.")
        return
    if uid in admins:
        await update.message.reply_text(f"⚠️ <code>{uid}</code> is already an admin.", parse_mode="HTML")
        return
    admins.add(uid)
    save_admins(admins)
    await update.message.reply_text(f"✅ <b>Admin added:</b> <code>{uid}</code>", parse_mode="HTML")


async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ This command is for the Owner only.")
        return
    if not ctx.args:
        admins_list = "\n".join(f"• <code>{a}</code>" for a in sorted(admins)) or "(no admins)"
        await update.message.reply_text(
            "📖 <b>Usage:</b> <code>/rm user_id</code>\n\n<b>Current admins:</b>\n" + admins_list,
            parse_mode="HTML",
        )
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return
    if uid not in admins:
        await update.message.reply_text(f"⚠️ <code>{uid}</code> is not an admin.", parse_mode="HTML")
        return
    admins.discard(uid)
    save_admins(admins)
    await update.message.reply_text(f"🗑️ <code>{uid}</code> admin access removed.", parse_mode="HTML")


async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_admins(update, ctx)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _cleanup_pending_login(ctx)
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_kb())
    return MAIN_MENU


async def cmd_emojiid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    src = update.message.reply_to_message or update.message
    entities = list(src.entities or []) + list(src.caption_entities or [])
    ids = []
    for ent in entities:
        cid = getattr(ent, "custom_emoji_id", None)
        if cid and cid not in ids:
            ids.append(cid)
    if not ids:
        await update.message.reply_text(
            "ℹ️ <b>Custom Emoji ID বের করার নিয়ম:</b>\n\n"
            "<code>/emojiid</code> লিখে স্পেস দিয়ে যেকোনো Premium/Custom Emoji পাঠান,\n"
            "অথবা কোনো Custom Emoji মেসেজে রিপ্লাই করে <code>/emojiid</code> লিখুন।",
            parse_mode="HTML",
        )
        return
    lines = ["✨ <b>Found Custom Emoji ID(s):</b>\n"]
    for i, cid in enumerate(ids, 1):
        lines.append(f"{i}. <code>{cid}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── HEALTH CHECK SERVER (Render + cron-job.org) ──────────────────────────────
async def health_handler(request):
    return web.Response(text="✅ BA VC JOINER Bot is alive!", status=200)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"✅ Health server running on port {PORT}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    await start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU:     [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)],
            WAIT_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            WAIT_CODE:     [
                CallbackQueryHandler(resend_otp_cb, pattern=r"^resend_otp$"),
                CallbackQueryHandler(cancel_otp_cb, pattern=r"^cancel_otp$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_code),
            ],
            WAIT_2FA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, got_2fa)],
            WAIT_LINK:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_link)],
            WAIT_COUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_count)],
            WAIT_CH_LINK:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_ch_link)],
            WAIT_CH_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_ch_count)],
            WAIT_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_admin_id)],
            WAIT_IMPORT:   [
                MessageHandler(filters.Document.ALL & ~filters.COMMAND, got_import_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_import_file),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(live_hand_raise_cb,    pattern=r"^live_hand_raise$"))
    app.add_handler(CallbackQueryHandler(live_leave_cb,         pattern=r"^live_leave$"))
    app.add_handler(CallbackQueryHandler(remove_cb,             pattern=r"^remove:"))
    app.add_handler(CallbackQueryHandler(remove_all_cb,         pattern=r"^remove_all$"))
    app.add_handler(CallbackQueryHandler(confirm_remove_cb,     pattern=r"^confirm_remove:"))
    app.add_handler(CallbackQueryHandler(confirm_remove_all_cb, pattern=r"^confirm_remove_all$"))
    app.add_handler(CallbackQueryHandler(cancel_remove_cb,      pattern=r"^cancel_remove$"))
    app.add_handler(CallbackQueryHandler(rm_admin_cb,           pattern=r"^rm_admin:"))
    app.add_handler(CallbackQueryHandler(rm_all_admins_cb,      pattern=r"^rm_all_admins$"))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("rm",      cmd_rm))
    app.add_handler(CommandHandler("admins",  cmd_admins))
    app.add_handler(CommandHandler("raise",   cmd_raise))
    app.add_handler(CommandHandler("leave",   cmd_leave))
    app.add_handler(CommandHandler("emojiid", cmd_emojiid))

    print("🤖 BA VC JOINER Bot starting...")
    async with app:
        await app.initialize()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(1)
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        print("✅ BA VC JOINER Bot is running!")
        try:
            await asyncio.Event().wait()
        finally:
            for ph in list(watchdog_tasks):
                _stop_watchdog(ph)
            live_state.clear()


if __name__ == "__main__":
    asyncio.run(main())
