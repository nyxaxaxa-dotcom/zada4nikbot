# bot.py
# Требуется: python-telegram-bot[webhooks] >= 21  (ставится из requirements.txt)

import json
import os
from pathlib import Path
from typing import Dict, Any, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)

TOKEN = os.getenv("TG_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан TG_BOT_TOKEN в окружении.")

REM_OPTIONS = {"1": 60 * 60, "3": 3 * 60 * 60, "6": 6 * 60 * 60}

def _user_file(user_id: int) -> Path:
    return DATA_DIR / f"{user_id}.json"

def _ensure_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    data.setdefault("seq", 0)
    data.setdefault("tasks", {})
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

def progress_bar(done: int, total: int, width: int = 10) -> str:
    total = max(total, 1)
    ratio = min(max(done / total, 0.0), 1.0)
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)

def task_line(t: Dict[str, Any]) -> str:
    bar = progress_bar(t["done"], t["total"])
    rem = ""
    interval = t.get("reminder_interval")
    if interval:
        mp = {REM_OPTIONS["1"]: "1ч", REM_OPTIONS["3"]: "3ч", REM_OPTIONS["6"]: "6ч"}
        rem = f" • напоминание: {mp.get(interval, str(interval)+'с')}"
    return f"{t['name']}  {bar}  {t['done']}/{t['total']}{rem}"

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Задача", callback_data="ui:new")],
        [InlineKeyboardButton("📋 Мои задачи", callback_data="ui:list")]
    ])

def reminder_menu_kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 1 час", callback_data=f"t:rem1:{tid}"),
            InlineKeyboardButton("🔔 3 часа", callback_data=f"t:rem3:{tid}"),
            InlineKeyboardButton("🔔 6 часов", callback_data=f"t:rem6:{tid}"),
        ],
        [InlineKeyboardButton("🔕 Отключить", callback_data=f"t:remoff:{tid}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"t:open:{tid}")]
    ])

def task_kb(task_id: int, t: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("＋1", callback_data=f"t:+:{task_id}"),
            InlineKeyboardButton("－1", callback_data=f"t:-:{task_id}")
        ],
        [
            InlineKeyboardButton("📝 Переименовать", callback_data=f"t:ren:{task_id}"),
            InlineKeyboardButton("🔢 Задать шаги", callback_data=f"t:set:{task_id}")
        ],
        [
            InlineKeyboardButton("♻️ Сброс", callback_data=f"t:rst:{task_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"t:del:{task_id}")
        ],
        [
            InlineKeyboardButton("🔔 Напоминание", callback_data=f"t:rem:{task_id}"),
            InlineKeyboardButton("✅ Закрыть", callback_data=f"t:close:{task_id}")
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="ui:list")],
        [
            InlineKeyboardButton("➕ Новая", callback_data="ui:new"),
            InlineKeyboardButton("📋 Список", callback_data="ui:list")
        ]
    ]
    if t["done"] >= t["total"]:
        buttons.insert(0, [InlineKeyboardButton("✅ Готово", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)

def parse_new_payload(text: str) -> Tuple[str, int]:
    parts = text.strip().rsplit("|", 1)
    name = parts[0].strip()
    total = 5
    if len(parts) == 2:
        try:
            total = max(1, int(parts[1].strip()))
        except Exception:
            total = 5
    return name, total

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

def _job_name(user_id: int, tid: int) -> str:
    return f"rem:{user_id}:{tid}"

def _cancel_reminder(app: Application, user_id: int, tid: int) -> None:
    for job in app.job_queue.get_jobs_by_name(_job_name(user_id, tid)):
        job.schedule_removal()

def _schedule_reminder(app: Application, user_id: int, tid: int, interval: int) -> None:
    _cancel_reminder(app, user_id, tid)
    app.job_queue.run_repeating(
        reminder_tick,
        interval=interval,
        first=interval,
        name=_job_name(user_id, tid),
        data={"user_id": user_id, "tid": tid},
    )

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
            InlineKeyboardButton("🔔 +1ч", callback_data=f"t:rem1:{tid}"),
            InlineKeyboardButton("🔔 +3ч", callback_data=f"t:rem3:{tid}"),
            InlineKeyboardButton("🔔 +6ч", callback_data=f"t:rem6:{tid}"),
        ],
        [
            InlineKeyboardButton("Открыть карточку", callback_data=f"t:open:{tid}"),
            InlineKeyboardButton("🔕 Выкл", callback_data=f"t:remoff:{tid}"),
        ],
    ])
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
    except Exception:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = None
    text = (
        "Привет! Это минималистичный трекер задач для СДВГ в Telegram.\n"
        "Создавай задачи с шагами, отмечай прогресс кнопками, смотри список и статистику.\n\n"
        "Команды:\n"
        "/new Название |5 — создать задачу с 5 шагами\n"
        "/list — показать список\n"
        "/stats — показать статистику"
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

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    raw = " ".join(context.args) if context.args else ""
    if not raw:
        context.user_data["awaiting"] = {"mode": "new"}
        await update.message.reply_text(
            "Введи название задачи. Можно добавить количество шагов, например: Монтаж подкаста |7",
            reply_markup=main_menu_kb()
        )
        return
    name, total = parse_new_payload(raw)
    data["seq"] += 1
    tid = data["seq"]
    data["tasks"][str(tid)] = {"id": tid, "name": name, "done": 0, "total": total, "reminder_interval": None}
    save_tasks(user_id, data)
    await update.message.reply_text(
        f"Создано: {task_line(data['tasks'][str(tid)])}",
        reply_markup=task_kb(tid, data["tasks"][str(tid)])
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    opened = len(data["tasks"])
    closed = data["stats"]["closed"]
    if not data["tasks"]:
        txt = f"Пока нет задач. Открытых {opened}, закрытых {closed}.\nНажми «➕ Задача» или команда /new Название |5"
        if update.message:
            await update.message.reply_text(txt, reply_markup=main_menu_kb())
        else:
            await safe_edit(update.callback_query, txt, reply_markup=main_menu_kb())
        return
    lines = [f"Открытых: {opened} | Закрытых: {closed}", ""]
    keyboard = []
    for sid, t in sorted(data["tasks"].items(), key=lambda x: int(x[0])):
        tid = int(sid)
        lines.append(f"{tid}. {task_line(t)}")
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
        await safe_edit(
            q,
            "Введи название новой задачи. Можно так: Сценарий выпуска |6",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="ui:menu")]])
        )
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
        await safe_edit(q, f"Напоминания для: {t['name']}\nВыбери интервал или отключи.", reply_markup=reminder_menu_kb(tid))
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
    if action not in {"rem1", "rem3", "rem6", "remoff"} and not t and action not in {"close"}:
        await safe_edit(q, "Эта задача уже отсутствует.", reply_markup=main_menu_kb())
        return

    if action in {"rem1", "rem3", "rem6"}:
        interval = REM_OPTIONS[action[-1]]
        if t:
            t["reminder_interval"] = interval
            save_tasks(user_id, data)
        _schedule_reminder(context.application, user_id, tid, interval)
        await safe_edit(q, f"Напоминание установлено: каждые {action[-1]}ч.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к задаче", callback_data=f"t:open:{tid}")],
            [InlineKeyboardButton("🔕 Отключить", callback_data=f"t:remoff:{tid}")]
        ]))
        return

    if action == "remoff":
        if t:
            t["reminder_interval"] = None
            save_tasks(user_id, data)
        _cancel_reminder(context.application, user_id, tid)
        await safe_edit(q, "Напоминания отключены.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к задаче", callback_data=f"t:open:{tid}")],
        ]))
        return

    if action == "open":
        await safe_edit(q, task_line(t), reply_markup=task_kb(tid, t))
        return

    if action == "+":
        t["done"] = min(t["done"] + 1, t["total"])
    elif action == "-":
        t["done"] = max(t["done"] - 1, 0)
    elif action == "ren":
        context.user_data["awaiting"] = {"mode": "rename", "id": tid}
        await safe_edit(q, "Введи новое название задачи:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"t:open:{tid}")]
        ]))
        return
    elif action == "set":
        context.user_data["awaiting"] = {"mode": "settotal", "id": tid}
        await safe_edit(q, "Введи новое количество шагов (целое число ≥ 1):", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"t:open:{tid}")]
        ]))
        return
    elif action == "rst":
        t["done"] = 0
    elif action == "del":
        _cancel_reminder(context.application, user_id, tid)
        del data["tasks"][str(tid)]
        save_tasks(user_id, data)
        await list_cmd(update, context)
        return
    elif action == "close":
        _cancel_reminder(context.application, user_id, tid)
        del data["tasks"][str(tid)]
        data["stats"]["closed"] = int(data["stats"].get("closed", 0)) + 1
        save_tasks(user_id, data)
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
            name, total = parse_new_payload(update.message.text)
            data["seq"] += 1
            tid = data["seq"]
            data["tasks"][str(tid)] = {"id": tid, "name": name, "done": 0, "total": total, "reminder_interval": None}
            save_tasks(user_id, data)
            context.user_data["awaiting"] = None
            await update.message.reply_text(
                f"Создано: {task_line(data['tasks'][str(tid)])}",
                reply_markup=task_kb(tid, data["tasks"][str(tid)])
            )
            return
        if mode == "rename":
            tid = int(awaiting["id"])
            t = data["tasks"].get(str(tid))
            if t:
                t["name"] = update.message.text.strip() or t["name"]
                save_tasks(user_id, data)
                await update.message.reply_text(task_line(t), reply_markup=task_kb(tid, t))
            context.user_data["awaiting"] = None
            return
        if mode == "settotal":
            tid = int(awaiting["id"])
            t = data["tasks"].get(str(tid))
            if t:
                try:
                    new_total = max(1, int(update.message.text.strip()))
                    t["total"] = new_total
                    if t["done"] > new_total:
                        t["done"] = new_total
                    save_tasks(user_id, data)
                    await update.message.reply_text(task_line(t), reply_markup=task_kb(tid, t))
                except Exception:
                    await update.message.reply_text("Нужно целое число ≥ 1. Попробуй ещё раз.")
                    return
            context.user_data["awaiting"] = None
            return
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_kb())

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, BadRequest) and "Message is not modified" in str(err):
        return
    print("ERROR:", err)

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
    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    _restore_reminders(app)

    public_url = os.getenv("PUBLIC_URL")
    if public_url:
        port = int(os.getenv("PORT", "8080"))
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
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
