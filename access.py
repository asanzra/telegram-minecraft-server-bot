# access.py
import os
import json
import tempfile
import logging
import threading
import time
from typing import List, Tuple, Dict, Any, Optional

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# --- Data file locations (adjust if needed) ---
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
CHATS_FILE = os.path.join(DATA_DIR, "chats.json")
BACKUP_USERS = os.path.join(DATA_DIR, "users.json.bak")
BACKUP_CHATS = os.path.join(DATA_DIR, "chats.json.bak")

# Internal runtime state (set by main script)
_bot = None  # set via set_bot(bot) in main program
_admin_id: Optional[int] = None  # set via set_admin_id(admin_id) in main program

# IO lock to guard reads/writes (recommended)
_io_lock = threading.Lock()


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _atomic_write(path: str, obj):
    """
    Atomic write: write JSON to a temp file then os.replace().
    `obj` will be serialized as JSON.
    """
    _ensure_data_dir()
    dirpath = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            json.dump(obj, tf, separators=(",", ":"), ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
        # backup old file (best-effort)
        try:
            if os.path.exists(path):
                os.replace(path, path + ".bak")
        except Exception:
            logger.warning("Could not create backup of %s", path)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# -------------------------
# Persistence functions (thread-safe)
# -------------------------


def load_users() -> List[int]:
    """
    Return a list of unique ints (user ids).
    Non-numeric entries are ignored (logged).
    If file missing -> [].
    """
    _ensure_data_dir()
    with _io_lock:
        if not os.path.exists(USERS_FILE):
            return []
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception("Failed to read users file; returning empty list.")
            return []

    if not isinstance(raw, list):
        logger.warning("users.json is not a list. Ignoring.")
        return []

    users = []
    seen = set()
    removed = []
    for item in raw:
        try:
            if isinstance(item, int):
                uid = item
            elif isinstance(item, str) and item.isdigit():
                uid = int(item)
            else:
                removed.append(item)
                continue
            if uid not in seen:
                seen.add(uid)
                users.append(uid)
        except Exception:
            removed.append(item)

    if removed:
        logger.info("Ignored non-numeric user entries: %s", removed)
    return users


def save_users(users: List[int]) -> None:
    """
    Save a list of users (convert to ints, dedupe, stable order).
    """
    normalized = []
    seen = set()
    for u in users:
        try:
            ui = int(u)
        except Exception:
            logger.warning("Skipping non-integer when saving users: %r", u)
            continue
        if ui not in seen:
            seen.add(ui)
            normalized.append(ui)
    with _io_lock:
        _atomic_write(USERS_FILE, normalized)
    logger.info("Saved %d users", len(normalized))


def load_chats() -> List[int]:
    """Return a list of unique ints (allowed group chat ids)."""
    _ensure_data_dir()
    with _io_lock:
        if not os.path.exists(CHATS_FILE):
            return []
        try:
            with open(CHATS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception("Failed to read chats file; returning empty list.")
            return []

    if not isinstance(raw, list):
        logger.warning("chats.json is not a list. Ignoring.")
        return []

    chats = []
    seen = set()
    removed = []
    for item in raw:
        try:
            if isinstance(item, int):
                cid = item
            elif isinstance(item, str) and (item.lstrip("-").isdigit()):
                cid = int(item)
            else:
                removed.append(item)
                continue
            if cid not in seen:
                seen.add(cid)
                chats.append(cid)
        except Exception:
            removed.append(item)
    if removed:
        logger.info("Ignored non-numeric chat entries: %s", removed)
    return chats


def save_chats(chats: List[int]) -> None:
    """Save list of chat ids (convert to ints, dedupe)."""
    normalized = []
    seen = set()
    for c in chats:
        try:
            ci = int(c)
        except Exception:
            logger.warning("Skipping non-integer when saving chats: %r", c)
            continue
        if ci not in seen:
            seen.add(ci)
            normalized.append(ci)
    with _io_lock:
        _atomic_write(CHATS_FILE, normalized)
    logger.info("Saved %d chats", len(normalized))


# -----------------------------------------------------------------------------
# Runtime wiring: set the bot and admin id from main script (after bot created)
# -----------------------------------------------------------------------------


def set_bot(bot_instance) -> None:
    """Call this once from your main script after creating the TeleBot: access.set_bot(bot)."""
    global _bot
    _bot = bot_instance


def set_admin_id(admin_id: int) -> None:
    """Call this once to give access module the admin id (optional but recommended)."""
    global _admin_id
    try:
        _admin_id = int(admin_id)
    except Exception:
        _admin_id = None


# -------------------------
# Broadcast function (robust)
# -------------------------


def broadcast_message(text: str, silent: bool = False) -> Dict[str, Any]:
    """
    Broadcast `text` to all saved users and saved group chats.
    - Requires set_bot(bot) to have been called.
    - Skips ids that raise exceptions, logs errors.
    - If `silent` True, uses disable_notification=True.
    Returns dict with 'sent' and 'failed' lists.
    """
    global _bot, _admin_id
    if _bot is None:
        raise RuntimeError(
            "Bot instance not set. Call access.set_bot(bot) from your main script before broadcasting."
        )

    users = load_users()
    chats = load_chats()

    targets: List[Tuple[str, int]] = []
    # include admin id if not already present (send as user)
    try:
        if _admin_id is not None and _admin_id not in users and _admin_id not in chats:
            targets.append(("user", _admin_id))
    except Exception:
        pass

    for u in users:
        targets.append(("user", u))
    for c in chats:
        targets.append(("chat", c))

    # Deduplicate by id but preserve order: will send once per unique id
    sent = set()
    results = {"sent": [], "failed": []}
    for ttype, tid in targets:
        if tid in sent:
            continue
        sent.add(tid)
        try:
            _bot.send_message(
                tid, text, disable_web_page_preview=True, disable_notification=silent
            )
            results["sent"].append((ttype, tid))
        except Exception as e:
            logger.exception("Failed to send broadcast to %s (%s): %s", ttype, tid, e)
            results["failed"].append((ttype, tid, str(e)))
            # continue broadcasting to others

        # gentle pause to reduce risk of hitting rate limits
        time.sleep(0.02)

    return results


# -----------------------------------------------------------------------------
# Handler functions (no decorator usage here). These are plain callables that
# the main script can register with bot.register_message_handler(...) AFTER
# the bot and the decorators exist.
# -----------------------------------------------------------------------------


def _add_group_chat_handler(message):
    """
    Same behavior as your previous add_group_chat handler.
    Intended to be wrapped by user/admin decorators and registered by main script.
    """
    parts = (message.text or "").split(maxsplit=1)
    target = None

    if len(parts) > 1:
        target = parts[1].strip()
    elif getattr(message, "reply_to_message", None):
        # If admin replies to a message from the group, message.chat.id is the group id
        target = str(
            message.reply_to_message.chat.id
            if getattr(message.reply_to_message, "chat", None)
            else message.chat.id
        )
    else:
        target = str(message.chat.id)

    # normalize
    try:
        chat_id = int(target)
    except Exception:
        # reply to sender; we don't import bot here; the main handler wrapper should handle replying
        return {
            "error": "invalid_chat_id",
            "message": "Please provide a numeric chat id. Example: /add_group_chat -1001234567890",
        }

    chats = load_chats()
    if chat_id in chats:
        return {"ok": False, "message": "already_exists", "chat_id": chat_id}

    chats.append(chat_id)
    save_chats(chats)
    return {"ok": True, "message": "added", "chat_id": chat_id}


def _list_group_chats_handler(message):
    chats = load_chats()
    return {"ok": True, "chats": chats}


# -----------------------------------------------------------------------------
# Helper to register these handlers with a TeleBot instance (from main script)
# -----------------------------------------------------------------------------


def register_handlers(
    bot_instance, user_restricted_decorator, admin_restricted_decorator
):
    """
    Register internal handler functions with the provided bot.
    - bot_instance: the TeleBot instance (required)
    - user_restricted_decorator, admin_restricted_decorator: decorator functions from main script
      (these wrap a callable and return a callable)
    Example (in main script):
        import access as users
        users.set_bot(bot)
        users.set_admin_id(ADMIN_ID)
        users.register_handlers(bot, user_restricted, admin_restricted)
    """
    if bot_instance is None:
        raise RuntimeError("bot_instance is required to register handlers")

    # Compose wrappers so both user_restricted and admin_restricted are enforced.
    wrapped_add = admin_restricted_decorator(
        user_restricted_decorator(_add_group_chat_handler)
    )
    wrapped_list = admin_restricted_decorator(
        user_restricted_decorator(_list_group_chats_handler)
    )

    # Register with TeleBot: use register_message_handler rather than module decorators
    bot_instance.register_message_handler(wrapped_add, commands=["add_group_chat"])
    bot_instance.register_message_handler(wrapped_list, commands=["list_group_chats"])

    logger.info(
        "Registered add_group_chat and list_group_chats handlers with provided bot instance."
    )


# -----------------------------------------------------------------------------
# Convenience functions for programmatic use
# -----------------------------------------------------------------------------


def add_group_chat(chat_id: int) -> bool:
    chats = load_chats()
    if int(chat_id) in chats:
        return False
    chats.append(int(chat_id))
    save_chats(chats)
    return True


def list_group_chats() -> List[int]:
    return load_chats()
