from clients import db, LOGGER

users_col = db["users"]
chats_col = db["chats"]
settings_col = db["settings"]

BOT_STATUS_KEY = "bot_status"
PROCESSING_TEXT_KEY = "processing_text_status"
VPLAY_STATUS_KEY = "vplay_status"


async def add_user(user_id: int):
    try:
        await users_col.update_one(
            {"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True
        )
    except Exception as e:
        LOGGER.warning(f"add_user DB error: {e}")


async def add_chat(chat_id: int):
    try:
        await chats_col.update_one(
            {"_id": chat_id}, {"$set": {"_id": chat_id}}, upsert=True
        )
    except Exception as e:
        LOGGER.warning(f"add_chat DB error: {e}")


async def get_all_users():
    try:
        return [doc["_id"] async for doc in users_col.find({})]
    except Exception as e:
        LOGGER.warning(f"get_all_users DB error: {e}")
        return []


async def get_all_chats():
    try:
        return [doc["_id"] async for doc in chats_col.find({})]
    except Exception as e:
        LOGGER.warning(f"get_all_chats DB error: {e}")
        return []


# ---------------------------------------------------------------------------
# Bot ka global ON/OFF status (owner: /on /off) — restart ke baad bhi yaad
# rehta hai, kyunki yeh DB mein persist hota hai.
# ---------------------------------------------------------------------------
async def set_bot_status(is_on: bool):
    try:
        await settings_col.update_one(
            {"_id": BOT_STATUS_KEY},
            {"$set": {"is_on": is_on}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"set_bot_status DB error: {e}")


async def get_bot_status() -> bool:
    try:
        doc = await settings_col.find_one({"_id": BOT_STATUS_KEY})
        if doc is None:
            return True
        return bool(doc.get("is_on", True))
    except Exception as e:
        LOGGER.warning(f"get_bot_status DB error: {e}")
        return True


# ---------------------------------------------------------------------------
# Processing text ON/OFF status (owner: /processingon /processingoff) —
# restart ke baad bhi yaad rehta hai, kyunki yeh DB mein persist hota hai.
# ---------------------------------------------------------------------------
async def set_processing_text_status(is_on: bool):
    try:
        await settings_col.update_one(
            {"_id": PROCESSING_TEXT_KEY},
            {"$set": {"is_on": is_on}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"set_processing_text_status DB error: {e}")


async def get_processing_text_status() -> bool:
    try:
        doc = await settings_col.find_one({"_id": PROCESSING_TEXT_KEY})
        if doc is None:
            return True
        return bool(doc.get("is_on", True))
    except Exception as e:
        LOGGER.warning(f"get_processing_text_status DB error: {e}")
        return True


# ---------------------------------------------------------------------------
# /vplay ON/OFF status (owner: /vplayon /vplayoff) — restart ke baad bhi
# yaad rehta hai, kyunki yeh DB mein persist hota hai.
# ---------------------------------------------------------------------------
async def set_vplay_status(is_on: bool):
    try:
        await settings_col.update_one(
            {"_id": VPLAY_STATUS_KEY},
            {"$set": {"is_on": is_on}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"set_vplay_status DB error: {e}")


async def get_vplay_status() -> bool:
    try:
        doc = await settings_col.find_one({"_id": VPLAY_STATUS_KEY})
        if doc is None:
            return True
        return bool(doc.get("is_on", True))
    except Exception as e:
        LOGGER.warning(f"get_vplay_status DB error: {e}")
        return True


# ---------------------------------------------------------------------------
# /restrict /unrestrict — group admin/owner reply karke kisi user ko bot ke
# commands se rok/chhod sakte hain (per-chat).
# ---------------------------------------------------------------------------
restricted_col = db["restricted"]


def _restrict_id(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


async def restrict_user(chat_id: int, user_id: int):
    try:
        await restricted_col.update_one(
            {"_id": _restrict_id(chat_id, user_id)},
            {"$set": {"chat_id": chat_id, "user_id": user_id}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"restrict_user DB error: {e}")


async def unrestrict_user(chat_id: int, user_id: int):
    try:
        await restricted_col.delete_one({"_id": _restrict_id(chat_id, user_id)})
    except Exception as e:
        LOGGER.warning(f"unrestrict_user DB error: {e}")


async def is_restricted(chat_id: int, user_id: int) -> bool:
    try:
        doc = await restricted_col.find_one({"_id": _restrict_id(chat_id, user_id)})
        return doc is not None
    except Exception as e:
        LOGGER.warning(f"is_restricted DB error: {e}")
        return False
