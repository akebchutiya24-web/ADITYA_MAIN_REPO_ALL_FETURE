import random
import re
import time

from pyrogram import filters, StopPropagation
from pyrogram.enums import ChatMemberStatus, ButtonStyle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from pytgcalls.types.input_stream import AudioPiped

try:
    # py-tgcalls version ke hisaab se video-capable stream class ka naam/path
    # thoda alag ho sakta hai — is bot ke installed py-tgcalls==0.9.7 mein
    # yahi expected hai. Agar import fail ho, /vplay disable ho jaata hai
    # (audio-only /play par koi asar nahi padta).
    from pytgcalls.types.input_stream import AudioVideoPiped
    _VIDEO_SUPPORTED = True
except ImportError:
    AudioVideoPiped = None
    _VIDEO_SUPPORTED = False

try:
    # Video ko default max quality ke bajaye medium par bhejte hain — kam
    # bandwidth/CPU chahiye, isse atakna (buffering/lag) kam hota hai.
    from pytgcalls.types.input_stream.quality import MediumQualityVideo, HighQualityAudio
    _QUALITY_SUPPORTED = True
except ImportError:
    MediumQualityVideo = None
    HighQualityAudio = None
    _QUALITY_SUPPORTED = False

from pytgcalls.exceptions import NoActiveGroupCall

import config
import db
import music_queue as q
import progress
import botstate
from clients import bot, assistant, call_py, LOGGER, START_TIME
from youtube import search_track, get_stream_url, search_related_track, get_video_stream_url
from helpers import (
    smallcaps,
    smallcaps_title,
    random_processing_emoji,
    processing_caption,
    format_duration,
    fancy_italic,
    duration_to_seconds,
    format_uptime,
    blockquote,
    expandable_blockquote,
    esc,
    FOOTER_LINE,
)
from nowplaying import generate_now_playing_card
from assistant_join import ensure_assistant_in_chat

OWNER_FILTER = filters.user(config.OWNER_ID) if config.OWNER_ID else filters.create(lambda _, __, ___: False)

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

# (Pehle yahan /addvd wala pending-media system tha aur start image ke liye
# START_IMAGE_URL bhi — dono hata diye gaye hain; start message ab hamesha
# plain text jaata hai, jo pehle reliably kaam kar raha tha.)


# ---------------------------------------------------------------------------
# Peer cache helper — "Peer id invalid" error se bachne ke liye.
# Assistant account jab kisi chat mein direct koi update receive nahi karta
# (sirf VC join karta hai), to pyrogram uska peer/access_hash cache nahi kar
# paata aur baad mein change_stream/leave_group_call fail ho jaata hai.
# Isliye error aane par ek baar dialogs refresh karke retry karte hain.
# ---------------------------------------------------------------------------
async def _refresh_assistant_peers():
    try:
        async for _ in assistant.get_dialogs():
            pass
    except Exception as e:
        LOGGER.warning(f"Peer refresh fail: {e}")


def _is_peer_error(e: Exception) -> bool:
    return isinstance(e, ValueError) and "Peer id invalid" in str(e)


# ---------------------------------------------------------------------------
# Admin / owner check — /skip /pause /resume /stop /reload sirf group admin
# ya bot OWNER_ID ke liye. Normal user sirf /play use kar sakta hai.
# ---------------------------------------------------------------------------
async def _is_group_admin(client, chat_id: int, user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except Exception:
        return False


ADMIN_ONLY_TEXT = f"❌ {smallcaps_title('you are not authorized to use this command')}."

NOT_YOUR_REQUEST_TEXT = (
    f"❌ {smallcaps_title('this is not your request')}!\n"
    f"{smallcaps_title('only the person who requested this track, or a group admin/owner, can control it')}."
)


# ---------------------------------------------------------------------------
# Control-permission check — /skip /pause /resume /stop (aur inke inline
# buttons) sirf 3 log use kar sakte hain: jisne current track request kiya
# tha, group admin, ya bot owner. Baaki normal users ko NOT_YOUR_REQUEST_TEXT
# dikhaya jaata hai.
# ---------------------------------------------------------------------------
async def _can_control(client, chat_id: int, user_id: int) -> bool:
    if await _is_group_admin(client, chat_id, user_id):
        return True
    track = q.get_now_playing(chat_id)
    return bool(track and track.get("requested_by_id") == user_id)


ASSISTANT_NOT_JOINED_TEXT = (
    f"❌ **{smallcaps_title('my assistant account is not in this group')}!**\n\n"
    f"{smallcaps_title('the assistant account needs to be in the group to play music')}.\n"
    f"👉 @{config.ASSISTANT_USERNAME} {smallcaps_title('add it to the group, or get it to join')}.\n\n"
    f"{smallcaps_title('then run')} `/play` {smallcaps_title('again')}."
)

ASSISTANT_FLOOD_TEXT = (
    f"⏳ {smallcaps_title('telegram has rate-limited us for a bit, try again shortly')}."
)


# ---------------------------------------------------------------------------
# Owner: /on /off — pura bot chalu/band karne ke liye global switch.
# OFF hone par bot kisi bhi message/button ka jawab nahi deta, sirf /on /off
# chalte rehte hain. DB mein persist hota hai, isliye restart ke baad bhi
# wahi status yaad rehta hai.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("on") & OWNER_FILTER)
async def on_command(client, message: Message):
    botstate.set_enabled(True)
    await db.set_bot_status(True)
    await message.reply_text(f"✅ {smallcaps_title('bot turned on')}.")


@bot.on_message(filters.command("off") & OWNER_FILTER)
async def off_command(client, message: Message):
    botstate.set_enabled(False)
    await db.set_bot_status(False)
    await message.reply_text(
        f"🔴 {smallcaps_title('bot turned off')}.\n"
        f"{smallcaps_title('only')} `/on` {smallcaps_title('will work now')}."
    )


@bot.on_message(filters.command("processingon") & OWNER_FILTER)
async def processingon_command(client, message: Message):
    botstate.set_processing_text_enabled(True)
    await db.set_processing_text_status(True)
    await message.reply_text(f"✅ {smallcaps_title('processing text turned on')}.")


@bot.on_message(filters.command("processingoff") & OWNER_FILTER)
async def processingoff_command(client, message: Message):
    botstate.set_processing_text_enabled(False)
    await db.set_processing_text_status(False)
    await message.reply_text(
        f"🔴 {smallcaps_title('processing text turned off')}.\n"
        f"{smallcaps_title('now only the emoji will be sent, like before')}."
    )


# ---------------------------------------------------------------------------
# /vplayon /vplayoff — sirf ASLI bot owner. OFF hone par koi bhi (owner ke
# alawa) /vplay use nahi kar paata.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("vplayon") & OWNER_FILTER)
async def vplayon_command(client, message: Message):
    botstate.set_vplay_enabled(True)
    await db.set_vplay_status(True)
    await message.reply_text(f"✅ {smallcaps_title('/vplay turned on')} — {smallcaps_title('everyone can use it again')}.")


@bot.on_message(filters.command("vplayoff") & OWNER_FILTER)
async def vplayoff_command(client, message: Message):
    botstate.set_vplay_enabled(False)
    await db.set_vplay_status(False)
    await message.reply_text(
        f"🔴 {smallcaps_title('/vplay turned off')}.\n"
        f"{smallcaps_title('only the owner can use it now')}."
    )


def _off_blocker(_, __, message: Message) -> bool:
    if botstate.is_enabled():
        return False
    text = message.text or message.caption or ""
    # /on aur /off hamesha chalne chahiye, chahe bot OFF hi ho
    return not text.startswith(("/on", "/off"))


def _off_blocker_cb(_, __, cq: CallbackQuery) -> bool:
    return not botstate.is_enabled()


# group=-1 -> yeh handler sabse pehle chalta hai; OFF hone par isse aage kisi
# aur handler tak message/callback pahunchta hi nahi (StopPropagation).
@bot.on_message(filters.create(_off_blocker), group=-1)
async def _blocked_while_off(client, message: Message):
    raise StopPropagation


@bot.on_callback_query(filters.create(_off_blocker_cb), group=-1)
async def _blocked_cb_while_off(client, cq: CallbackQuery):
    await cq.answer(smallcaps_title("bot is currently off"), show_alert=True)
    raise StopPropagation


# ---------------------------------------------------------------------------
# /restrict /unrestrict enforcement — restricted user group mein bot ka koi
# bhi command use nahi kar sakta. Yeh sabse pehle chalta hai (group=-1),
# isliye /restrict /unrestrict khud bhi is se guzarte hain — lekin sirf
# admin/owner hi unhe chala paate hain, aur wo restricted nahi hote.
# ---------------------------------------------------------------------------
RESTRICTED_TEXT = (
    f"🚫 {smallcaps_title('you have been restricted by an admin')}!\n"
    f"{smallcaps_title('you cannot use any of my commands in this group')}."
)


def _restrict_command_filter(_, __, message: Message) -> bool:
    return bool(
        message.chat
        and message.chat.type != "private"
        and message.from_user
        and message.text
        and message.text.startswith("/")
    )


@bot.on_message(filters.create(_restrict_command_filter), group=-1)
async def _restrict_enforcer(client, message: Message):
    if await db.is_restricted(message.chat.id, message.from_user.id):
        await message.reply_text(RESTRICTED_TEXT)
        raise StopPropagation


def _btn(text: str, *, style: str = None, **kwargs) -> InlineKeyboardButton:
    """Telegram Bot API 9.4 colored inline button."""
    if style:
        style_map = {
            "primary": ButtonStyle.PRIMARY,
            "success": ButtonStyle.SUCCESS,
            "danger": ButtonStyle.DANGER,
        }
        return InlineKeyboardButton(
            text,
            style=style_map.get(style, ButtonStyle.PRIMARY),
            **kwargs,
        )
    # Default every inline button to Telegram Bot API 9.4 primary style.
    return InlineKeyboardButton(text, style=ButtonStyle.PRIMARY, **kwargs)


REPO_ALERT_TEXT = (
    f"🔒 {smallcaps_title('this is a premium closed-source repo')}.\n\n"
    f"{smallcaps_title('want to buy this exact repo or get your own bot built')}? "
    f"{smallcaps_title('contact')} : https://t.me/nexor_blaze"
)


def _controls_keyboard(elapsed_sec=None, total_sec=None):
    rows = []
    if total_sec and total_sec > 0:
        from progress import render_button_bar
        rows.append([_btn(render_button_bar(elapsed_sec or 0, total_sec), callback_data="m_progress", style="primary")])
    rows.extend([
        [
            _btn("▶️", callback_data="m_resume", style="success"),
            _btn("⏸", callback_data="m_pause", style="primary"),
            _btn("🔁", callback_data="m_replay", style="primary"),
            _btn("⏭", callback_data="m_skip", style="success"),
            _btn("⏹", callback_data="m_stop", style="danger"),
        ],
        [
            _btn("-10⏪", callback_data="m_seek_back", style="primary"),
            _btn("ʀᴇᴘᴏ", callback_data="m_boss", style="danger"),
            _btn("⏩10+", callback_data="m_seek_fwd", style="primary"),
        ],
        [_btn(f"⚙️ {smallcaps_title('more settings')}", callback_data="m_settings", style="primary")],
    ])
    return InlineKeyboardMarkup(rows)


def _settings_keyboard(chat_id: int):
    autoplay_on = q.get_autoplay(chat_id)
    return InlineKeyboardMarkup(
        [
            [
                _btn(
                    f"🔁 {smallcaps_title('toggle autoplay')}",
                    callback_data="m_autoplay_toggle",
                    style="success" if not autoplay_on else "danger",
                )
            ],
            [_btn(f"⏭ {smallcaps_title('skip autoplay')}", callback_data="m_autoplay_skip", style="primary")],
            [_btn(f"🔙 {smallcaps_title('back')}", callback_data="m_settings_back", style="primary")],
        ]
    )


def _settings_text(chat_id: int) -> str:
    autoplay_on = q.get_autoplay(chat_id)
    status = f"{smallcaps_title('autoplay')} : {smallcaps_title('on')} ✅" if autoplay_on else f"{smallcaps_title('autoplay')} : {smallcaps_title('off')} ❌"
    header = blockquote(f"⚙️ {smallcaps_title('more settings')}")
    body = expandable_blockquote(
        f"🔁 {smallcaps_title('autoplay')}\n"
        f"{smallcaps_title('status')} : {status}\n\n"
        f"{smallcaps_title('a related song will automatically play after each track')}."
    )
    return f"{header}\n\n{body}"


def _start_keyboard(bot_username: str):
    """Screenshot jaisa 3-row layout: Add me | Help | Updates+Support (side by side)."""
    return InlineKeyboardMarkup(
        [
            [
                _btn(
                    f"🎧+ {smallcaps_title('add me to your chat')} 🎧+",
                    url=f"https://t.me/{bot_username}?startgroup=true",
                    style="success",
                )
            ],
            [_btn(f"❓ {smallcaps_title('help and command')}", callback_data="help_menu", style="primary")],
            [
                _btn(f"📢 {smallcaps_title('updates')}", url=config.CHANNEL_URL, style="primary"),
                _btn(f"🛠 {smallcaps_title('support')}", url=config.SUPPORT_URL, style="primary"),
            ],
        ]
    )


def _help_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                _btn(smallcaps("admin"), callback_data="help_admin", style="primary"),
                _btn(smallcaps("auth"), callback_data="help_auth", style="primary"),
                _btn(smallcaps("b-cast"), callback_data="help_bcast", style="primary"),
            ],
            [
                _btn(smallcaps("play"), callback_data="help_play", style="primary"),
                _btn(smallcaps("sudo"), callback_data="help_sudo", style="primary"),
                _btn(smallcaps("restrict"), callback_data="help_restrict", style="primary"),
            ],
            [
                _btn(smallcaps("thumbnail"), callback_data="help_thumbnail", style="primary"),
                _btn(smallcaps("start"), callback_data="help_start", style="primary"),
                _btn(smallcaps("autoplay"), callback_data="help_autoplay", style="primary"),
            ],
            [_btn(smallcaps("back"), callback_data="back_to_start", style="danger")],
        ]
    )


def _help_category_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                _btn(smallcaps("back"), callback_data="help_menu", style="primary"),
                _btn(smallcaps("home"), callback_data="back_to_start", style="danger"),
            ]
        ]
    )


async def _send_welcome(chat_id: int, text: str, reply_markup):
    return await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)


async def _edit_body(cq_message, text: str, reply_markup):
    """Callback pe message edit karta hai — chahe woh media caption ho ya plain text."""
    if cq_message.photo or cq_message.video or cq_message.animation:
        await cq_message.edit_caption(text, reply_markup=reply_markup)
    else:
        await cq_message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)


HELP_TEXT = (
    blockquote(f"💮 {smallcaps_title('dive into all command categories below')}")
    + "\n\n"
    + blockquote(
        f"• {smallcaps_title('get guidance & support assistance')}\n"
        f"• {smallcaps_title('use commands with this syntax')} ➜ /"
    )
)

# ---------------------------------------------------------------------------
# Help category pages — har button click par upar wala message isi text se
# edit ho jaata hai (_help_category_keyboard: Back + Home).
# NOTE: In mein se zyadatar commands abhi sirf REFERENCE ke liye hain — inka
# asli backend (queue jump/move/shuffle, broadcast modes, channel play, sudo
# list, blacklist, logs/maintenance, stats, /eval /sh, etc.) is update mein
# implement nahi kiya gaya hai (bahut bada scope hai — agla batch bata do to
# priority ke hisaab se banata hoon). `/eval` aur `/sh` jaise arbitrary
# code/shell-execution commands maine jaanbojh kar implement nahi kiye — yeh
# bot ke liye ek security backdoor ban jaate hain, chahe "owner only" hi kyun
# na ho.
# ---------------------------------------------------------------------------
HELP_CATEGORY_TEXT = {
    "help_admin": (
        "⊚ ᴀᴅᴍɪɴ ᴄᴏᴍᴍᴀɴᴅs :\n\n"
        "💮 ᴊᴜsᴛ ᴀᴅᴅ ᴄ ᴀs ᴀ ᴘʀᴇғɪx ᴛᴏ ᴛʜᴇ ᴄᴏᴍᴍᴀɴᴅs ᴛᴏ ᴄᴏɴᴛʀᴏʟ ᴄʜᴀɴɴᴇʟ sᴛʀᴇᴀᴍɪɴɢ.\n\n"
        "➻ `/pause` : ʜᴀʟᴛ ᴛʜᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴀᴄᴛɪᴠᴇ sᴛʀᴇᴀᴍ.\n"
        "➻ `/resume` : ʀᴇsᴛᴀʀᴛ ᴛʜᴇ ᴘᴀᴜsᴇᴅ ᴘʟᴀʏʙᴀᴄᴋ.\n"
        "➻ `/skip` : ᴘʟᴀʏ ᴛʜᴇ ɴᴇxᴛ ᴛʀᴀᴄᴋ ɪɴ ᴛʜᴇ ǫᴜᴇᴜᴇ.\n"
        "➻ `/askip` : ᴘʟᴀʏ ᴛʜᴇ ɴᴇxᴛ ᴛʀᴀᴄᴋ ɪɴ ᴛʜᴇ ᴀᴜᴛᴏᴘʟᴀʏ ǫᴜᴇᴜᴇ.\n"
        "➻ `/stop` : ᴄᴇᴀsᴇ ᴛʜᴇ sᴛʀᴇᴀᴍ ᴀɴᴅ ᴇᴍᴘᴛʏ ǫᴜᴇᴜᴇ.\n"
        "➻ `/mute` : ᴍᴜᴛᴇ ᴘʟᴀʏʙᴀᴄᴋ ɪɴ ᴠɪᴅᴇᴏᴄʜᴀᴛ.\n"
        "➻ `/unmute` : ᴜɴᴍᴜᴛᴇ ᴘʟᴀʏʙᴀᴄᴋ ɪɴ ᴠɪᴅᴇᴏᴄʜᴀᴛ.\n"
        "➻ `/queue` : ᴠɪᴇᴡ ᴛʜᴇ ʟɪsᴛ ᴏғ ᴜᴘᴄᴏᴍɪɴɢ ᴛʀᴀᴄᴋs.\n"
        "➻ `/jump` : ᴊᴜᴍᴘ ᴛᴏ ᴀ ɢɪᴠᴇɴ ᴛɪᴍᴇ ɪɴ ᴛʀᴀᴄᴋ.\n"
        "➻ `/move` : ᴍᴏᴠᴇ ᴀ ǫᴜᴇᴜᴇᴅ ᴛʀᴀᴄᴋ ᴛᴏ ᴀɴᴏᴛʜᴇʀ ᴘᴏsɪᴛɪᴏɴ.\n"
        "➻ `/clear` : ᴄʟᴇᴀʀ ᴀʟʟ sᴏɴɢs ғʀᴏᴍ ǫᴜᴇᴜᴇ.\n"
        "➻ `/remove` : ʀᴇᴍᴏᴠᴇ ᴀ sᴘᴇᴄɪғɪᴄ ᴛʀᴀᴄᴋ ғʀᴏᴍ ǫᴜᴇᴜᴇ.\n"
        "➻ `/loop [1-10]` : ʀᴇᴘᴇᴀᴛ ᴛʜᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴘʟᴀʏɪɴɢ sᴏɴɢ.\n"
        "➻ `/shuffle` : ʀᴀɴᴅᴏᴍɪᴢᴇ ᴛʜᴇ ǫᴜᴇᴜᴇᴅ ᴘʟᴀʏʟɪsᴛ.\n"
        "➻ `/seek [secs]` : ғᴏʀᴡᴀʀᴅ sᴇᴇᴋ ᴛᴏ ᴀ sᴘᴇᴄɪғɪᴄ ᴛɪᴍᴇ.\n"
        "➻ `/seekback [secs]` : ʀᴇᴠᴇʀsᴇ sᴇᴇᴋ ᴛᴏ ᴀ sᴘᴇᴄɪғɪᴄ ᴛɪᴍᴇ.\n"
        "➻ `/speed` : ᴍᴏᴅɪғʏ ᴘʟᴀʏʙᴀᴄᴋ sᴘᴇᴇᴅ (0.5x - 2.0x).\n"
        "➻ `/playmode` : ᴄᴏɴᴛʀᴏʟ ᴡʜᴏ ᴄᴀɴ ᴜsᴇ /ᴘʟᴀʏ ᴄᴍᴅ.\n"
        "➻ `/cmddelete` : ᴛᴏɢɢʟᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄ ᴅᴇʟᴇᴛɪᴏɴ ᴏғ ʙᴏᴛ ᴄᴏᴍᴍᴀɴᴅs."
    ),
    "help_auth": (
        "⊚ ᴀᴜᴛʜ ᴜsᴇʀs :\n\n"
        "💮 ᴀᴜᴛʜ ᴜsᴇʀs ᴀʀᴇ ɢʀᴀɴᴛᴇᴅ ᴀᴅᴍɪɴ ᴘʀɪᴠɪʟᴇɢᴇs ᴡɪᴛʜᴏᴜᴛ ʀᴇǫᴜɪʀɪɴɢ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴ sᴛᴀᴛᴜs ᴛᴏ ᴍᴀɴᴀɢᴇ sᴛʀᴇᴀᴍs.\n\n"
        "➻ `/auth [user]` : ᴀᴅᴅ ᴜsᴇʀ ᴛᴏ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ʟɪsᴛ.\n"
        "➻ `/unauth [user]` : ʀᴇᴍᴏᴠᴇ ᴜsᴇʀ ғʀᴏᴍ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ʟɪsᴛ.\n"
        "➻ `/authlist` : ᴅɪsᴘʟᴀʏ ᴀʟʟ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴍᴇᴍʙᴇʀs."
    ),
    "help_bcast": (
        "⊚ ʙʀᴏᴀᴅᴄᴀsᴛ ғᴇᴀᴛᴜʀᴇ [ᴏɴʟʏ ғᴏʀ sᴜᴅᴏᴇʀs] :\n\n"
        "`/broadcast [message or reply]` : sᴇɴᴅ ᴛᴏ ᴀʟʟ ᴄʜᴀᴛs.\n\n"
        "➻ ᴍᴏᴅᴇs :\n"
        "`-pin` : ᴩɪɴ ᴛʜᴇ ᴍᴇssᴀɢᴇ\n"
        "`-pinloud` : ᴩɪɴ + ɴᴏᴛɪғʏ ᴍᴇᴍʙᴇʀs\n"
        "`-user` : ʙʀᴏᴀᴅᴄᴀsᴛ ᴛᴏ sᴛᴀʀᴛᴇᴅ ᴜsᴇʀs\n"
        "`-assistant` : sᴇɴᴅ ᴠɪᴀ ᴀssɪsᴛᴀɴᴛ\n"
        "`-nobot` : ᴅᴏɴ'ᴛ sᴇɴᴅ ғʀᴏᴍ ʙᴏᴛ\n\n"
        "⊚ Example : `/broadcast -user -pin Testing broadcast`"
    ),
    "help_play": (
        "⊚ ᴘʟᴀʏ & ᴄʜᴀɴɴᴇʟ ᴘʟᴀʏ :\n\n"
        "➻ `/play` : sᴛᴀʀᴛ ᴀᴜᴅɪᴏ sᴛʀᴇᴀᴍɪɴɢ ɪɴ ᴄʜᴀᴛ.\n"
        "➻ `/vplay` : sᴛᴀʀᴛ ᴠɪᴅᴇᴏ sᴛʀᴇᴀᴍɪɴɢ ɪɴ ᴄʜᴀᴛ.\n"
        "➻ `/playforce` : ɪɴsᴛᴀɴᴛ ᴘʟᴀʏ (ᴏᴠᴇʀʀɪᴅᴇs ǫᴜᴇᴜᴇ).\n\n"
        "⊚ ᴄʜᴀɴɴᴇʟ ᴘʟᴀʏ :\n\n"
        "➻ `/cplay` : ᴀᴜᴅɪᴏ sᴛʀᴇᴀᴍ ɪɴ ʟɪɴᴋᴇᴅ ᴄʜᴀɴɴᴇʟ.\n"
        "➻ `/cvplay` : ᴠɪᴅᴇᴏ sᴛʀᴇᴀᴍ ɪɴ ʟɪɴᴋᴇᴅ ᴄʜᴀɴɴᴇʟ.\n"
        "➻ `/cplayforce` : ғᴏʀᴄᴇ sᴛʀᴇᴀᴍ ɪɴ ʟɪɴᴋᴇᴅ ᴄʜᴀɴɴᴇʟ.\n"
        "➻ `/channelplay [id]` : ᴄᴏɴɴᴇᴄᴛ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴛʜɪs ɢʀᴏᴜᴘ."
    ),
    "help_sudo": (
        "⊚ sʏsᴛᴇᴍ & sᴜᴅᴏ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ :\n\n"
        "💮 ᴍᴀɴᴀɢᴇ ʟᴏɢs, ᴀɴᴅ ᴀᴅᴍɪɴɪsᴛʀᴀᴛɪᴠᴇ ᴘʀɪᴠɪʟᴇɢᴇs ᴏғ ʏᴏᴜʀ ʙᴏᴛ.\n\n"
        "‣ ʙᴏᴛ ʟᴏɢs & ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ :\n\n"
        "➻ `/logs` : ɢᴇᴛ ʀᴇᴀʟ-ᴛɪᴍᴇ ʟᴏɢs ᴏғ ᴛʜᴇ ʙᴏᴛ.\n"
        "➻ `/logger` : [ᴇɴᴀʙʟᴇ/ᴅɪsᴀʙʟᴇ] ʙᴏᴛ ᴀᴄᴛɪᴠɪᴛʏ ʟᴏɢɢɪɴɢ.\n"
        "➻ `/maintenance` : [ᴇɴᴀʙʟᴇ/ᴅɪsᴀʙʟᴇ] ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴍᴏᴅᴇ.\n\n"
        "⊚ ᴀᴄᴛɪᴠᴇ sᴛʀᴇᴀᴍs :\n\n"
        "➻ `/activevoice` : ᴠɪᴇᴡ ᴏɴɢᴏɪɴɢ ᴀᴜᴅɪᴏ sᴇssɪᴏɴs.\n"
        "➻ `/ac` : ᴠɪᴇᴡ ᴀʟʟ ᴄᴏᴍʙɪɴᴇᴅ ᴀᴄᴛɪᴠᴇ sᴛʀᴇᴀᴍs.\n\n"
        "‣ sᴜᴅᴏ ʟɪsᴛ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ :\n\n"
        "➻ `/sudo` : ᴀᴅᴅ ᴀ ɴᴇᴡ sᴜᴅᴏ ᴜsᴇʀ ᴛᴏ ʙᴏᴛ.\n"
        "➻ `/rmsudo` : ʀᴇᴍᴏᴠᴇ ᴀ ᴜsᴇʀ ғʀᴏᴍ sᴜᴅᴏ ʟɪsᴛ.\n"
        "➻ `/sudolist` : ᴄʜᴇᴄᴋ ᴀʟʟ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ sᴜᴅᴏ ᴜsᴇʀs.\n\n"
        "❏ ᴛʜᴇsᴇ ᴄᴏᴍᴍᴀɴᴅs ᴀʀᴇ ʜɪɢʜʟʏ sᴇɴsɪᴛɪᴠᴇ, ᴜsᴇ ᴡɪᴛʜ ᴄᴀʀᴇ."
    ),
    "help_restrict": (
        "⊚ ʙʟᴀᴄᴋʟɪsᴛ & ɢʟᴏʙᴀʟ ʙᴀɴ :\n\n"
        "💮 ʀᴇsᴛʀɪᴄᴛ sᴘᴇᴄɪғɪᴄ ᴄʜᴀᴛs ᴏʀ ᴜsᴇʀs ғʀᴏᴍ ᴜsɪɴɢ ᴛʜᴇ ʙᴏᴛ ɢʟᴏʙᴀʟʟʏ.\n\n"
        "‣ ᴄʜᴀᴛ ʙʟᴀᴄᴋʟɪsᴛ :\n\n"
        "➻ `/blacklistchat [chat id]` : ʙᴀɴ ᴀ ᴄʜᴀᴛ ғʀᴏᴍ ᴜsɪɴɢ ʙᴏᴛ.\n"
        "➻ `/whitelistchat [chat id]` : ᴜɴʙᴀɴ ᴀ ʙʟᴀᴄᴋʟɪsᴛᴇᴅ ᴄʜᴀᴛ.\n"
        "➻ `/blacklistedchat` : sʜᴏᴡ ᴀʟʟ ʙʟᴀᴄᴋʟɪsᴛᴇᴅ ɢʀᴏᴜᴘs.\n\n"
        "‣ ᴜsᴇʀ ʙʟᴏᴄᴋ :\n\n"
        "➻ `/block [username/id]` : sᴛᴏᴘ ᴜsᴇʀ ғʀᴏᴍ ᴜsɪɴɢ ʙᴏᴛ.\n"
        "➻ `/unblock [username/id]` : ᴀʟʟᴏᴡ ᴛʜᴇ ʙʟᴏᴄᴋᴇᴅ ᴜsᴇʀ.\n"
        "➻ `/blockedusers` : ᴠɪᴇᴡ ᴛʜᴇ ʟɪsᴛ ᴏғ ᴀʟʟ ʙʟᴏᴄᴋᴇᴅ ᴜsᴇʀs."
    ),
    "help_thumbnail": (
        "⊚ ᴛʜᴜᴍʙɴᴀɪʟ sᴇᴛᴛɪɴɢs :\n\n"
        "➻ `/thumb` : ᴍᴀɴᴀɢᴇ sᴛʀᴇᴀᴍ ᴠɪsᴜᴀʟ sᴇᴛᴛɪɴɢs.\n\n"
        "⊚ ᴡʜᴀᴛ ɪs ᴛʜᴜᴍʙɴᴀɪʟ ᴛᴏɢɢʟᴇ?\n\n"
        "💮 ᴛᴏɢɢʟᴇ ᴀʟʙᴜᴍ ᴀʀᴛ ᴅɪsᴘʟᴀʏ. ᴅɪsᴀʙʟɪɴɢ ɪᴛ ʀᴇᴅᴜᴄᴇs ᴅᴀᴛᴀ ʟᴏᴀᴅ ᴀɴᴅ ᴍᴀɪɴᴛᴀɪɴs ᴀ ᴍɪɴɪᴍᴀʟɪsᴛ ᴄʜᴀᴛ ʟᴏᴏᴋ.\n\n"
        "๏ ᴘᴀɴᴇʟ ᴏᴘᴛɪᴏɴs :\n\n"
        "• ᴇɴᴀʙʟᴇ : ᴅɪsᴘʟᴀʏ ᴀʀᴛᴡᴏʀᴋ ᴡɪᴛʜ sᴛʀᴇᴀᴍ ɪɴғᴏ.\n"
        "• ᴅɪsᴀʙʟᴇ : sᴇɴᴅ ᴘᴜʀᴇ ᴛᴇxᴛ-ʙᴀsᴇᴅ ᴜᴘᴅᴀᴛᴇs.\n"
        "• ᴄʟᴏsᴇ : ᴅɪsᴍɪss ᴛʜᴇ ᴄᴏɴғɪɢᴜʀᴀᴛɪᴏɴ.\n\n"
        "๏ ɴᴏᴛᴇ : sᴇᴛᴛɪɴɢs ᴀʀᴇ sᴀᴠᴇᴅ ɪɴᴅɪᴠɪᴅᴜᴀʟʟʏ ᴘᴇʀ ᴄʜᴀᴛ."
    ),
    "help_start": (
        "⊚ ʙᴀsɪᴄ ᴄᴏᴍᴍᴀɴᴅs :\n\n"
        "➻ `/start` : ɪɴɪᴛɪᴀʟɪᴢᴇ ᴛʜᴇ ᴍᴜsɪᴄ sᴇʀᴠɪᴄᴇ.\n"
        "➻ `/help` : ᴀᴄᴄᴇss ᴛʜᴇ ᴄᴏᴍᴍᴀɴᴅ ɢᴜɪᴅᴇʟɪɴᴇs.\n"
        "➻ `/ping` : ᴍᴇᴀsᴜʀᴇ sʏsᴛᴇᴍ ʟᴀᴛᴇɴᴄʏ ᴀɴᴅ ᴘɪɴɢ.\n"
        "➻ `/position` : sʜᴏᴡ ᴄᴜʀʀᴇɴᴛ ᴛʀᴀᴄᴋ's ᴛɪᴍᴇsᴛᴀᴍᴘ.\n"
        "➻ `/reload` : ʀᴇғʀᴇsʜ ᴀᴅᴍɪɴ ᴅᴀᴛᴀ ᴄᴀᴄʜᴇ.\n"
        "➻ `/settings` : ᴀᴅᴊᴜsᴛ ᴄᴏɴғɪɢᴜʀᴀᴛɪᴏɴ ᴏᴘᴛɪᴏɴs.\n"
        "➻ `/lang` : sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʀᴇғᴇʀʀᴇᴅ ʟᴀɴɢᴜᴀɢᴇ.\n"
        "➻ `/bug` : ʀᴇᴘᴏʀᴛ ᴀɴ ɪssᴜᴇ ᴏʀ ᴘʀᴏʙʟᴇᴍ.\n"
        "➻ `/json` : sʜᴏᴡ ᴍᴇssᴀɢᴇ ᴊsᴏɴ sᴛʀᴜᴄᴛᴜʀᴇ.\n"
        "➻ `/sudolist` : ᴠɪᴇᴡ ᴛʜᴇ ʟɪsᴛ ᴏғ sᴜᴅᴏ ᴀᴅᴍɪɴs.\n"
        "➻ `/stats` : ᴠɪᴇᴡ ᴄᴏᴍᴘʀᴇʜᴇɴsɪᴠᴇ ʙᴏᴛ ᴍᴇᴛʀɪᴄs."
    ),
    "help_autoplay": (
        "⊚ ᴀᴜᴛᴏᴘʟᴀʏ ᴄᴏɴᴛʀᴏʟ :\n\n"
        "➻ `/autoplayon` `/autoplayoff` : ᴛᴏɢɢʟᴇ ᴛʜᴇ ᴀᴜᴛᴏᴘʟᴀʏ sᴇᴛᴛɪɴɢs.\n"
        "➻ `/askip` : ᴘʟᴀʏ ᴛʜᴇ ɴᴇxᴛ ᴛʀᴀᴄᴋ ɪɴ ᴛʜᴇ ᴀᴜᴛᴏᴘʟᴀʏ ǫᴜᴇᴜᴇ.\n\n"
        "⊚ ᴡʜᴀᴛ ɪs ᴀᴜᴛᴏᴘʟᴀʏ?\n\n"
        "💮 ᴀᴜᴛᴏᴘʟᴀʏ sᴇᴀᴍʟᴇssʟʏ ᴀᴅᴅs ʀᴇʟᴀᴛᴇᴅ ᴛʀᴀᴄᴋs ᴛᴏ ᴛʜᴇ ǫᴜᴇᴜᴇ ʙᴀsᴇᴅ ᴏɴ ʏᴏᴜʀ ᴄᴜʀʀᴇɴᴛ ᴘʟᴀʏʟɪsᴛ ғᴏʀ ɴᴏɴ-sᴛᴏᴘ ᴍᴜsɪᴄ.\n\n"
        "๏ ᴘᴀɴᴇʟ ᴏᴘᴛɪᴏɴs :\n\n"
        "• ᴇɴᴀʙʟᴇ : ᴀᴄᴛɪᴠᴀᴛᴇ sᴍᴀʀᴛ ʀᴇᴄᴏᴍᴍᴇɴᴅᴀᴛɪᴏɴs.\n"
        "• ᴅɪsᴀʙʟᴇ : ᴅᴇᴀᴄᴛɪᴠᴀᴛᴇ sᴍᴀʀᴛ ʀᴇᴄᴏᴍᴍᴇɴᴅᴀᴛɪᴏɴs.\n"
        "• ᴄʟᴏsᴇ : ᴇxɪᴛ ᴛʜᴇ ᴄᴏɴᴛʀᴏʟ ᴘᴀɴᴇʟ.\n\n"
        "๏ ɴᴏᴛᴇ : ʀᴇǫᴜɪʀᴇs ᴀᴛ ʟᴇᴀsᴛ ᴏɴᴇ ᴀᴄᴛɪᴠᴇ sᴏɴɢ ᴛᴏ ғᴜɴᴄᴛɪᴏɴ."
    ),
}


def _welcome_text(user_name: str, user_id: int, bot_name: str, bot_username: str) -> str:
    user_tag = f"[{smallcaps_title(user_name)}](tg://user?id={user_id})"
    bot_tag = f"[{fancy_italic(bot_name)}](https://t.me/{bot_username})"

    greeting = blockquote(f"💐 {smallcaps_title('greetings')} {user_tag} 🥀")
    details = expandable_blockquote(
        f"💮 {smallcaps_title('you are using')} {bot_tag} : "
        f"{smallcaps_title('the ultimate destination for high quality streaming')}.\n\n"
        f"● {smallcaps_title('build')} : V2.0 Stable.\n"
        f"● {smallcaps_title('output')} : Hi-Res Audio.\n"
        f"● {smallcaps_title('latency')} : Zero Delay.\n\n"
        f"🎋 {smallcaps_title('click help to see all available commands')}."
    )
    return f"{greeting}\n\n{details}"


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await db.add_user(message.from_user.id)
    me = await bot.get_me()

    text = _welcome_text(message.from_user.first_name, message.from_user.id, me.first_name, me.username)

    if message.chat.type != "private":
        await db.add_chat(message.chat.id)

    await _send_welcome(message.chat.id, text, _start_keyboard(me.username))

    # Owner ko batao ki kisne bot use kiya (private chat mein)
    if message.chat.type == "private" and config.OWNER_ID and message.from_user.id != config.OWNER_ID:
        try:
            await bot.send_message(
                config.OWNER_ID,
                f"👤 {smallcaps_title('bot used by')}:\n"
                f"{smallcaps_title('name')}: {message.from_user.first_name}\n"
                f"{smallcaps_title('username')}: @{message.from_user.username}\n"
                f"{smallcaps_title('id')}: `{message.from_user.id}`",
            )
        except Exception as e:
            LOGGER.warning(f"Owner notify fail: {e}")


@bot.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply_text(HELP_TEXT, reply_markup=_help_keyboard(), disable_web_page_preview=True)


@bot.on_callback_query(filters.regex("^help_menu$"))
async def help_menu_cb(client, cq: CallbackQuery):
    await cq.answer()
    await _edit_body(cq.message, HELP_TEXT, _help_keyboard())


@bot.on_callback_query(filters.regex("^help_(admin|auth|bcast|play|sudo|restrict|thumbnail|start|autoplay)$"))
async def help_category_cb(client, cq: CallbackQuery):
    await cq.answer()
    text = HELP_CATEGORY_TEXT.get(cq.data)
    if not text:
        return
    await _edit_body(cq.message, text, _help_category_keyboard())


@bot.on_callback_query(filters.regex("^back_to_start$"))
async def back_to_start_cb(client, cq: CallbackQuery):
    await cq.answer()
    me = await bot.get_me()
    text = _welcome_text(cq.from_user.first_name, cq.from_user.id, me.first_name, me.username)
    await _edit_body(cq.message, text, _start_keyboard(me.username))


# ---------------------------------------------------------------------------
# Bot ko group mein add kiya jaana
# ---------------------------------------------------------------------------
@bot.on_message(filters.new_chat_members)
async def added_to_group(client, message: Message):
    me = await bot.get_me()
    if not any(u.id == me.id for u in message.new_chat_members):
        return

    await db.add_chat(message.chat.id)
    adder = message.from_user.first_name if message.from_user else "there"
    bot_tag = f"[{fancy_italic(me.first_name)}](https://t.me/{me.username})"

    await message.reply_text(
        f"❖ {smallcaps_title('hey')} {adder}..!! 🥀\n"
        f"» {smallcaps_title('thanks for adding')} {bot_tag}!\n\n"
        f"» {bot_tag} {smallcaps_title('can now play songs in this chat')}.\n\n"
        f"⌾ {smallcaps_title('play music')} : /play\n"
        f"⌾ {smallcaps_title('help & cmds')} : /help\n\n"
        f"{FOOTER_LINE}",
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /play — private chat mein bheja gaya to bata do ki yeh group command hai
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("play") & filters.private)
async def play_private_command(client, message: Message):
    me = await bot.get_me()
    await message.reply_text(
        f"❌ {smallcaps_title('this is a group command')}!\n\n"
        f"» {smallcaps_title('add me to your group, start the voice chat there and turn the vc live on, then use')} "
        f"`/play` {smallcaps_title('in the group')}.",
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /play — sabke liye khula hai
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("play") & filters.group)
async def play_command(client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"❌ {smallcaps_title('give me a song name too')}!\nExample: `/play Shape of You`"
        )

    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
    requester = message.from_user.first_name if message.from_user else "Someone"
    requester_id = message.from_user.id if message.from_user else None

    # User ka /play command wala msg turant delete — taaki kisi aur group
    # member ko pata na chale isne kaunsa gaana request kiya tha.
    try:
        await message.delete()
    except Exception as e:
        LOGGER.warning(f"Play command msg delete fail: {e}")

    # Pehle confirm karo ki assistant account is group mein hai — nahi hai to
    # VC join hi nahi ho paayega. Khud join karwane ki koshish yahin hoti hai.
    joined, reason = await ensure_assistant_in_chat(chat_id)
    if not joined:
        if reason == "flood_wait":
            return await bot.send_message(chat_id, ASSISTANT_FLOOD_TEXT)
        return await bot.send_message(chat_id, ASSISTANT_NOT_JOINED_TEXT)

    # Pehle sirf ek single emoji jaata hai...
    status = await bot.send_message(chat_id, random_processing_emoji())
    # ...uske turant baad, agar owner ne /processingon kiya hai, to usi emoji
    # ke niche ek random text (bot ki smallcaps style mein) jud jaata hai.
    if botstate.is_processing_text_enabled():
        try:
            await status.edit_text(processing_caption(status.text))
        except Exception as e:
            LOGGER.warning(f"Processing caption edit fail: {e}")

    track = await search_track(query)
    if not track:
        return await status.edit_text(f"❌ {smallcaps_title('could not find anything, try a different name')}.")

    try:
        stream_url = await get_stream_url(track["id"])
    except Exception as e:
        LOGGER.error(f"Stream URL error: {e}")
        return await status.edit_text(
            f"❌ {smallcaps_title('could not load this track, try again shortly or send another song')}."
        )

    track["stream_url"] = stream_url
    track["requested_by"] = requester
    track["requested_by_id"] = requester_id

    # Agar pehle se kuch baj raha hai -> queue mein daal do
    if q.is_playing(chat_id):
        position = q.push(chat_id, track)
        await status.delete()
        queue_title = track["title"].split("|")
        queue_main = smallcaps_title(esc(queue_title[0].strip()))
        queue_lines = [f"╰┈➤ {smallcaps_title(esc(x.strip()))}" for x in queue_title[1:] if x.strip()]
        requester_tag = f"[{smallcaps_title(requester)}](tg://user?id={requester_id})"

        header = blockquote(f"❖ {smallcaps_title('queued to play')}..!! ✦")
        body_lines = [f"» 『 #{position} • {queue_main} 』"] + queue_lines
        body = expandable_blockquote("\n".join(body_lines))
        meta = expandable_blockquote(
            f"⌾ {smallcaps_title('duration')} : {esc(track['duration'])}\n"
            f"⌾ {smallcaps_title('requested by')} : {requester_tag}"
        )
        queue_text = f"{header}\n\n{body}\n\n{meta}\n\n{FOOTER_LINE}"

        await bot.send_message(
            chat_id,
            queue_text,
            reply_markup=InlineKeyboardMarkup([[
                _btn(f"🎧+ {smallcaps_title('add me to your chat')} 🎧+",
                     url=f"https://t.me/{(await bot.get_me()).username}?startgroup=true", style="success")
            ]]),
            disable_web_page_preview=True,
        )
        return

    await status.delete()
    await _start_playing(chat_id, track)


# ---------------------------------------------------------------------------
# /vplay — audio ke saath VIDEO bhi VC mein stream hota hai (jaise screen
# share). Owner ke diye hue video-download API se link nikalta hai.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("vplay") & filters.private)
async def vplay_private_command(client, message: Message):
    me = await bot.get_me()
    await message.reply_text(
        f"❌ {smallcaps_title('this is a group command')}!\n\n"
        f"» {smallcaps_title('add me to your group, start the voice chat there and turn the vc live on, then use')} "
        f"`/vplay` {smallcaps_title('in the group')}.",
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


@bot.on_message(filters.command("vplay") & filters.group)
async def vplay_command(client, message: Message):
    is_owner = bool(config.OWNER_ID and message.from_user and message.from_user.id == config.OWNER_ID)
    if not botstate.is_vplay_enabled() and not is_owner:
        return await message.reply_text(
            f"🔴 {smallcaps_title('vplay is currently turned off by the owner')}."
        )

    if not _VIDEO_SUPPORTED:
        return await message.reply_text(
            f"❌ {smallcaps_title('video streaming is not available on this deployment')} "
            f"({smallcaps_title('installed py-tgcalls version does not expose AudioVideoPiped')})."
        )

    if len(message.command) < 2:
        return await message.reply_text(
            f"❌ {smallcaps_title('give me a song name or a youtube link')}!\nExample: `/vplay Shape of You`"
        )

    query = message.text.split(None, 1)[1].strip()
    chat_id = message.chat.id
    requester = message.from_user.first_name if message.from_user else "Someone"
    requester_id = message.from_user.id if message.from_user else None

    # User ka /vplay command wala msg turant delete — /play jaisa hi behavior.
    try:
        await message.delete()
    except Exception as e:
        LOGGER.warning(f"Vplay command msg delete fail: {e}")

    joined, reason = await ensure_assistant_in_chat(chat_id)
    if not joined:
        if reason == "flood_wait":
            return await bot.send_message(chat_id, ASSISTANT_FLOOD_TEXT)
        return await bot.send_message(chat_id, ASSISTANT_NOT_JOINED_TEXT)

    status = await bot.send_message(
        chat_id,
        f"🎬 {smallcaps_title('fetching the video — this can take a few minutes, please wait')}...",
    )

    # Seedha YouTube link diya ho to ID nikalo, warna text se search karo
    if re.match(r"^https?://", query):
        video_id = _extract_youtube_id(query)
        if not video_id:
            return await status.edit_text(
                f"❌ {smallcaps_title('could not recognize that youtube link')}.", reply_markup=None
            )
        track = await search_track(query)  # best-effort thumbnail/title — link ho to shayad kuch na mile
        if not track:
            track = {"id": video_id, "title": query, "duration": "?", "thumbnail": None}
    else:
        track = await search_track(query)
        if not track:
            return await status.edit_text(
                f"❌ {smallcaps_title('could not find anything, try a different name')}.", reply_markup=None
            )
        video_id = track["id"]

    try:
        stream_url = await get_video_stream_url(video_id)
    except Exception as e:
        LOGGER.error(f"Video stream URL error: {e}")
        return await status.edit_text(
            f"❌ {smallcaps_title('could not load this video, try again shortly or send another link')}.",
            reply_markup=None,
        )

    track["stream_url"] = stream_url
    track["is_video"] = True
    track["requested_by"] = requester
    track["requested_by_id"] = requester_id

    # Agar pehle se kuch baj raha hai -> queue mein daal do (jaise /play)
    if q.is_playing(chat_id):
        position = q.push(chat_id, track)
        await status.delete()
        queue_title = str(track["title"]).split("|")
        queue_main = smallcaps_title(esc(queue_title[0].strip()))
        queue_lines = [f"╰┈➤ {smallcaps_title(esc(x.strip()))}" for x in queue_title[1:] if x.strip()]
        requester_tag = f"[{smallcaps_title(requester)}](tg://user?id={requester_id})"

        header = blockquote(f"❖ 🎬 {smallcaps_title('queued to play')}..!! ✦")
        body_lines = [f"» 『 #{position} • {queue_main} 』"] + queue_lines
        body = expandable_blockquote("\n".join(body_lines))
        meta = expandable_blockquote(
            f"⌾ {smallcaps_title('duration')} : {esc(track.get('duration', '?'))}\n"
            f"⌾ {smallcaps_title('requested by')} : {requester_tag}"
        )
        queue_text = f"{header}\n\n{body}\n\n{meta}\n\n{FOOTER_LINE}"

        await bot.send_message(
            chat_id,
            queue_text,
            reply_markup=InlineKeyboardMarkup([[
                _btn(f"🎧+ {smallcaps_title('add me to your chat')} 🎧+",
                     url=f"https://t.me/{(await bot.get_me()).username}?startgroup=true", style="success")
            ]]),
            disable_web_page_preview=True,
        )
        return

    await status.delete()
    await _start_playing(chat_id, track)


def _extract_youtube_id(text: str):
    """YouTube link (youtu.be/ID, watch?v=ID, shorts/ID) se 11-char video ID nikalta hai."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([0-9A-Za-z_-]{11})", text)
    return m.group(1) if m else None


_RECONNECT_FLAGS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def _make_stream(track: dict, ffmpeg_seek: int = None):
    """Track ke `is_video` flag ke hisaab se audio-only ya audio+video input
    stream banata hai (jaise VC mein screen-share hoti hai). ffmpeg_seek
    diya ho to -10/+10 seek ke liye stream ke beech se shuru hota hai.

    Lag/buffering kam karne ke liye:
    - HTTP reconnect flags hamesha lagte hain (source connection beech mein
      thodi der ke liye toot jaaye to ffmpeg khud reconnect kar leta hai —
      ye "atak atak ke chalna" ka sabse aam reason hota hai).
    - Video ke liye Medium quality use hoti hai (Max/High ke bajaye) taaki
      kam CPU/bandwidth lage — is se stutter kaafi kam ho jaata hai.
    """
    ffmpeg_params = _RECONNECT_FLAGS
    if ffmpeg_seek is not None:
        ffmpeg_params += f" -ss {int(ffmpeg_seek)}"
    kwargs = {"additional_ffmpeg_parameters": ffmpeg_params}

    if track.get("is_video") and _VIDEO_SUPPORTED:
        if _QUALITY_SUPPORTED:
            kwargs["video_parameters"] = MediumQualityVideo()
            kwargs["audio_parameters"] = HighQualityAudio()
        return AudioVideoPiped(track["stream_url"], **kwargs)
    return AudioPiped(track["stream_url"], **kwargs)


async def _start_playing(chat_id: int, track: dict):
    """VC join/change karke track play karta hai aur Now Playing card bhejta hai.
    (User ka original /play msg turant delete ho jaata hai, isliye yahan
    reply ke bajaye seedha chat_id par bheja jaata hai.)"""
    try:
        try:
            await call_py.join_group_call(chat_id, _make_stream(track))
        except NoActiveGroupCall:
            return await bot.send_message(
                chat_id,
                f"❌ **{smallcaps_title('voice chat is not active')}!**\n\n"
                f"{smallcaps_title('start a voice chat in the group first')}:\n"
                "Group Settings → Voice Chat → Start Voice Chat\n\n"
                f"{smallcaps_title('then send')} `/play` {smallcaps_title('again')}.",
            )
        except Exception as e:
            if _is_peer_error(e):
                await _refresh_assistant_peers()
            try:
                await call_py.change_stream(chat_id, _make_stream(track))
            except Exception as e2:
                LOGGER.error(f"Play error: {e2}")
                return await bot.send_message(
                    chat_id,
                    f"❌ **{smallcaps_title('could not play it')}**\n\n"
                    f"{smallcaps_title('check whether the voice chat is active, then try again')}.",
                )

        q.set_now_playing(chat_id, track)
        q.remember_played(chat_id, track.get("id"))
        await _send_now_playing(chat_id, track)

    except Exception as e:
        LOGGER.error(f"_start_playing fatal error: {e}")
        await bot.send_message(chat_id, f"❌ {smallcaps_title('something went wrong, try again')}.")


def _now_playing_caption(track: dict) -> str:
    parts = [x.strip() for x in str(track.get("title", "")).split("|") if x.strip()]
    main_title = smallcaps_title(esc(parts[0])) if parts else smallcaps_title("unknown")
    song_lines = [f"» 『 {main_title} 』"]
    for part in parts[1:]:
        song_lines.append(f"╰┈➤ {smallcaps_title(esc(part))}")

    requester = track.get("requested_by", "Unknown")
    requester_id = track.get("requested_by_id")
    if requester_id:
        requester_tag = f"[{smallcaps_title(requester)}](tg://user?id={requester_id})"
    else:
        requester_tag = smallcaps_title(requester)

    header = blockquote(f"❖ {'🎬 ' if track.get('is_video') else ''}{smallcaps_title('now playing')}..!! ✦")
    body = expandable_blockquote("\n".join(song_lines))
    meta = expandable_blockquote(
        f"⌾ {smallcaps_title('duration')} : {esc(track.get('duration', '0:00'))}\n"
        f"⌾ {smallcaps_title('by')} : {requester_tag}"
    )
    return f"{header}\n\n{body}\n\n{meta}\n\n{FOOTER_LINE}"


async def _send_now_playing(chat_id: int, track: dict, message: Message = None, edit_message: Message = None):
    """
    Now Playing card bhejta hai. Agar `edit_message` diya gaya hai (jaise skip
    button se), to naya message bhejne/purana delete karne ke bajaye wahi
    message in-place update ho jaata hai — isse card kabhi "gayab" nahi hota,
    bas apne aap refresh ho jaata hai.
    """
    caption = _now_playing_caption(track)
    card = await generate_now_playing_card(track.get("thumbnail"), track["title"], track["duration"])
    total_sec = duration_to_seconds(track.get("duration"))
    markup = _controls_keyboard(0, total_sec)
    media = card or track.get("thumbnail")

    sent = None

    if edit_message is not None:
        try:
            if media:
                sent = await edit_message.edit_media(InputMediaPhoto(media, caption=caption), reply_markup=markup)
            else:
                sent = await edit_message.edit_text(caption, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            LOGGER.warning(f"Now playing in-place edit fail, naya message bhej rahe hain: {e}")

    if sent is None:
        try:
            if media:
                sent = await bot.send_photo(chat_id, media, caption=caption, reply_markup=markup)
            elif message is not None:
                sent = await message.reply_text(caption, reply_markup=markup, disable_web_page_preview=True)
            else:
                sent = await bot.send_message(chat_id, caption, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            LOGGER.warning(f"Now playing card send fail: {e}")
            if message is not None:
                sent = await message.reply_text(caption, reply_markup=markup, disable_web_page_preview=True)
            else:
                sent = await bot.send_message(chat_id, caption, reply_markup=markup, disable_web_page_preview=True)

    # 🎚️ Live progress bar shuru — gaana ke saath 00:00 se duration tak khud
    # aage badhta rahega (button ke andar), jaise screenshot mein dikha tha.
    if sent is not None:
        total_sec = duration_to_seconds(track.get("duration"))
        progress.start(chat_id, track["id"])
        progress.start_updater(
            chat_id, sent,
            lambda: _now_playing_caption(track),
            lambda el, tot: _controls_keyboard(el, tot),
            track["id"], total_sec,
        )

    return sent


# ---------------------------------------------------------------------------
# Stream khatam hone par queue se agla gaana
# ---------------------------------------------------------------------------
@call_py.on_stream_end()
async def on_stream_end(client, update):
    chat_id = update.chat_id
    await _play_next_or_autoplay(chat_id, silent_empty=True)


# ---------------------------------------------------------------------------
# /skip /pause /resume /stop — sirf group admin/owner
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    await _play_next_or_autoplay(message.chat.id, reply_message=message)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.pause_stream(message.chat.id)
        q.set_state(message.chat.id, "paused")
        progress.pause(message.chat.id)
        await message.reply_text(f"⏸ {smallcaps_title('paused')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.resume_stream(message.chat.id)
        q.set_state(message.chat.id, "playing")
        progress.resume(message.chat.id)
        await message.reply_text(f"▶️ {smallcaps_title('resumed')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
async def stop_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.leave_group_call(message.chat.id)
    except Exception:
        pass
    q.clear(message.chat.id)
    progress.clear(message.chat.id)
    await message.reply_text(f"⏹️ {smallcaps_title('left the voice chat')}.")


@bot.on_message(filters.command("askip") & filters.group)
async def askip_command(client, message: Message):
    """/skip ka hi alias — autoplay queue mein bhi agla track yahi se aata hai."""
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    await _play_next_or_autoplay(message.chat.id, reply_message=message)


@bot.on_message(filters.command("mute") & filters.group)
async def mute_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.mute_stream(message.chat.id)
        await message.reply_text(f"🔇 {smallcaps_title('muted')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command("unmute") & filters.group)
async def unmute_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.unmute_stream(message.chat.id)
        await message.reply_text(f"🔊 {smallcaps_title('unmuted')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_command(client, message: Message):
    chat_id = message.chat.id
    now_playing = q.get_now_playing(chat_id)
    upcoming = q.get_queue(chat_id)

    if not now_playing and not upcoming:
        return await message.reply_text(f"📭 {smallcaps_title('queue is empty right now')}.")

    lines = [f"❖ {smallcaps_title('queue')}"]
    if now_playing:
        lines.append(f"\n▶️ {smallcaps_title('now playing')} : {esc(now_playing['title'])}")
    if upcoming:
        lines.append(f"\n🔜 {smallcaps_title('up next')} :")
        for i, t in enumerate(upcoming[:15], start=1):
            lines.append(f"{i}. {esc(t['title'])} — {esc(t.get('duration', '?'))}")
        if len(upcoming) > 15:
            lines.append(f"… {smallcaps_title('and')} {len(upcoming) - 15} {smallcaps_title('more')}")
    await message.reply_text("\n".join(lines))


@bot.on_message(filters.command("clear") & filters.group)
async def clear_queue_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    q.clear_queue(message.chat.id)
    await message.reply_text(f"🧹 {smallcaps_title('queue cleared')} — {smallcaps_title('currently playing track is unaffected')}.")


@bot.on_message(filters.command("shuffle") & filters.group)
async def shuffle_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    if not q.get_queue(message.chat.id):
        return await message.reply_text(f"📭 {smallcaps_title('nothing queued to shuffle')}.")
    q.shuffle_queue(message.chat.id)
    await message.reply_text(f"🔀 {smallcaps_title('queue shuffled')}.")


@bot.on_message(filters.command("remove") & filters.group)
async def remove_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    if len(message.command) < 2 or not message.command[1].isdigit():
        return await message.reply_text(
            f"❌ {smallcaps_title('usage')} : `/remove [position]`\n{smallcaps_title('example')} : `/remove 2`"
        )
    removed = q.remove_at(message.chat.id, int(message.command[1]))
    if not removed:
        return await message.reply_text(f"❌ {smallcaps_title('no track at that position')}.")
    await message.reply_text(f"🗑 {smallcaps_title('removed')} : {esc(removed['title'])}")


@bot.on_message(filters.command("move") & filters.group)
async def move_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    if len(message.command) < 3 or not all(x.isdigit() for x in message.command[1:3]):
        return await message.reply_text(
            f"❌ {smallcaps_title('usage')} : `/move [from] [to]`\n{smallcaps_title('example')} : `/move 3 1`"
        )
    ok = q.move_track(message.chat.id, int(message.command[1]), int(message.command[2]))
    if not ok:
        return await message.reply_text(f"❌ {smallcaps_title('invalid positions')}.")
    await message.reply_text(f"↕️ {smallcaps_title('track moved')}.")


async def _do_seek(message: Message, delta: int):
    chat_id = message.chat.id
    track = q.get_now_playing(chat_id)
    if not track:
        return await message.reply_text(f"❌ {smallcaps_title('nothing is playing right now')}.")
    total_sec = duration_to_seconds(track.get("duration"))
    current = progress.elapsed(chat_id)
    new_pos = max(0, min(current + delta, max(total_sec - 1, 0)) if total_sec else current + delta)
    try:
        await call_py.change_stream(chat_id, _make_stream(track, ffmpeg_seek=new_pos))
        progress.seek_to(chat_id, new_pos)
        await message.reply_text(f"⏱ {smallcaps_title('seeked to')} {format_duration(int(new_pos))}.")
    except Exception as e:
        LOGGER.warning(f"Seek command fail: {e}")
        await message.reply_text(f"❌ {smallcaps_title('could not seek')}.")


@bot.on_message(filters.command("seek") & filters.group)
async def seek_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text(
            f"❌ {smallcaps_title('usage')} : `/seek [secs]`\n{smallcaps_title('example')} : `/seek 30`"
        )
    await _do_seek(message, int(message.command[1]))


@bot.on_message(filters.command("seekback") & filters.group)
async def seekback_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text(
            f"❌ {smallcaps_title('usage')} : `/seekback [secs]`\n{smallcaps_title('example')} : `/seekback 30`"
        )
    await _do_seek(message, -int(message.command[1]))


@bot.on_message(filters.command("jump") & filters.group)
async def jump_command(client, message: Message):
    """Absolute time par jump karta hai (jaise `/jump 1:30` ya `/jump 90`)."""
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    if len(message.command) < 2:
        return await message.reply_text(
            f"❌ {smallcaps_title('usage')} : `/jump [mm:ss or secs]`\n{smallcaps_title('example')} : `/jump 1:30`"
        )
    target_sec = duration_to_seconds(message.command[1]) if ":" in message.command[1] else (
        int(message.command[1]) if message.command[1].isdigit() else None
    )
    if target_sec is None:
        return await message.reply_text(f"❌ {smallcaps_title('invalid time format')}.")
    current = progress.elapsed(message.chat.id)
    await _do_seek(message, target_sec - current)


# ---------------------------------------------------------------------------
# Baaki commands jo help menu mein documented hain lekin abhi is build mein
# implement nahi hain — silent rehne ke bajaye usage example ke saath batate
# hain, aur permission ke hisaab se "not authorized" bhi dikhate hain.
# ---------------------------------------------------------------------------
PLACEHOLDER_COMMANDS = {
    "loop": ("`/loop [1-10]` — repeat the currently playing song.", "admin"),
    "speed": ("`/speed [0.5-2.0]` — change playback speed.", "admin"),
    "playmode": ("`/playmode [everyone|admins]` — control who can use /play.", "admin"),
    "cmddelete": ("`/cmddelete [on|off]` — auto-delete bot command messages.", "admin"),
    "auth": ("`/auth [user]` — grant a user admin-level bot control.", "owner"),
    "unauth": ("`/unauth [user]` — remove a user from the authorized list.", "owner"),
    "authlist": ("`/authlist` — show all authorized users.", "open"),
    "broadcast": ("`/broadcast [message]` — send a message to all chats.\n"
                  "Example : `/broadcast -user -pin Testing broadcast`", "owner"),
    "playforce": ("`/playforce [song]` — instantly play, overriding the queue.", "admin"),
    "vplayforce": ("`/vplayforce [song]` — instantly play video, overriding the queue.", "admin"),
    "cplay": ("`/cplay [song]` — play audio in the linked channel.", "admin"),
    "cvplay": ("`/cvplay [song]` — play video in the linked channel.", "admin"),
    "cplayforce": ("`/cplayforce [song]` — force-play in the linked channel.", "admin"),
    "channelplay": ("`/channelplay [id]` — connect a channel to this group.", "admin"),
    "logs": ("`/logs` — get real-time logs of the bot.", "owner"),
    "logger": ("`/logger [on|off]` — toggle bot activity logging.", "owner"),
    "maintenance": ("`/maintenance [on|off]` — toggle maintenance mode.", "owner"),
    "activevoice": ("`/activevoice` — view ongoing audio sessions.", "owner"),
    "ac": ("`/ac` — view all combined active streams.", "owner"),
    "sudo": ("`/sudo [user]` — add a sudo user.", "owner"),
    "rmsudo": ("`/rmsudo [user]` — remove a sudo user.", "owner"),
    "sudolist": ("`/sudolist` — view sudo admins.", "open"),
    "blacklistchat": ("`/blacklistchat [chat id]` — ban a chat from using the bot.", "owner"),
    "whitelistchat": ("`/whitelistchat [chat id]` — unban a blacklisted chat.", "owner"),
    "blacklistedchat": ("`/blacklistedchat` — show all blacklisted groups.", "owner"),
    "block": ("`/block [username/id]` — stop a user from using the bot.", "owner"),
    "unblock": ("`/unblock [username/id]` — allow a blocked user again.", "owner"),
    "blockedusers": ("`/blockedusers` — view all blocked users.", "open"),
    "thumb": ("`/thumb [enable|disable]` — toggle album-art thumbnails.", "admin"),
    "settings": ("`/settings` — adjust configuration options.", "open"),
    "lang": ("`/lang` — select your preferred language.", "open"),
    "bug": ("`/bug [description]` — report an issue or problem.", "open"),
    "json": ("`/json` — show this message's JSON structure.", "open"),
    "stats": ("`/stats` — view bot metrics.", "open"),
}


@bot.on_message(filters.command(list(PLACEHOLDER_COMMANDS.keys())))
async def placeholder_command(client, message: Message):
    cmd = message.command[0].lower()
    example, level = PLACEHOLDER_COMMANDS[cmd]
    if level == "owner":
        if not (config.OWNER_ID and message.from_user and message.from_user.id == config.OWNER_ID):
            return await message.reply_text(ADMIN_ONLY_TEXT)
    elif level == "admin":
        if message.chat.type == "private" or not await _is_group_admin(client, message.chat.id, message.from_user.id):
            return await message.reply_text(ADMIN_ONLY_TEXT)
    await message.reply_text(
        f"🚧 {smallcaps_title('this command is not available in this build yet')}.\n\n"
        f"{smallcaps_title('usage')} : {example}"
    )


@bot.on_message(filters.command(["eval", "sh"]))
async def disabled_dangerous_command(client, message: Message):
    await message.reply_text(
        f"⛔ {smallcaps_title('this command is permanently disabled')} — "
        f"{smallcaps_title('running arbitrary code or shell commands from telegram is a security risk, even for the owner')}."
    )


@bot.on_message(filters.command("autoplayon") & filters.group)
async def autoplayon_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    q.set_autoplay(message.chat.id, True)
    await message.reply_text(f"✅ {smallcaps_title('autoplay turned on')} — {smallcaps_title('a related song will play after each track')}.")


@bot.on_message(filters.command("autoplayoff") & filters.group)
async def autoplayoff_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)
    q.set_autoplay(message.chat.id, False)
    await message.reply_text(f"🔴 {smallcaps_title('autoplay turned off')}.")


# ---------------------------------------------------------------------------
# /reload — sirf group admin/owner. Check karta hai bot khud admin hai ya nahi.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("reload") & filters.group)
async def reload_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)

    me = await bot.get_me()
    try:
        bot_member = await client.get_chat_member(message.chat.id, me.id)
        is_bot_admin = bot_member.status in ADMIN_STATUSES
    except Exception as e:
        LOGGER.warning(f"Reload admin-check fail: {e}")
        is_bot_admin = False

    if is_bot_admin:
        await message.reply_text(f"✅ {smallcaps_title('reloaded successfully')}.")
    else:
        await message.reply_text(
            f"❌ {smallcaps_title('make me a group admin first, then run')} `/reload` {smallcaps_title('again')}."
        )


# ---------------------------------------------------------------------------
# /restrict /unrestrict — reply karke kisi user ko group mein bot ke commands
# se rok/chhod sakte ho. Group admin normal user ko restrict kar sakta hai,
# lekin ek group admin ko sirf BOT KA ASLI OWNER restrict kar sakta hai —
# aur bot owner ko koi bhi kabhi restrict nahi kar sakta.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("restrict") & filters.group)
async def restrict_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)

    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.reply_text(
            f"❌ {smallcaps_title('reply to a user')}'{smallcaps_title('s message with')} `/restrict` "
            f"{smallcaps_title('to restrict them')}."
        )

    target = message.reply_to_message.from_user
    is_caller_owner = bool(config.OWNER_ID and message.from_user.id == config.OWNER_ID)

    if target.is_self:
        return await message.reply_text(f"❌ {smallcaps_title('i cannot restrict myself')}.")
    if config.OWNER_ID and target.id == config.OWNER_ID:
        return await message.reply_text(f"❌ {smallcaps_title('my owner cannot be restricted')}.")
    if not is_caller_owner and await _is_group_admin(client, message.chat.id, target.id):
        return await message.reply_text(
            f"❌ {smallcaps_title('only my owner can restrict a group admin')}."
        )

    await db.restrict_user(message.chat.id, target.id)
    target_tag = f"[{esc(target.first_name)}](tg://user?id={target.id})"
    await message.reply_text(
        f"🚫 {target_tag} {smallcaps_title('has been restricted')}!\n"
        f"{smallcaps_title('this user can no longer use any of my commands in this group')}."
    )


@bot.on_message(filters.command("unrestrict") & filters.group)
async def unrestrict_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)

    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.reply_text(
            f"❌ {smallcaps_title('reply to a user')}'{smallcaps_title('s message with')} `/unrestrict` "
            f"{smallcaps_title('to remove their restriction')}."
        )

    target = message.reply_to_message.from_user
    await db.unrestrict_user(message.chat.id, target.id)
    target_tag = f"[{esc(target.first_name)}](tg://user?id={target.id})"
    await message.reply_text(
        f"✅ {target_tag} {smallcaps_title('has been unrestricted')} — "
        f"{smallcaps_title('they can use my commands again')}."
    )


# ---------------------------------------------------------------------------
# /activateapi1 /activateapi2 — owner-only, sirf dikhawe ke liye (koi asli
# API switch nahi hota).
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("activateapi1") & OWNER_FILTER)
async def activateapi1_command(client, message: Message):
    await message.reply_text("✅ Activated Youtube API")


@bot.on_message(filters.command("activateapi2") & OWNER_FILTER)
async def activateapi2_command(client, message: Message):
    await message.reply_text("✅ Activated Spotify mode")


async def _play_next_or_autoplay(chat_id: int, edit_message: Message = None, reply_message: Message = None, silent_empty: bool = False):
    """Queue mein agla track ho to wahi play karta hai. Nahi ho aur autoplay ON
    hai to khud ek related track dhoond kar play karta hai. Dono hi na milein
    to VC chhod deta hai. Skip button, /skip command, aur track khatam hone
    (on_stream_end) — teeno yahi function use karte hain."""
    next_track = q.pop_next(chat_id)

    if not next_track and q.get_autoplay(chat_id):
        current = q.get_now_playing(chat_id)
        seed_title = (current or {}).get("title", "").split("|")[0].strip() if current else None
        if seed_title:
            try:
                related = await search_related_track(seed_title, exclude_ids=q.recently_played(chat_id))
            except Exception as e:
                LOGGER.warning(f"Autoplay search fail: {e}")
                related = None
            if related:
                try:
                    related["stream_url"] = await get_stream_url(related["id"])
                    related["requested_by"] = smallcaps_title("autoplay")
                    related["requested_by_id"] = None
                    next_track = related
                except Exception as e:
                    LOGGER.warning(f"Autoplay stream fetch fail: {e}")

    if not next_track:
        q.set_now_playing(chat_id, None)
        progress.clear(chat_id)
        try:
            await call_py.leave_group_call(chat_id)
        except Exception:
            pass
        if not silent_empty:
            text = f"⏭ {smallcaps_title('queue is empty, left the voice chat')}."
            if edit_message is not None:
                try:
                    await edit_message.edit_reply_markup(None)
                except Exception:
                    pass
                await edit_message.reply_text(text)
            elif reply_message is not None:
                await reply_message.reply_text(text)
        return

    try:
        await call_py.change_stream(chat_id, _make_stream(next_track))
    except Exception as e:
        if _is_peer_error(e):
            await _refresh_assistant_peers()
        try:
            await call_py.join_group_call(chat_id, _make_stream(next_track))
        except Exception as e2:
            LOGGER.error(f"Play-next error: {e2}")
            if reply_message is not None:
                await reply_message.reply_text(f"❌ {smallcaps_title('could not continue playback, try again')}.")
            return

    q.set_now_playing(chat_id, next_track)
    q.remember_played(chat_id, next_track.get("id"))
    await _send_now_playing(chat_id, next_track, edit_message=edit_message)


# ---------------------------------------------------------------------------
# Inline buttons (Now Playing card ke neeche)
# ---------------------------------------------------------------------------
@bot.on_callback_query(filters.regex("^m_"))
async def controls_callback(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    action = cq.data

    # Playback/settings-changing actions — sirf requester, group admin ya owner.
    if action in ("m_resume", "m_pause", "m_skip", "m_stop", "m_seek_back", "m_seek_fwd",
                  "m_autoplay_toggle", "m_autoplay_skip"):
        if not await _can_control(client, chat_id, cq.from_user.id):
            return await cq.answer(NOT_YOUR_REQUEST_TEXT, show_alert=True)

    try:
        if action == "m_progress":
            await cq.answer()

        elif action == "m_boss":
            await cq.answer(REPO_ALERT_TEXT, show_alert=True)

        elif action == "m_resume":
            await call_py.resume_stream(chat_id)
            q.set_state(chat_id, "playing")
            progress.resume(chat_id)
            await cq.answer("▶️ Resumed")

        elif action == "m_pause":
            await call_py.pause_stream(chat_id)
            q.set_state(chat_id, "paused")
            progress.pause(chat_id)
            await cq.answer("⏸ Paused")

        elif action == "m_replay":
            track = q.get_now_playing(chat_id)
            if track:
                await call_py.change_stream(chat_id, _make_stream(track))
                progress.replay(chat_id)
                await cq.answer("🔁 Replaying")
            else:
                await cq.answer(smallcaps_title("nothing is playing right now"), show_alert=True)

        elif action in ("m_seek_back", "m_seek_fwd"):
            track = q.get_now_playing(chat_id)
            if not track:
                return await cq.answer(smallcaps_title("nothing is playing right now"), show_alert=True)
            total_sec = duration_to_seconds(track.get("duration"))
            current = progress.elapsed(chat_id)
            delta = -10 if action == "m_seek_back" else 10
            new_pos = max(0, min(current + delta, max(total_sec - 1, 0)) if total_sec else current + delta)
            try:
                await call_py.change_stream(chat_id, _make_stream(track, ffmpeg_seek=new_pos))
                progress.seek_to(chat_id, new_pos)
                await cq.answer("⏪ -10s" if action == "m_seek_back" else "⏩ +10s")
            except Exception as e:
                LOGGER.warning(f"Seek fail: {e}")
                await cq.answer(f"❌ {smallcaps_title('could not seek')}.", show_alert=True)

        elif action == "m_skip":
            await cq.answer("⏭ Skipping")
            await _play_next_or_autoplay(chat_id, edit_message=cq.message)

        elif action == "m_stop":
            await call_py.leave_group_call(chat_id)
            q.clear(chat_id)
            progress.clear(chat_id)
            await cq.answer("⏹ Stopped")
            try:
                await cq.message.edit_reply_markup(None)
            except Exception:
                pass
            await cq.message.reply_text(f"⏹️ {smallcaps_title('left the voice chat')}.")

        elif action == "m_settings":
            await cq.answer()
            await _edit_body(cq.message, _settings_text(chat_id), _settings_keyboard(chat_id))

        elif action == "m_autoplay_toggle":
            new_value = not q.get_autoplay(chat_id)
            q.set_autoplay(chat_id, new_value)
            await cq.answer(
                f"🔁 {smallcaps_title('autoplay')} {smallcaps_title('on') if new_value else smallcaps_title('off')}"
            )
            await _edit_body(cq.message, _settings_text(chat_id), _settings_keyboard(chat_id))

        elif action == "m_autoplay_skip":
            await cq.answer("⏭ Skipping")
            await _play_next_or_autoplay(chat_id, edit_message=cq.message)

        elif action == "m_settings_back":
            await cq.answer()
            track = q.get_now_playing(chat_id)
            if track:
                total_sec = duration_to_seconds(track.get("duration"))
                el = progress.elapsed(chat_id)
                await _edit_body(cq.message, _now_playing_caption(track), _controls_keyboard(el, total_sec))
            else:
                await cq.message.delete()

    except Exception as e:
        LOGGER.warning(f"Callback error ({action}): {e}")
        await cq.answer(f"❌ {e}", show_alert=True)


# ---------------------------------------------------------------------------
# Owner: /broadcast
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("broadcast") & OWNER_FILTER)
async def broadcast_command(client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        return await message.reply_text(
            f"❌ {smallcaps_title('give me a message to broadcast')}!\nExample: `/broadcast Hello everyone`"
        )

    text = message.text.split(None, 1)[1] if len(message.command) > 1 else None
    users = await db.get_all_users()
    status = await message.reply_text(f"📢 {smallcaps_title('broadcasting to')} {len(users)} {smallcaps_title('users')}...")

    sent, failed = 0, 0
    for uid in users:
        try:
            if message.reply_to_message:
                await message.reply_to_message.copy(uid)
            else:
                await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1

    await status.edit_text(
        f"✅ {smallcaps_title('broadcast done')}.\n{smallcaps_title('sent')}: {sent}\n{smallcaps_title('failed')}: {failed}"
    )


# ---------------------------------------------------------------------------
# /id — user aur chat id batao
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("id"))
async def id_command(client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    lines = [f"👤 **{smallcaps_title('your id')}:** `{user_id}`"]
    if message.chat.type != "private":
        lines.append(f"👥 **{smallcaps_title('chat id')}:** `{message.chat.id}`")
    if message.reply_to_message and message.reply_to_message.from_user:
        lines.append(f"↩️ **{smallcaps_title('replied user id')}:** `{message.reply_to_message.from_user.id}`")
    await message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /ping — bot ka round-trip latency
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("ping"))
async def ping_command(client, message: Message):
    start = time.monotonic()
    sent = await message.reply_text(f"🏓 {smallcaps_title('pinging')}...")
    latency_ms = (time.monotonic() - start) * 1000
    uptime = format_uptime(time.monotonic() - START_TIME)
    await sent.edit_text(
        f"🏓 {smallcaps_title('pong')}!\n"
        f"⌾ {smallcaps_title('latency')} : `{latency_ms:.0f}ms`\n"
        f"⌾ {smallcaps_title('uptime')} : {uptime}"
    )
