"""
Телеграм-бот-менеджер: присылает уведомление о каждой новой заявке из Google-формы.

Как работает: раз в POLL_SECONDS секунд бот читает ответы формы через Google Forms API
и присылает в Telegram короткое уведомление с кнопкой «Открыть заявку».
"""

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# ---------- Настройки из .env ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
FORM_ID_RAW = os.environ["FORM_ID"]
ADMIN_CHAT_IDS = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS", "service_account.json")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))

SCOPES = [
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lead_bot")


def extract_form_id(value: str) -> str:
    """Принимает ID формы или ссылку на редактирование формы."""
    value = value.strip()
    if "/d/e/" in value:
        raise SystemExit(
            "В FORM_ID стоит публичная ссылка (/d/e/...). Нужна ссылка на РЕДАКТИРОВАНИЕ формы: "
            "docs.google.com/forms/d/<ID>/edit"
        )
    match = re.search(r"/forms/d/([a-zA-Z0-9_-]+)", value)
    return match.group(1) if match else value


FORM_ID = extract_form_id(FORM_ID_RAW)


# ---------- Работа с Google Forms ----------
class FormWatcher:
    def __init__(self):
        creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        self.service = build("forms", "v1", credentials=creds, cache_discovery=False)
        self.lock = threading.Lock()
        self.form_title = ""
        self.state = self._load_state()

    # --- состояние: какие ответы уже отправлены ---
    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"initialized": False, "seen": [], "last_time": None}

    def save_state(self):
        self.state["seen"] = self.state["seen"][-2000:]
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark_seen(self, response: dict):
        self.state["seen"].append(response["responseId"])
        t = response.get("lastSubmittedTime") or response.get("createTime")
        if t and (not self.state["last_time"] or t > self.state["last_time"]):
            self.state["last_time"] = t
        self.save_state()

    # --- форма ---
    def load_form(self):
        form = self.service.forms().get(formId=FORM_ID).execute()
        self.form_title = form.get("info", {}).get("title", "")

    # --- ответы ---
    def _list_responses(self) -> list[dict]:
        params = {"formId": FORM_ID, "pageSize": 100}
        if self.state["last_time"]:
            params["filter"] = f"timestamp >= {self.state['last_time']}"
        responses, token = [], None
        while True:
            if token:
                params["pageToken"] = token
            data = self.service.forms().responses().list(**params).execute()
            responses.extend(data.get("responses", []))
            token = data.get("nextPageToken")
            if not token:
                break
        responses.sort(key=lambda r: r.get("lastSubmittedTime") or r.get("createTime", ""))
        return responses

    def fetch_new(self) -> list[dict]:
        """Возвращает новые ответы. При первом запуске старые помечает прочитанными, чтобы не спамить."""
        with self.lock:
            responses = self._list_responses()

            if not self.state["initialized"]:
                for r in responses:
                    self.mark_seen(r)
                self.state["initialized"] = True
                self.save_state()
                log.info("Первый запуск: %s старых ответов пропущено, жду новые", len(responses))
                return []

            seen = set(self.state["seen"])
            return [r for r in responses if r["responseId"] not in seen]


# ---------- Оформление уведомления ----------
def build_message(response: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🔥 <b>Новая заявка на разбор</b>"]
    submitted = response.get("lastSubmittedTime") or response.get("createTime")
    if submitted:
        dt = datetime.fromisoformat(submitted.replace("Z", "+00:00")).astimezone(TIMEZONE)
        lines.append(f"🕐 {dt.strftime('%d.%m.%Y %H:%M')}")

    url = f"https://docs.google.com/forms/d/{FORM_ID}/edit#response={response['responseId']}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📄 Открыть заявку", url=url)]])
    return "\n".join(lines), keyboard


# ---------- Telegram ----------
def is_admin(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id in ADMIN_CHAT_IDS


async def check_and_notify(context: ContextTypes.DEFAULT_TYPE) -> int:
    watcher: FormWatcher = context.application.bot_data["watcher"]
    try:
        new = await asyncio.to_thread(watcher.fetch_new)
    except Exception:
        log.exception("Не получилось прочитать ответы формы")
        return 0

    for response in new:
        text, keyboard = build_message(response)
        try:
            for chat_id in ADMIN_CHAT_IDS:
                await context.bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML,
                    reply_markup=keyboard, disable_web_page_preview=True,
                )
        except Exception:
            log.exception("Не отправилось уведомление, попробую на следующей проверке")
            break
        watcher.mark_seen(response)
        log.info("Отправлена заявка %s", response["responseId"])
    return len(new)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ADMIN_CHAT_IDS:
        await update.message.reply_text(
            f"Твой chat_id: {chat_id}\n\nВпиши его в .env в ADMIN_CHAT_IDS и перезапусти бота."
        )
        return
    if not is_admin(update):
        await update.message.reply_text("Привет! Это служебный бот.")
        return
    watcher: FormWatcher = context.application.bot_data["watcher"]
    await update.message.reply_text(
        f"На связи. Слежу за формой «{watcher.form_title or FORM_ID}», "
        f"проверяю каждые {POLL_SECONDS} сек.\n\n/check проверить прямо сейчас"
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    count = await check_and_notify(context)
    await update.message.reply_text("Новых заявок нет." if count == 0 else f"Новых заявок: {count}")


async def on_startup(app: Application):
    watcher: FormWatcher = app.bot_data["watcher"]
    await asyncio.to_thread(watcher.load_form)
    log.info("Слежу за формой «%s»", watcher.form_title)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
    app.bot_data["watcher"] = FormWatcher()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))

    if ADMIN_CHAT_IDS:
        app.job_queue.run_repeating(check_and_notify, interval=POLL_SECONDS, first=5)
    else:
        log.warning("ADMIN_CHAT_IDS пустой: напиши боту /start, он пришлёт твой chat_id")

    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
