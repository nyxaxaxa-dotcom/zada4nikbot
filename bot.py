git --version
# bot.py
# Требуется: python-telegram-bot >= 21  (pip install "python-telegram-bot>=21,<22")

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

DATA_DIR = Path(".venv/data")
DATA_DIR.mkdir(exist_ok=True)

# Твой токен (после теста лучше /revoke и заменить)
TOKEN = "8294959063:AAFEriM_sicn3-GNGXaR2WmRdp8M6bSaN_M"

def _user_file(user_id: int) -> Path:
    return DATA_DIR / f"{user_id}.json"

def load_tasks(user_id: int) -> Dict[str, Any]:
    fp = _user_file(user_id)
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return {"seq": 0, "tasks": {}}
    return {"seq": 0, "tasks": {}}

def save_tasks(user_id: int, data: Dict[str, Any]) -> None:
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
    return f"{t['name']}  {bar}  {t['done']}/{t['total']}"

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Задача", callback_data="ui:new")],
        [InlineKeyboardButton("📋 Мои задачи", callback_data="ui:list")]
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
        [InlineKeyboardButton("⬅️ Назад", callback_data="ui:list")],
        # Новый ряд для быстрого добавления следующей задачи
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = None
    text = (
        "Привет! Это минималистичный трекер задач для СДВГ в Telegram.\n"
        "Создавай задачи с шагами, отмечай прогресс кнопками, смотри общий список.\n\n"
        "Команды:\n"
        "/new Название |5 — создать задачу с 5 шагами (по умолчанию 5)\n"
        "/list — показать список задач\n\n"
        "Или просто пользуйся кнопками ниже."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_kb())
    else:
        await safe_edit(update.callback_query, text, reply_markup=main_menu_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)

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
    data["tasks"][str(tid)] = {"id": tid, "name": name, "done": 0, "total": total}
    save_tasks(user_id, data)

    await update.message.reply_text(
        f"Создано: {task_line(data['tasks'][str(tid)])}",
        reply_markup=task_kb(tid, data["tasks"][str(tid)])
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    data = load_tasks(user_id)
    if not data["tasks"]:
        txt = "Пока нет задач. Нажми «➕ Задача» или команда /new Название |5"
        if update.message:
            await update.message.reply_text(txt, reply_markup=main_menu_kb())
        else:
            await safe_edit(update.callback_query, txt, reply_markup=main_menu_kb())
        return

    lines = []
    keyboard = []
    for sid, t in sorted(data["tasks"].items(), key=lambda x: int(x[0])):
        tid = int(sid)
        lines.append(f"{tid}. {task_line(t)}")
        keyboard.append([InlineKeyboardButton(f"Открыть: {t['name']}", callback_data=f"t:open:{tid}")])

    text = "Мои задачи:\n" + "\n".join(lines)
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

    try:
        _, action, sid = q.data.split(":")
        tid = int(sid)
    except Exception:
        return

    t = data["tasks"].get(str(tid))
    if not t:
        await safe_edit(q, "Эта задача уже отсутствует.", reply_markup=main_menu_kb())
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
        del data["tasks"][str(tid)]
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
            data["tasks"][str(tid)] = {"id": tid, "name": name, "done": 0, "total": total}
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

def make_app() -> Application:
    return Application.builder().token(TOKEN).build()

def main() -> None:
    app = make_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    public_url = os.getenv("PUBLIC_URL")
    if public_url:
        port = int(os.getenv("PORT", "8080"))
        path = os.getenv("WEBHOOK_PATH", f"/{TOKEN}")
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
