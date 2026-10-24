"""
ExternalNotifier — уведомления во внешние мессенджеры (плагин для FunPay Cardinal v0.1.17+)

📡 Дублирует важные события магазина (новые заказы, сообщения покупателей,
выдачи товара) в Discord, Slack или любой другой сервис через обычный
вебхук. Так вы узнаете о заказе даже тогда, когда Telegram недоступен
или уведомления Кардинала отключены.

Возможности:
 • 3 формата вебхука: Discord (content), Slack (text), Generic JSON.
 • Раздельные переключатели событий: 🛒 новый заказ, 💬 сообщение, ✅ выдача.
 • Очередь отправки на диске — события не теряются при рестарте и
   временной недоступности вебхука (до 3 попыток на событие).
 • Кнопка «🧪 Тест» — мгновенная проверка вебхука.
 • Счётчики доставленных и потерянных уведомлений.
 • Текст уведомления автоматически режется под лимиты площадки.

Команды Telegram:
 • /notifier — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Автор: yungspider
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    import requests
except Exception:
    requests = None

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.ExternalNotifier")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "ExternalNotifier (Discord / Slack Webhook Alerts)"
VERSION = "1.0.0"
DESCRIPTION = ("Дублирует события магазина (новые заказы, сообщения покупателей, выдачи) "
               "в Discord/Slack/любой сервис через вебхук. Очередь переживает рестарт, "
               "ретраи, тест-кнопка. Команда: /notifier")
CREDITS = "yungspider"
UUID = "4ea9220f-03cb-46ae-9f96-65668aa242a1"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "external_notifier")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

FORMATS = ("discord", "slack", "json")
TEXT_LIMITS = {"discord": 1900, "slack": 2900, "json": 3900}
MAX_TRIES = 3
QUEUE_LIMIT = 200
LOOP_SLEEP = 15
PRUNE_AFTER = 3600          # события старше часа уже не актуальны

DEFAULT_SETTINGS = {
    "enabled": True,
    "url": "",              # адрес вебхука
    "format": "discord",    # discord | slack | json
    "events": {"new_order": True, "new_message": True, "delivery": True},
    "queue": [],            # [{"ts","kind","text","tries"}]
    "stats": {"sent": 0, "failed": 0},
}

EVENT_TITLES = {
    "new_order": "🛒 Новый заказ",
    "new_message": "💬 Сообщение покупателя",
    "delivery": "✅ Товар выдан",
    "test": "🧪 Тест вебхука",
}

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "en:home"
CB_TOG = "en:tog"
CB_URL = "en:url"
CB_FMT = "en:fmt"                 # + :<format>
CB_EV = "en:ev"                   # + :<event>
CB_TEST = "en:test"
CB_CLEAR = "en:clear"
CB_NOOP = "en:noop"
ST_URL = "en:st:url"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_send_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("ExternalNotifier: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["enabled"] = bool(settings["enabled"])
        if not isinstance(settings["url"], str):
            settings["url"] = ""
        settings["url"] = settings["url"].strip()
        if settings["format"] not in FORMATS:
            settings["format"] = DEFAULT_SETTINGS["format"]
        if not isinstance(settings["events"], dict):
            settings["events"] = dict(DEFAULT_SETTINGS["events"])
        for ev in DEFAULT_SETTINGS["events"]:
            settings["events"][ev] = bool(settings["events"].get(ev, True))
        if not isinstance(settings["queue"], list):
            settings["queue"] = []
        if not isinstance(settings["stats"], dict):
            settings["stats"] = {"sent": 0, "failed": 0}
        settings["stats"].setdefault("sent", 0)
        settings["stats"].setdefault("failed", 0)
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("ExternalNotifier: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def is_valid_url(url: str) -> bool:
    """Простая валидация адреса вебхука: только http/https, без пробелов."""
    url = str(url or "").strip()
    if not url or " " in url:
        return False
    return url.startswith("http://") or url.startswith("https://")


def build_payload(fmt: str, text: str) -> dict:
    """Собирает JSON-тело вебхук-запроса под формат площадки (текст режется под лимит)."""
    fmt = fmt if fmt in FORMATS else "json"
    limit = TEXT_LIMITS.get(fmt, 3900)
    text = str(text or "")[:limit]
    if fmt == "discord":
        return {"content": text}
    if fmt == "slack":
        return {"text": text}
    return {"text": text, "ts": int(time.time())}


def _fmt_order(order) -> str:
    oid = getattr(order, "id", "?")
    desc = getattr(order, "description", "") or "лот"
    price = getattr(order, "price", None)
    buyer = getattr(order, "buyer_username", "") or "?"
    price_s = f" за {price} ₽" if price is not None else ""
    return f"{EVENT_TITLES['new_order']}: #{oid} — «{desc}»{price_s} (покупатель: {buyer})"


def _fmt_message(msg) -> str:
    author = getattr(msg, "author", "") or "покупатель"
    text = str(getattr(msg, "text", "") or "(медиа)").replace("\n", " ")[:300]
    return f"{EVENT_TITLES['new_message']} от {author}: {text}"


def _fmt_delivery(order) -> str:
    oid = getattr(order, "id", "?")
    buyer = getattr(order, "buyer_username", "") or "?"
    return f"{EVENT_TITLES['delivery']}: #{oid} (покупатель: {buyer})"


def _post(url: str, payload: dict, timeout: int = 10) -> bool:
    """Отправка вебхука. Возвращает True при HTTP 2xx."""
    if requests is None:
        logger.error("ExternalNotifier: модуль requests недоступен — отправка невозможна.")
        return False
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        return 200 <= int(getattr(r, "status_code", 0)) < 300
    except Exception:
        logger.debug("ExternalNotifier: ошибка отправки вебхука.", exc_info=True)
        return False


# ══════════════════════════════ ОЧЕРЕДЬ СОБЫТИЙ ═════════════════════════════════
def enqueue(kind: str, text: str, settings: dict | None = None) -> bool:
    """Добавляет событие в очередь. Возвращает True, если добавлено."""
    own = settings is None
    if own:
        settings = _load_settings()
    if not settings["enabled"] or not is_valid_url(settings["url"]):
        return False
    queue = settings["queue"]
    if len(queue) >= QUEUE_LIMIT:      # переполнение — выбрасываем самое старое
        queue.pop(0)
    queue.append({"ts": time.time(), "kind": str(kind), "text": str(text)[:3900], "tries": 0})
    if own:
        _save_settings(settings)
    return True


def _push_event(crd: "Cardinal", kind: str, text: str) -> None:
    """Общая точка входа обработчиков событий: проверка включенности + очередь."""
    try:
        if not _plugin_enabled(crd):
            return
        settings = _load_settings()
        if not settings["events"].get(kind, True):
            return
        if enqueue(kind, text, settings):
            _save_settings(settings)
    except Exception:
        logger.error("ExternalNotifier: ошибка постановки события в очередь.")
        logger.debug("TRACEBACK", exc_info=True)


def new_order_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_NEW_ORDER: уведомление о новом заказе."""
    order = getattr(e, "order", None)
    if order is None:
        return
    _push_event(c, "new_order", _fmt_order(order))


def new_message_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_NEW_MESSAGE: уведомление о сообщении покупателя (свои/системные — пропускаем)."""
    msg = getattr(e, "message", None)
    if msg is None:
        return
    if getattr(msg, "by_bot", False):
        return
    my_id = getattr(c.account, "id", None)
    if my_id is not None and getattr(msg, "author_id", None) == my_id:
        return
    if getattr(msg, "author_id", None) in (0, None):
        return
    _push_event(c, "new_message", _fmt_message(msg))


def post_delivery_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_POST_DELIVERY: уведомление о выдаче товара."""
    if not getattr(e, "delivered", False):
        return
    order = getattr(e, "order", None)
    if order is None:
        return
    _push_event(c, "delivery", _fmt_delivery(order))


def queue_tick(crd: "Cardinal", poster=None, now_ts: float | None = None) -> tuple:
    """
    Один проход очереди: отправляет накопившиеся события.
    poster(url, payload) -> bool (по умолчанию _post).
    Возвращает (отправлено, потеряно).
    """
    poster = poster or _post
    settings = _load_settings()
    now_ts = now_ts if now_ts is not None else time.time()
    if not settings["enabled"] or not is_valid_url(settings["url"]):
        return 0, 0
    url, fmt = settings["url"], settings["format"]
    sent = failed = 0
    changed = False
    new_queue = []
    for item in settings["queue"]:
        if _stop_event.is_set():
            new_queue.append(item)
            continue
        try:
            ts = float(item.get("ts", 0))
            tries = int(item.get("tries", 0))
        except (TypeError, ValueError):
            ts, tries = 0.0, MAX_TRIES
        if now_ts - ts > PRUNE_AFTER:        # устарело — не шлем, считаем потерянным
            failed += 1
            changed = True
            logger.info(f"ExternalNotifier: событие «{item.get('kind')}» устарело (>1 ч) — отброшено.")
            continue
        with _send_lock:
            ok = poster(url, build_payload(fmt, item.get("text", "")))
        if ok:
            sent += 1
            changed = True
        else:
            item["tries"] = tries + 1
            changed = True
            if item["tries"] >= MAX_TRIES:
                failed += 1
                logger.warning(f"ExternalNotifier: событие «{item.get('kind')}» не доставлено "
                               f"после {MAX_TRIES} попыток — отброшено.")
            else:
                new_queue.append(item)
    if changed:
        settings["queue"] = new_queue
        settings["stats"]["sent"] = int(settings["stats"].get("sent", 0)) + sent
        settings["stats"]["failed"] = int(settings["stats"].get("failed", 0)) + failed
        _save_settings(settings)
    return sent, failed


def _send_loop(crd: "Cardinal") -> None:
    while not _stop_event.is_set():
        try:
            queue_tick(crd)
        except Exception:
            logger.error("ExternalNotifier: ошибка цикла отправки.")
            logger.debug("TRACEBACK", exc_info=True)
        _stop_event.wait(LOOP_SLEEP)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    s = _load_settings()
    state = "ВКЛ 🟢" if s["enabled"] else "ВЫКЛ 🔴"
    url = s["url"] or "не задан"
    if len(url) > 60:
        url = url[:57] + "…"
    events = "\n".join(
        f"{'✅' if s['events'].get(ev) else '⛔'} {EVENT_TITLES[ev]}"
        for ev in ("new_order", "new_message", "delivery"))
    return (f"📡 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"События магазина дублируются во внешний сервис через вебхук.\n\n"
            f"Плагин: <b>{state}</b>\n"
            f"🔗 Вебхук: <code>{_escape(url)}</code>\n"
            f"🧩 Формат: <b>{s['format']}</b>\n"
            f"📮 В очереди: <b>{len(s['queue'])}</b>\n"
            f"📊 Доставлено: <b>{s['stats'].get('sent', 0)}</b>, потеряно: <b>{s['stats'].get('failed', 0)}</b>\n\n"
            f"<b>События:</b>\n{events}")


def _home_kb() -> K:
    s = _load_settings()
    kb = K(row_width=2)
    kb.add(B(f"{'🟢 Включено' if s['enabled'] else '🔴 Выключено'}", None, CB_TOG))
    kb.add(B("🔗 Вебхук", None, CB_URL))
    kb.add(*[B(f"{'✅' if s['format'] == f else '·'} {f.capitalize()}", None, f"{CB_FMT}:{f}")
             for f in FORMATS])
    for ev in ("new_order", "new_message", "delivery"):
        kb.add(B(f"{'✅' if s['events'].get(ev) else '⛔'} {EVENT_TITLES[ev].split(' ', 1)[1]}", None, f"{CB_EV}:{ev}"))
    kb.add(B("🧪 Тест", None, CB_TEST))
    kb.add(B("🧹 Очистить очередь", None, CB_CLEAR))
    kb.add(B("🔄 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def toggle_enabled(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["enabled"] = not settings["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"📡 Уведомления: {'ВКЛ' if settings['enabled'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def act_url(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                "🔗 Отправьте <b>адрес вебхука</b> (http/https).\n\n"
                "Discord: Настройки сервера → Интеграции → Вебхуки → Скопировать URL.\n"
                "Slack: Apps → Incoming Webhooks → Add to Slack.\n\n"
                f"Текущий: <code>{_escape(s['url'] or 'не задан')}</code>",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_URL)
            _answer(bot, c)

        def edit_url(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            url = str(m.text or "").strip()
            if not is_valid_url(url):
                bot.reply_to(m, "❌ Это не похоже на адрес вебхука (нужен http:// или https://). Значение не сохранено.")
                return
            settings = _load_settings()
            settings["url"] = url
            _save_settings(settings)
            bot.reply_to(m, "✅ Вебхук сохранен. Нажмите «🧪 Тест» для проверки.", parse_mode="HTML")

        def set_format(c: CallbackQuery) -> None:
            fmt = c.data.split(":")[-1]
            if fmt not in FORMATS:
                return _answer(bot, c, "❌ Неизвестный формат.", alert=True)
            settings = _load_settings()
            settings["format"] = fmt
            _save_settings(settings)
            _answer(bot, c, f"🧩 Формат вебхука: {fmt}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def toggle_event(c: CallbackQuery) -> None:
            ev = c.data.split(":")[-1]
            settings = _load_settings()
            if ev not in settings["events"]:
                return _answer(bot, c, "❌ Неизвестное событие.", alert=True)
            settings["events"][ev] = not settings["events"][ev]
            _save_settings(settings)
            _answer(bot, c, f"{EVENT_TITLES[ev]}: {'ВКЛ' if settings['events'][ev] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def send_test(c: CallbackQuery) -> None:
            settings = _load_settings()
            if not is_valid_url(settings["url"]):
                return _answer(bot, c, "❌ Сначала укажите адрес вебхука.", alert=True)
            _answer(bot, c, "⏳ Отправляю тест…")
            text = f"{EVENT_TITLES['test']}: ExternalNotifier v{VERSION} подключен успешно! 🎉"
            ok = _post(settings["url"], build_payload(settings["format"], text))
            if ok:
                settings["stats"]["sent"] = int(settings["stats"].get("sent", 0)) + 1
                _save_settings(settings)
                _answer(bot, c, "✅ Тест доставлен!")
            else:
                settings["stats"]["failed"] = int(settings["stats"].get("failed", 0)) + 1
                _save_settings(settings)
                _answer(bot, c, "❌ Вебхук не ответил 2xx. Проверьте URL и формат.", alert=True)

        def clear_queue(c: CallbackQuery) -> None:
            settings = _load_settings()
            n = len(settings["queue"])
            settings["queue"] = []
            _save_settings(settings)
            _answer(bot, c, f"🧹 Очередь очищена ({n} событий).")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}") if CBT else False)
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(toggle_enabled, lambda c: c.data == CB_TOG)
        tg.cbq_handler(act_url, lambda c: c.data == CB_URL)
        tg.cbq_handler(set_format, lambda c: c.data.startswith(f"{CB_FMT}:"))
        tg.cbq_handler(toggle_event, lambda c: c.data.startswith(f"{CB_EV}:"))
        tg.cbq_handler(send_test, lambda c: c.data == CB_TEST)
        tg.cbq_handler(clear_queue, lambda c: c.data == CB_CLEAR)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_url, func=lambda m: _state_eq(m, ST_URL))
        tg.msg_handler(cmd_open, commands=["notifier"])

        try:
            crd.add_telegram_commands(UUID, [("notifier", "Уведомления в Discord/Slack через вебхук.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"ExternalNotifier v{VERSION}: Telegram-меню зарегистрировано (команда /notifier).")
    else:
        logger.info(f"ExternalNotifier v{VERSION}: Telegram выключен — очередь работает в фоне.")

    threading.Thread(target=_send_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_NEW_MESSAGE = [new_message_handler]
BIND_TO_POST_DELIVERY = [post_delivery_handler]
BIND_TO_DELETE = on_delete
