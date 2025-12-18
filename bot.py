# Telegram Minecraft Bot (generic)
import datetime
import logging
import os
import re
import signal
import sys
import threading

import telebot
from telebot.types import ReplyKeyboardMarkup
from dotenv import load_dotenv

from functools import wraps

from minecraft import MinecraftServerManager
import access as users

# Load env
try:
    load_dotenv()
except Exception:
    pass

# Required env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
_ADMIN_ID_ENV = os.getenv("ADMIN_ID")
COMPOSE_DIR = os.getenv("COMPOSE_DIR")
RCON_SERVICE = os.getenv("RCON_SERVICE")  # optional; service name for rcon-cli

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not configured. Set in .env (from BotFather).")

try:
    ADMIN_ID = int(_ADMIN_ID_ENV) if _ADMIN_ID_ENV else None
except Exception:
    ADMIN_ID = None

if ADMIN_ID is None:
    raise RuntimeError(
        "ADMIN_ID not configured. Set your numeric Telegram user id in .env."
    )

if not COMPOSE_DIR:
    raise RuntimeError(
        "COMPOSE_DIR not configured. Point it to your docker-compose project directory."
    )

# Optional tuning
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
START_TIMEOUT = int(os.getenv("START_TIMEOUT", "360"))
HEALTH_GRACE_SECONDS = int(os.getenv("HEALTH_GRACE_SECONDS", "120"))

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Wire access module
users.set_bot(bot)
users.set_admin_id(ADMIN_ID)

# Manager
mc_server = MinecraftServerManager(
    COMPOSE_DIR,
    monitor_interval=MONITOR_INTERVAL,
    start_timeout=START_TIMEOUT,
    health_grace_seconds=HEALTH_GRACE_SECONDS,
    rcon_service=RCON_SERVICE,
)

_last_broadcast_state = {"healthy": False, "stopped": False}
_broadcast_state_lock = threading.Lock()


def _safe_message_user_id(message):
    try:
        u = getattr(message, "from_user", None)
        return int(u.id) if u and getattr(u, "id", None) is not None else None
    except Exception:
        return None


def _safe_chat_id(message):
    try:
        c = getattr(message, "chat", None)
        return int(c.id) if c and getattr(c, "id", None) is not None else None
    except Exception:
        return None


# Event handler


def _manager_event_handler(ev):
    ev_name = (ev.get("event") or ev.get("type") or "").lower()
    msg = ev.get("message", str(ev))
    # Treat both manual confirmation and monitor health_ok as healthy confirmations
    is_final_healthy = ev_name in ("manual_start_confirmed", "health_ok")
    is_final_stopped = ev_name in ("server_stop",)
    if not (is_final_healthy or is_final_stopped):
        return

    def _do_broadcast_if_needed():
        try:
            with _broadcast_state_lock:
                if is_final_healthy:
                    if (
                        _last_broadcast_state["healthy"]
                        and not _last_broadcast_state["stopped"]
                    ):
                        return
                    users.broadcast_message(msg)
                    _last_broadcast_state["healthy"] = True
                    _last_broadcast_state["stopped"] = False
                elif is_final_stopped:
                    if (
                        _last_broadcast_state["stopped"]
                        and not _last_broadcast_state["healthy"]
                    ):
                        return
                    users.broadcast_message(msg)
                    _last_broadcast_state["stopped"] = True
                    _last_broadcast_state["healthy"] = False
        except Exception:
            logger.exception("Exception while broadcasting manager event")

    threading.Thread(target=_do_broadcast_if_needed, daemon=True).start()


mc_server.register_event_listener(_manager_event_handler)

# Decorators


def user_restricted(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = _safe_message_user_id(message)
        chat_id = _safe_chat_id(message)
        if user_id is None:
            try:
                bot.reply_to(
                    message,
                    "❌ Could not determine your user id. Message me in private once so I can read it.",
                )
            except Exception:
                pass
            return
        try:
            users_list = [int(u) for u in users.load_users()]
        except Exception:
            logger.exception("users.load_users failed")
            users_list = []
        if user_id == ADMIN_ID or user_id in users_list:
            return func(message, *args, **kwargs)
        if chat_id == ADMIN_ID:
            return func(message, *args, **kwargs)
        try:
            bot.reply_to(message, f"❌ You are not authorized. User id:{user_id}")
        except Exception:
            pass
        return

    return wrapper


def group_chat_restricted(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        chat_id = _safe_chat_id(message)
        user_id = _safe_message_user_id(message)
        if chat_id is None:
            bot.reply_to(message, "❌ Could not determine chat id.")
            return
        try:
            allowed = [int(c) for c in (users.load_chats() or [])]
        except Exception:
            logger.exception("load_chats failed")
            allowed = []
        if user_id == ADMIN_ID:
            return func(message, *args, **kwargs)
        chat_type = getattr(getattr(message, "chat", None), "type", None)
        if (chat_id in allowed) and (chat_type in ("group", "supergroup")):
            return func(message, *args, **kwargs)
        try:
            bot.reply_to(
                message,
                f"❌ This command can't be run here. Use an authorized group chat. Chat id:{chat_id}",
            )
        except Exception:
            pass
        return

    return wrapper


def admin_restricted(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        chat_id = _safe_chat_id(message)
        user_id = _safe_message_user_id(message)
        if user_id is None:
            bot.reply_to(message, "❌ Could not determine your user id.")
            return
        if user_id == ADMIN_ID or chat_id == ADMIN_ID:
            return func(message, *args, **kwargs)
        bot.reply_to(message, f"❌ Admin only. User id:{user_id}. Chat id:{chat_id}.")
        return

    return wrapper


# Group chat helper thunks


def _add_group_chat_thunk(message):
    res = users._add_group_chat_handler(message)
    if res.get("error") == "invalid_chat_id":
        bot.reply_to(
            message,
            "Please provide a numeric chat id. Example: /add_group_chat -1001234567890",
            parse_mode="Markdown",
        )
    elif res.get("ok") and res.get("message") == "added":
        bot.reply_to(message, f"Added chat id {res['chat_id']} to broadcast list.")
    elif res.get("message") == "already_exists":
        bot.reply_to(message, f"{res['chat_id']} is already in the allowed chats list.")
    else:
        bot.reply_to(message, "Unexpected result while adding chat. Check logs.")


def _list_group_chats_thunk(message):
    res = users._list_group_chats_handler(message)
    chats = res.get("chats", [])
    if not chats:
        bot.reply_to(message, "No group chats saved.")
    else:
        pretty = "\n".join(str(c) for c in chats)
        bot.reply_to(message, f"Saved group chats ({len(chats)}):\n{pretty}")


bot.register_message_handler(
    admin_restricted(user_restricted(_add_group_chat_thunk)),
    commands=["add_group_chat"],
)
bot.register_message_handler(
    admin_restricted(user_restricted(_list_group_chats_thunk)),
    commands=["list_group_chats"],
)

# Commands


@bot.message_handler(commands=["start"])
@user_restricted
def send_welcome(message):
    if _safe_chat_id(message) == ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "Admin hints:\n"
            "- Manage users: /add, /list_users, /remove_user\n"
            "- Manage chats: /add_chat, /remove_chat, /list_chats\n"
            "- Broadcast: /broadcast <message>\n"
            "- Stop bot: /shutdown_bot\n",
        )
    bot.reply_to(
        message,
        "Hello, your identity is verified. You can use the following commands (/help for help):",
    )
    markup = ReplyKeyboardMarkup(
        one_time_keyboard=True, input_field_placeholder="Choose an option"
    )
    markup.add(
        "/server_start", "/server_stop", "/server_status", "/server_historic", "/help"
    )
    bot.send_message(message.chat.id, "Select a command:", reply_markup=markup)


@bot.message_handler(
    commands=["add_whitelist"]
)  # optional, requires rcon-cli in the server container
@user_restricted
@admin_restricted
def whitelist(message):
    username = message.text[15:]
    response = mc_server.add_whitelist(username)
    bot.reply_to(message, response.get("message"))


@bot.message_handler(commands=["add"])
@user_restricted
@admin_restricted
def add_user(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    target_raw = None
    if len(parts) > 1:
        target_raw = parts[1].strip()
    elif message.reply_to_message:
        replied = message.reply_to_message
        try:
            self_id = int(getattr(bot.get_me(), "id", 0))
        except Exception:
            self_id = 0
        if (
            getattr(replied, "from_user", None)
            and int(getattr(replied.from_user, "id", 0)) == self_id
        ):
            text_src = getattr(replied, "text", "") or getattr(replied, "caption", "")
            m = re.search(r"\b(\d{5,})\b", text_src)
            if m:
                target_raw = m.group(1)
            else:
                bot.reply_to(
                    message,
                    "Reply to a user's message, or provide `/add <user_id>`.",
                    parse_mode="Markdown",
                )
                return
        elif getattr(replied, "from_user", None):
            target_raw = str(replied.from_user.id)
        elif getattr(replied, "forward_from", None):
            target_raw = str(replied.forward_from.id)
        else:
            bot.reply_to(
                message,
                "I cannot determine the user from that reply. Please run `/add <numeric_id>`.",
                parse_mode="Markdown",
            )
            return
    if not target_raw:
        bot.reply_to(
            message,
            "Please provide a numeric user id: `/add <user_id>`\nOr reply to a user's message with /add.",
            parse_mode="Markdown",
        )
        return
    if target_raw.startswith("@"):
        try:
            chat = bot.get_chat(target_raw)
            target_raw = str(chat.id)
        except Exception:
            bot.reply_to(
                message,
                "I can't resolve that @username to an id. Please provide the numeric id instead.",
            )
            return
    try:
        user_id = int(target_raw)
    except ValueError:
        bot.reply_to(
            message,
            "User id must be an integer. Example: `/add 123456789`",
            parse_mode="Markdown",
        )
        return
    try:
        users_list = [int(u) for u in users.load_users()]
    except Exception:
        logger.exception("users.load_users failed")
        users_list = []
    if user_id in users_list:
        bot.reply_to(message, f"{user_id} is already in the list.")
        return
    users_list.append(user_id)
    try:
        users.save_users(users_list)
    except Exception as e:
        logger.exception("save_users failed")
        bot.reply_to(message, f"❌ Failed to save user: {e}")
        return
    bot.reply_to(message, f"Added {user_id}. Now {len(users_list)} user(s) saved.")


@bot.message_handler(commands=["remove_user"])
@user_restricted
@admin_restricted
def remove_user(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    target_raw = None
    if len(parts) > 1:
        target_raw = parts[1].strip()
    elif message.reply_to_message:
        replied = message.reply_to_message
        if getattr(replied, "from_user", None):
            target_raw = str(replied.from_user.id)
    if not target_raw:
        bot.reply_to(
            message,
            "Please provide a user id to remove: `/remove_user <user_id>`",
            parse_mode="Markdown",
        )
        return
    try:
        user_id = int(target_raw)
    except ValueError:
        bot.reply_to(message, "User id must be an integer.")
        return
    try:
        users_list = [int(u) for u in users.load_users()]
    except Exception:
        logger.exception("users.load_users failed")
        users_list = []
    if user_id not in users_list:
        bot.reply_to(message, f"{user_id} is not in the saved users list.")
        return
    users_list = [u for u in users_list if u != user_id]
    try:
        users.save_users(users_list)
    except Exception:
        logger.exception("save_users failed")
        bot.reply_to(message, "❌ Failed to update user list.")
        return
    bot.reply_to(message, f"Removed {user_id}. Now {len(users_list)} user(s) saved.")


@bot.message_handler(commands=["list_users"])
@user_restricted
@admin_restricted
def list_users_handler(message):
    try:
        users_list = [int(u) for u in users.load_users()]
    except Exception:
        logger.exception("users.load_users failed")
        bot.reply_to(message, "❌ Error reading users list.")
        return
    if not users_list:
        bot.reply_to(message, "No users saved yet.")
        return
    pretty = "\n".join(str(u) for u in users_list)
    bot.reply_to(message, f"Saved users ({len(users_list)}):\n{pretty}")


@bot.message_handler(commands=["add_chat"])
@admin_restricted
def add_chat(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    target_raw = None
    if len(parts) > 1:
        target_raw = parts[1].strip()
    elif message.reply_to_message:
        target_raw = str(message.reply_to_message.chat.id)
    if not target_raw:
        bot.reply_to(
            message,
            "Please provide a numeric chat id or reply to a group message to add it.",
        )
        return
    try:
        chat_id = int(target_raw)
    except ValueError:
        bot.reply_to(message, "Chat id must be numeric.")
        return
    try:
        chats = [int(c) for c in (users.load_chats() or [])]
    except Exception:
        logger.exception("load_chats failed")
        chats = []
    if chat_id in chats:
        bot.reply_to(message, f"Chat {chat_id} already authorized.")
        return
    chats.append(chat_id)
    try:
        users.save_chats(chats)
    except Exception:
        logger.exception("save_chats failed")
        bot.reply_to(message, "❌ Failed to save chat list.")
        return
    bot.reply_to(message, f"Added chat {chat_id}. Now {len(chats)} authorized chats.")


@bot.message_handler(commands=["remove_chat"])
@admin_restricted
def remove_chat(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /remove_chat <chat_id>")
        return
    try:
        chat_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "Chat id must be numeric.")
        return
    try:
        chats = [int(c) for c in (users.load_chats() or [])]
    except Exception:
        logger.exception("load_chats failed")
        chats = []
    if chat_id not in chats:
        bot.reply_to(message, "Chat not authorized.")
        return
    chats = [c for c in chats if c != chat_id]
    try:
        users.save_chats(chats)
    except Exception:
        logger.exception("save_chats failed")
        bot.reply_to(message, "❌ Failed to save chat list.")
        return
    bot.reply_to(message, f"Removed chat {chat_id}. Now {len(chats)} authorized chats.")


@bot.message_handler(commands=["list_chats"])
@admin_restricted
def list_chats_handler(message):
    try:
        chats = [int(c) for c in (users.load_chats() or [])]
    except Exception:
        logger.exception("load_chats failed")
        chats = []
    bot.reply_to(
        message,
        f"Authorized chats ({len(chats)}):\n" + "\n".join(str(c) for c in chats),
    )


@bot.message_handler(commands=["server_start"])
@group_chat_restricted
@user_restricted
def handle_server_start(message):
    mc_server.start_server()
    bot.reply_to(message, "🟠 Start requested. Waiting until the server is ready…")
    try:
        bot.send_message(
            ADMIN_ID,
            f"Start requested by user {_safe_message_user_id(message)}. Waiting for readiness confirmation.",
        )
    except Exception:
        logger.exception("Failed to notify admin about manual start request.")


@bot.message_handler(commands=["server_stop"])
@group_chat_restricted
@user_restricted
def handle_server_stop(message):
    bot.reply_to(message, "Stopping server...")
    response = mc_server.stop_server()
    bot.reply_to(message, response.get("message", "Server stop response"))


@bot.message_handler(commands=["server_status"])
@user_restricted
def handle_server_status(message):
    status = mc_server.server_status()
    bot.reply_to(message, status.get("message", "Status unknown"))


@bot.message_handler(commands=["server_logs"])
@user_restricted
def handle_server_logs(message):
    logs = mc_server.get_logs(5)
    reply = f"{logs.get('message', '')}\n\n{logs.get('logs', 'No logs available')}"
    bot.reply_to(message, reply)


@bot.message_handler(commands=["server_stats"])
@user_restricted
def handle_server_stats(message):
    response = mc_server.get_uptime_stats()
    if response.get("status") == "success":
        stats = response["stats"]
        stats_message = (
            "📊 **Server Uptime Statistics**\n\n"
            f"• **Total Starts**: {stats['total_starts']}\n"
            f"• **Manual Starts**: {stats['manual_starts']}\n"
            f"• **Auto-Detected Starts**: {stats['auto_starts']}\n"
            f"• **Manual Stops**: {stats['manual_stops']}\n"
            f"• **Auto-Detected Stops**: {stats['auto_stops']}\n\n"
        )
        if stats["last_start"]:
            try:
                last_start = datetime.datetime.fromisoformat(
                    stats["last_start"]
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                last_start = stats["last_start"]
            stats_message += f"• **Last Start**: {last_start}\n"
        else:
            stats_message += "• **Last Start**: Never\n"
        if stats["last_stop"]:
            try:
                last_stop = datetime.datetime.fromisoformat(
                    stats["last_stop"]
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                last_stop = stats["last_stop"]
            stats_message += f"• **Last Stop**: {last_stop}\n"
        else:
            stats_message += "• **Last Stop**: Never\n"
        stats_message += "\n**Daily Starts (Last 7 days)**:\n"
        for day in stats["daily_stats"][:7]:
            stats_message += f"• {day['date']}: {day['starts']} times\n"
        bot.reply_to(message, stats_message, parse_mode="Markdown")
    else:
        bot.reply_to(
            message,
            f"❌ Error getting statistics: {response.get('error', 'Unknown error')}",
        )


@bot.message_handler(commands=["server_uptime_log"])
@user_restricted
def handle_server_uptime_log(message):
    try:
        parts = message.text.split()
        lines = int(parts[1]) if len(parts) > 1 else 10
    except Exception:
        lines = 10
    response = mc_server.get_uptime_log(lines)
    if response.get("status") == "success":
        if response.get("logs"):
            log_message = f"📋 **Last {len(response['logs'])} Uptime Events**\n\n"
            for log_entry in response["logs"]:
                try:
                    parts = log_entry.split(" - ")
                    if len(parts) >= 3:
                        ts = datetime.datetime.fromisoformat(parts[0]).strftime(
                            "%m/%d %H:%M"
                        )
                        event = parts[1]
                        reason = parts[2] if len(parts) > 2 else ""
                        event_map = {
                            "SERVER_START": "🟢 SERVER STARTED",
                            "SERVER_START_CONFIRMED": "🟢 SERVER STARTED (confirmed)",
                            "SERVER_STOP": "🔴 SERVER STOPPED",
                            "SERVER_HEALTH_ISSUE": "🟡 SERVER HEALTH ISSUE",
                            "START_FAILED": "❌ START FAILED",
                            "STOP_FAILED": "❌ STOP FAILED",
                        }
                        reason_map = {
                            "manual_start": "manual start",
                            "manual_start_confirmed": "manual start (confirmed)",
                            "manual_start_ignored_duplicate": "duplicate start ignored",
                            "manual_stop": "manual stop",
                            "auto_detected": "auto-detected",
                            "idle_timeout": "idle timeout",
                        }
                        display_event = event_map.get(event, event)
                        display_reason = reason_map.get(reason, reason)
                        log_message += f"`{ts}` {display_event}"
                        if display_reason:
                            log_message += f" - {display_reason}"
                        log_message += "\n"
                    else:
                        log_message += f"`{log_entry}`\n"
                except Exception:
                    log_message += f"`{log_entry}`\n"
            if len(log_message) > 4000:
                log_message = (
                    log_message[:4000]
                    + "\n\n... (log too long, showing first part only)"
                )
            bot.reply_to(message, log_message, parse_mode="Markdown")
        else:
            bot.reply_to(message, "📝 No events recorded yet.")
    else:
        bot.reply_to(
            message,
            f"❌ Error getting the log: {response.get('error', 'Unknown error')}",
        )


@bot.message_handler(commands=["server_historic"])
@user_restricted
def handle_server_historic(message):
    response = mc_server.get_historic_uptime()
    if response.get("status") == "success":
        data = response["data"]
        if data["total_sessions"] == 0:
            bot.reply_to(message, "📊 No historic uptime data available yet.")
            return
        message_text = (
            "📊 **Historic Uptime Statistics**\n\n"
            f"• **Total Uptime**: {data['total_uptime_hours']} hours\n"
            f"• **Total Sessions**: {data['total_sessions']}\n"
            f"• **Average Session**: {data['average_session_hours']} hours\n"
            f"• **Longest Session**: {data['longest_session_hours']} hours\n\n"
        )
        today = datetime.datetime.now()
        message_text += "**Recent Daily Uptime:**\n"
        days_shown = 0
        for i in range(14):
            date = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            hours = data["uptime_by_day"].get(date, 0)
            if hours > 0 or days_shown < 7:
                message_text += f"• {date}: {hours:.1f} hours\n"
                days_shown += 1
            if days_shown >= 7:
                break
        bot.reply_to(message, message_text, parse_mode="Markdown")
    else:
        bot.reply_to(message, f"❌ Error: {response.get('error', 'Unknown error')}")


@bot.message_handler(commands=["debug_monitor"])
@user_restricted
def handle_debug_monitor(message):
    response = mc_server.get_monitoring_status()
    if response.get("status") == "success":
        data = response["data"]
        monitor_status = "✅ RUNNING" if data["monitor_running"] else "❌ STOPPED"
        current_session = (
            "✅ ACTIVE" if data["current_session_active"] else "❌ INACTIVE"
        )
        interval_str = (
            f"{mc_server.monitor_interval // 60} minutes"
            if mc_server.monitor_interval >= 60
            else f"{mc_server.monitor_interval} seconds"
        )
        message_text = (
            "🔍 **Monitor Status**\n\n"
            f"• **Status**: {monitor_status}\n"
            f"• **Check Interval**: {interval_str}\n"
            f"• **Last Known Status**: {data['last_known_status'].upper()}\n"
            f"• **Current Session**: {current_session}\n"
            f"• **Auto-detected Events**: {data['auto_detected_events']}\n\n"
            "The monitor checks server status on the configured interval and automatically detects when the server starts/stops."
        )
        bot.reply_to(message, message_text, parse_mode="Markdown")
    else:
        bot.reply_to(message, f"❌ Error: {response.get('error', 'Unknown error')}")


@bot.message_handler(commands=["help"])
@user_restricted
def help_cmd(message):
    interval_str = (
        f"{mc_server.monitor_interval // 60} minutes"
        if mc_server.monitor_interval >= 60
        else f"{mc_server.monitor_interval} seconds"
    )
    help_text = (
        "🤖 **Minecraft Server Bot**\n\n"
        "Commands:\n"
        "• `/server_start` — Start the server (authorized group)\n"
        "• `/server_stop` — Stop the server (authorized group)\n"
        "• `/server_status` — Show server status\n"
        "• `/server_logs` — Show recent server logs\n"
        "• `/server_stats` — Start/stop counters and last events\n"
        "• `/server_uptime_log` — Show uptime event log\n"
        "• `/server_historic` — Historic uptime statistics\n"
        "• `/debug_monitor` — Monitor thread details\n\n"
        "Admin-only:\n"
        "• `/add <user_id>` — Authorize a user\n"
        "• `/remove_user <user_id>` — Revoke a user\n"
        "• `/list_users` — List authorized users\n"
        "• `/add_chat <chat_id>` — Authorize a group chat\n"
        "• `/remove_chat <chat_id>` — Revoke a group chat\n"
        "• `/list_chats` — List authorized chats\n"
        "• `/broadcast <message>` — Broadcast to users and chats\n\n"
        f"Monitoring every {interval_str}."
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")


@bot.message_handler(commands=["broadcast"])
@admin_restricted
def admin_broadcast(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /broadcast <message>")
        return
    msg = parts[1].strip()
    try:
        results = users.broadcast_message(msg)
        bot.reply_to(message, f"Broadcast sent. Results: {results}")
    except Exception:
        logger.exception("Admin broadcast failed")
        bot.reply_to(message, "Broadcast failed. Check logs.")


@bot.message_handler(commands=["shutdown_bot"])
@admin_restricted
def shutdown_bot(message):
    bot.reply_to(message, "Shutting down bot and stopping monitor...")

    def _shutdown():
        try:
            mc_server.close()
        except Exception:
            logger.exception("Error closing mc_server")
        try:
            bot.stop_polling()
        except Exception:
            logger.exception("Failed to stop polling cleanly")
        try:
            os._exit(0)
        except Exception:
            sys.exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()


if __name__ == "__main__":

    def _handle_signal(sig, frame):
        logger.info("Signal received, shutting down...")
        try:
            mc_server.close()
        except Exception:
            logger.exception("Error closing mc_server during signal handling")
        try:
            bot.stop_polling()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    print("Starting bot")
    bot.infinity_polling()
