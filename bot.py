# bot.py
# Требуется: python-telegram-bot[webhooks,job-queue] >= 21  (см. requirements.txt)

import json
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- ХРАНИЛИЩЕ ДАННЫХ ----------
# Если DATA_DIR не задан, пишем локально в ./data (на Render без диска данные сотрутся при новом деплое)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Логи кладём рядом, чтобы видеть действия
LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("taskbot")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(LOGS_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(ch)

# Токен берём из окружения
TOKEN = os.getenv("TG_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан TG_BOT_TOKEN в окружении.")

# интервалы напоминаний
REM_OPTIONS = {
    "5m": 5 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "3h": 3 * 60 * 60,
    "6h": 6 * 60 * 60,
}

# ---------- УТИЛЫ ХРАНИЛИЩА ----------
def _user_file(user_id: int) -> Path:
    return DATA_DIR / f"{user_id}.json"

def _ensure_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    data.setdefault("seq", 0)
    data.setdefault("tasks", {})           # id -> {id, name, progress(0..100), reminder_interval}
    data.setdefault("stats", {"closed": 0})
    return data

def load_tasks(user_id: int) -> Dict[str, Any]:
    fp = _user_file(user_id)
    if fp.exists():
        try:
            return _ensure_defaults(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            return {"seq": 0, "tasks": {}, "stats": {"closed": 0}}
    return {"seq": 0, "tasks": {}, "stats": {"closed": 0}}

def save_tasks(user_id: int, data: Dict[str, Any]) -> None:
    data = _ensure_defaults(data)
    fp = _user_file(user_id)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)

# ---------- ВИЗУАЛ ПРОГРЕССА ----------
PALETTE = ["🟥","🟥","🟧","🟧","🟨","🟨","🟩","🟩","🟩","🟩"]
EMPTY = "◻️"

def progress_bar(percent: int) -> str:
    pct = max(0, min(100, int(percent)))
    filled = round(pct / 10)  # 0..10 сегментов
    bar = "".join(PALETTE[i] for i in range(filled)) + (EMPTY * (10 - filled))
    return f"{bar} {pct}%"

def task_line(t: Dict[str, Any]) -> str:
    rem = ""
    interval = t.get("reminder_interval")
    labels = {
        REM_OPTIONS["5m"]: "5м",
        REM_OPTIONS["30m"]: "30м",
        REM_OPTIONS["1h"]: "1ч",
        REM_OPTIONS["3h"]: "3ч",
        REM_OPTIONS["6h"]: "6ч",
    }
    if interval:
        rem = f" • напоминание: {labels.get(int(interval), str(interval)+'с')}"
    return f"{t['name']}\n{progress_bar(t.get('progress', 0))}{rem}"

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Задача", callback_data="ui:new")],
        [InlineKeyboardButton("📋 Мои задачи", callback_data="ui:list")]
    ])

def reminder_menu_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 5 мин", callback_data=f"t:rem5m:{tid}"),
            InlineKeyboardButton("🔔 30 мин", callback_data=f"t:rem30m:{tid}"),
        ],
        [
            InlineKeyboardButton("🔔 1 час", callback_data=f"t:rem1h:{tid}"),
            InlineKeyboardButton("🔔 3 часа", callback_data=f"t:rem3h:{tid}"),
            InlineKeyboardButton("🔔 6 часов", callback_data=f"t:rem6h:{tid}"),
        ],
        [InlineKeyboardButton("🧪 Тест (5 сек)", callback_data=f"t:remtest:{tid}")],
        [InlineKeyboardButton("🔕 Отключить", callback_data=f"t:remoff:{tid}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"t:open:{tid}")]
    ])

def task_kb(task_id: int, t: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("＋10%", callback_data=f"t:+10:{task_id}"),
            InlineKeyboardButton("－10%", callback_data=f"t:-10:{task_id}")
        ],
        [
            InlineKeyboardButton("📝 Переименовать", callback_data=f"t:ren:{task_id}"),
            InlineKeyboardButton("🔄 Сброс 0%", callback_data=f"t:reset:{task_id}")
        ],
        [
            InlineKeyboardButton("🔔 Напоминание", callback_data=f"t:rem:{task_id}"),
            InlineKeyboardButton("✅ Закрыть", callback_data=f"t:close:{task_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"t:del:{task_id}")
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="ui:list")],
        [
            InlineKeyboardButton("➕ Новая", callback_data="ui:new"),
            InlineKeyboardButton("📋 Список", callback_data="ui:list")
        ]
    ])

def parse_new_payload(text: str) -> Tuple[str, int]:
    return text.strip(), 0

async def safe_edit(query, text: str, reply_markup=None):
    try:
        if query and query.message and query.message.text == text:
            await query.answer()
            return
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer()
            return
        raise

# ---------- JOB QUEUE ----------
def _job_name(user_id: int, tid: int) -> str:
    return f"rem:{user_id}:{tid}"

def _cancel_reminder(app: Application, user_id: int, tid: int) -> None:
    for job in app.job_queue.get_jobs_by_name(_job_name(user_id, tid)):
        job.schedule_removal()
    logger.info(f"REM: cancel user={user_id} tid={tid}")

def _schedule_reminder(app: Application, user_id: int, tid: int, interval: int) -> None:
    _cancel_reminder(app, user_id, tid)
    app.job_queue.run_repeating(
        reminder_tick,
        interval=interval,
        first=interval,
        name=_job_name(user_id, tid),
        data={"user_id": user_id, "tid": tid},
    )
    logger.info(f"REM: schedule user={user_id} tid={tid} every={interval}s")

async def reminder_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    tid = context.job.data["tid"]
    data = load_tasks(user_id)
    t = data["tasks"].get(str(tid))
    if not t:
        _cancel_reminder(context.application, user_id, tid)
        return
    text = f"Напоминание по задаче: {t['name']}\nОткрыть, продлить или отключить?"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 +5м", callback_data=f"t:rem5m:{tid}"),
            InlineKeyboardButton("🔔 +30м", callback_data=f"t:rem30m:{tid}"),
            InlineKeyboardButton("🔔 +1ч", callback_data=f"t:rem1h:{tid}"),
        ],
        [
            InlineKeyboardButton("Открыть карточку", callback_data=f"t:open:{tid}"),
            InlineKeyboardButton("🔕 Выкл", callback_data=f"t:remoff:{tid}"),
        ],
    ])
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
        logger.info(f"REM: tick sent user={user_id} tid={tid}")
    except Exception as e:
        logger.warning(f"REM: tick failed user={user_id} tid={tid} err={e}")

async def reminder_test_once(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    tid = context.job.data["tid"]
    data = load_tasks(user_id)
    t = data["tasks"].get(str(tid))
    if not t:
        return
    text = f"Тест-напоминание (5 сек): {t['name']}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть карточку", callback_data=f"t:open:{tid}")],
        [InlineKeyboardButton("🔕 Выкл", callback_data=f"t:remoff:{tid}")],
    ])
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
        logger.info(f"REM: test sent user={user_id} tid={tid}")
    except Exception as e:
        logger.warning(f"REM: test failed user={user_id} tid={tid} err={e}")

# ---------- ХЕНДЛЕРЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = None
    text = (
        "Привет! Это минималистичный трекер-напоминалка.\n"
        "/new Название — создать\n"
        "/list — список\n"
        "/stats — статистика\n"
        "/debugrem — активные напоминания"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_kb())
    else:
        await safe_edit(update.callback_query, text, reply_markup=main_menu_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    opened = len(data["tasks"])
    closed = data["stats"]["closed"]
    await update.message.reply_text(f"Статистика: открытых {opened}, закрытых {closed}", reply_markup=main_menu_kb())

async def debugrem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    rows = []
    for sid, t in sorted(data["tasks"].items(), key=lambda x: int(x[0])):
        if t.get("reminder_interval"):
            rows.append(f"{sid}. {t['name']} — {progress_bar(int(t.get('progress',0)))}")
    txt = "Активные напоминания:\n" + ("\n".join(rows) if rows else "нет")
    await update.message.reply_text(txt, reply_markup=main_menu_kb())

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    raw = " ".join(context.args) if context.args else ""
    if not raw:
        context.user_data["awaiting"] = {"mode": "new"}
        await update.message.reply_text("Введи название задачи", reply_markup=main_menu_kb())
        return
    name, _ = parse_new_payload(raw)
    data["seq"] += 1
    tid = data["seq"]
    data["tasks"][str(tid)] = {"id": tid, "name": name, "progress": 0, "reminder_interval": None}
    save_tasks(user_id, data)
    logger.info(f"TASK: create user={user_id} tid={tid} name={name}")
    await update.message.reply_text(task_line(data["tasks"][str(tid)]), reply_markup=task_kb(tid, data["tasks"][str(tid)]))

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    opened = len(data["tasks"])
    closed = data["stats"]["closed"]
    if not data["tasks"]:
        txt = f"Пока нет задач. Открытых {opened}, закрытых {closed}.\nНажми «➕ Задача» или /new Название"
        if update.message:
            await update.message.reply_text(txt, reply_markup=main_menu_kb())
        else:
            await safe_edit(update.callback_query, txt, reply_markup=main_menu_kb())
        return
    lines = [f"Открытых: {opened} | Закрытых: {closed}", ""]
    keyboard = []
    for sid, t in sorted(data["tasks"].items(), key=lambda x: int(x[0])):
        tid = int(sid)
        lines.append(f"{tid}. {t['name']} — {progress_bar(int(t.get('progress',0)))}")
        keyboard.append([InlineKeyboardButton(f"Открыть: {t['name']}", callback_data=f"t:open:{tid}")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton("⬅️ Меню", callback_data="ui:menu")]])
    if update.message:
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await safe_edit(update.callback_query, text, reply_markup=kb)

async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    if not q.data:
        return

    if q.data == "ui:menu":
        context.user_data["awaiting"] = None
        await safe_edit(q, "Главное меню", reply_markup=main_menu_kb())
        return
    if q.data == "ui:new":
        context.user_data["awaiting"] = {"mode": "new"}
        await safe_edit(q, "Введи название новой задачи", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="ui:menu")]]))
        return
    if q.data == "ui:list":
        await list_cmd(update, context)
        return
    if q.data == "noop":
        return
    if q.data.startswith("t:rem:"):
        try:
            _, _, sid = q.data.split(":")
            tid = int(sid)
        except Exception:
            return
        t = data["tasks"].get(str(tid))
        if not t:
            await safe_edit(q, "Эта задача уже отсутствует.", reply_markup=main_menu_kb())
            return
        await safe_edit(q, f"Напоминания для: {t['name']}", reply_markup=reminder_menu_kb(tid))
        return

    parts = q.data.split(":")
    try:
        if len(parts) == 3:
            _, action, sid = parts
            tid = int(sid)
        elif len(parts) == 4:
            _, action, _, sid = parts
            tid = int(sid)
        else:
            return
    except Exception:
        return

    t = data["tasks"].get(str(tid))
    if action not in {"rem5m", "rem30m", "rem1h", "rem3h", "rem6h", "remoff", "remtest"} and not t and action not in {"close"}:
        await safe_edit(q, "Эта задача уже отсутствует.", reply_markup=main_menu_kb())
        return

    if action in {"rem5m", "rem30m", "rem1h", "rem3h", "rem6h"}:
        key = action.replace("rem","")
        seconds = {
            "5m": REM_OPTIONS["5m"],
            "30m": REM_OPTIONS["30m"],
            "1h": REM_OPTIONS["1h"],
            "3h": REM_OPTIONS["3h"],
            "6h": REM_OPTIONS["6h"],
        }[key]
        if t:
            t["reminder_interval"] = seconds
            save_tasks(user_id, data)
        _schedule_reminder(context.application, user_id, tid, seconds)
        await safe_edit(q, f"Напоминание каждые {key} установлено.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к задаче", callback_data=f"t:open:{tid}")],
            [InlineKeyboardButton("🔕 Отключить", callback_data=f"t:remoff:{tid}")]
        ]))
        return

    if action == "remoff":
        if t:
            t["reminder_interval"] = None
            save_tasks(user_id, data)
        _cancel_reminder(context.application, user_id, tid)
        await safe_edit(q, "Напоминания отключены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к задаче", callback_data=f"t:open:{tid}")]]))
        return

    if action == "remtest":
        context.application.job_queue.run_once(reminder_test_once, when=5, data={"user_id": user_id, "tid": tid})
        await safe_edit(q, "Тест-напоминание придёт через 5 секунд.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к задаче", callback_data=f"t:open:{tid}")]]))
        return

    if not t and action != "close":
        await safe_edit(q, "Эта задача уже отсутствует.", reply_markup=main_menu_kb())
        return

    if action == "open":
        await safe_edit(q, task_line(t), reply_markup=task_kb(tid, t))
        return

    if action == "+10":
        t["progress"] = min(100, int(t.get("progress", 0)) + 10)
    elif action == "-10":
        t["progress"] = max(0, int(t.get("progress", 0)) - 10)
    elif action == "reset":
        t["progress"] = 0
    elif action == "ren":
        context.user_data["awaiting"] = {"mode": "rename", "id": tid}
        await safe_edit(q, "Введи новое название задачи:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"t:open:{tid}")]]))
        return
    elif action == "del":
        _cancel_reminder(context.application, user_id, tid)
        if str(tid) in data["tasks"]:
            del data["tasks"][str(tid)]
        save_tasks(user_id, data)
        logger.info(f"TASK: delete user={user_id} tid={tid}")
        await list_cmd(update, context)
        return
    elif action == "close":
        _cancel_reminder(context.application, user_id, tid)
        if str(tid) in data["tasks"]:
            del data["tasks"][str(tid)]
        data["stats"]["closed"] = int(data["stats"].get("closed", 0)) + 1
        save_tasks(user_id, data)
        logger.info(f"TASK: close user={user_id} tid={tid}")
        await list_cmd(update, context)
        return

    save_tasks(user_id, data)
    await safe_edit(q, task_line(t), reply_markup=task_kb(tid, t))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    awaiting = context.user_data.get("awaiting")
    if awaiting:
        mode = awaiting.get("mode")
        data = load_tasks(user_id)
        if mode == "new":
            name, _ = parse_new_payload(update.message.text)
            data["seq"] += 1
            tid = data["seq"]
            data["tasks"][str(tid)] = {"id": tid, "name": name, "progress": 0, "reminder_interval": None}
            save_tasks(user_id, data)
            logger.info(f"TASK: create(user input) user={user_id} tid={tid} name={name}")
            context.user_data["awaiting"] = None
            await update.message.reply_text(task_line(data["tasks"][str(tid)]), reply_markup=task_kb(tid, data["tasks"][str(tid)]))
            return
        if mode == "rename":
            tid = int(awaiting["id"])
            t = data["tasks"].get(str(tid))
            if t:
                t["name"] = update.message.text.strip() or t["name"]
                save_tasks(user_id, data)
                logger.info(f"TASK: rename user={user_id} tid={tid} new_name={t['name']}")
                await update.message.reply_text(task_line(t), reply_markup=task_kb(tid, t))
            context.user_data["awaiting"] = None
            return
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_kb())

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, BadRequest) and "Message is not modified" in str(err):
        return
    logger.error(f"ERROR: {err}")

def _restore_reminders(app: Application) -> None:
    for fp in DATA_DIR.glob("*.json"):
        try:
            user_id = int(fp.stem)
        except ValueError:
            continue
        data = load_tasks(user_id)
        for sid, t in data["tasks"].items():
            interval = t.get("reminder_interval")
            if interval:
                _schedule_reminder(app, user_id, int(sid), int(interval))

def make_app() -> Application:
    return Application.builder().token(TOKEN).build()

def main() -> None:
    app = make_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("debugrem", debugrem_cmd))
    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    _restore_reminders(app)
    logger.info("BOOT: app started, reminders restored")

    public_url = os.getenv("PUBLIC_URL")
    if public_url:
        # РЕЖИМ ВЕБХУКОВ. НИКАКОГО POLLING при наличии PUBLIC_URL.
        port = int(os.getenv("PORT", "10000"))
        path = os.getenv("WEBHOOK_PATH", "/hook")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=path,
            webhook_url=f"{public_url}{path}",
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # Локальный режим (например, на твоём ПК)
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
