"""
Функциональные тесты плагина external_notifier.py на моках.
Запуск: cd FPC && python3 tests_plugin_external_notifier.py
"""
import importlib.util
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

spec = importlib.util.spec_from_file_location("plugins.external_notifier", "plugins/external_notifier.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.external_notifier"] = plugin
spec.loader.exec_module(plugin)

plugin._send_loop = lambda crd: None  # фоновый поток тестируем через queue_tick

PASS = []
FAIL = []
GOOD_URL = "https://discord.com/api/webhooks/123/abc"


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


def mk_order(oid="AAAA1111", desc="Аккаунт Steam", price=1500.0, buyer="buyer1"):
    return SimpleNamespace(id=oid, description=desc, price=price, buyer_username=buyer)


def mk_msg(author="buyer1", author_id=777, text="Здравствуйте!", by_bot=False, chat_id=101):
    return SimpleNamespace(author=author, author_id=author_id, text=text,
                           by_bot=by_bot, chat_id=chat_id, chat_name=author, type=None)


# ───────────────────────────── моки ─────────────────────────────
class MockAccount:
    def __init__(self):
        self.is_initiated = True
        self.id = 999

    def send_message(self, chat_id, text=None, **kw):
        return SimpleNamespace(id=1)


class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_data(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(str(b.callback_data) for row in kb.keyboard for b in row if b.callback_data)
    except Exception:
        return ""


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notifications = []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, **kwargs):
        self.notifications.append(text)


class MockCardinal:
    def __init__(self, account, telegram):
        self.account = account
        self.telegram = telegram
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True):
    acc = MockAccount()
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg), acc, tg


# ═══════════════════════ ТЕСТ 1: метаданные ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 автор yungspider, название на английском",
      plugin.CREDITS == "yungspider" and plugin.NAME.isascii())
check("1.4 все бинды привязаны",
      plugin.BIND_TO_PRE_INIT == [plugin.init]
      and plugin.BIND_TO_NEW_ORDER == [plugin.new_order_handler]
      and plugin.BIND_TO_NEW_MESSAGE == [plugin.new_message_handler]
      and plugin.BIND_TO_POST_DELIVERY == [plugin.post_delivery_handler])

# ═══════════════════════ ТЕСТ 2: URL и payload ═══════════════════════
check("2.1 валидные URL", plugin.is_valid_url(GOOD_URL) and plugin.is_valid_url("http://x.y/z"))
check("2.2 невалидные URL", not plugin.is_valid_url("") and not plugin.is_valid_url("ftp://x")
      and not plugin.is_valid_url("https://a b") and not plugin.is_valid_url(None))

p = plugin.build_payload("discord", "hello")
check("2.3 discord payload → content", p == {"content": "hello"})
p = plugin.build_payload("slack", "hello")
check("2.4 slack payload → text", p == {"text": "hello"})
p = plugin.build_payload("json", "hello")
check("2.5 json payload → text+ts", p["text"] == "hello" and isinstance(p["ts"], int))

long = "x" * 5000
check("2.6 discord режется под лимит 1900",
      len(plugin.build_payload("discord", long)["content"]) == plugin.TEXT_LIMITS["discord"])
check("2.7 неизвестный формат → json", "ts" in plugin.build_payload("weird", "t"))

# ═══════════════════════ ТЕСТ 3: форматирование событий ═══════════════════════
t = plugin._fmt_order(mk_order())
check("3.1 текст заказа содержит id, лот, цену, покупателя",
      "AAAA1111" in t and "Аккаунт Steam" in t and "1500" in t and "buyer1" in t)
t = plugin._fmt_message(mk_msg(text="Есть в наличии?"))
check("3.2 текст сообщения содержит автора и текст", "buyer1" in t and "Есть в наличии?" in t)
t = plugin._fmt_delivery(mk_order())
check("3.3 текст выдачи содержит id заказа", "AAAA1111" in t and "buyer1" in t)

# ═══════════════════════ ТЕСТ 4: очередь (enqueue) ═══════════════════════
reset_settings(url=GOOD_URL)
check("4.1 enqueue при валидном URL → True", plugin.enqueue("test", "hello") is True)
q = plugin._load_settings()["queue"]
check("4.2 событие в очереди с tries=0", len(q) == 1 and q[0]["text"] == "hello" and q[0]["tries"] == 0)

reset_settings(url="")
check("4.3 без URL → False", plugin.enqueue("test", "x") is False)
reset_settings(url=GOOD_URL, enabled=False)
check("4.4 выключенный плагин → False", plugin.enqueue("test", "x") is False)

reset_settings(url=GOOD_URL, queue=[{"ts": time.time(), "kind": "k", "text": f"t{i}", "tries": 0}
                                    for i in range(plugin.QUEUE_LIMIT)])
plugin.enqueue("test", "overflow")
q = plugin._load_settings()["queue"]
check("4.5 переполнение очереди → старое выбрасывается",
      len(q) == plugin.QUEUE_LIMIT and q[-1]["text"] == "overflow" and q[0]["text"] == "t1")

# ═══════════════════════ ТЕСТ 5: обработчики событий ═══════════════════════
crd, acc, tg = make_crd(with_tg=False)
reset_settings(url=GOOD_URL)
plugin.new_order_handler(crd, SimpleNamespace(order=mk_order()))
q = plugin._load_settings()["queue"]
check("5.1 новый заказ → событие в очереди", len(q) == 1 and q[0]["kind"] == "new_order"
      and "AAAA1111" in q[0]["text"])

plugin.new_message_handler(crd, SimpleNamespace(message=mk_msg()))
check("5.2 сообщение покупателя → в очереди",
      plugin._load_settings()["queue"][-1]["kind"] == "new_message")

plugin.new_message_handler(crd, SimpleNamespace(message=mk_msg(by_bot=True)))
plugin.new_message_handler(crd, SimpleNamespace(message=mk_msg(author_id=999)))   # свой
plugin.new_message_handler(crd, SimpleNamespace(message=mk_msg(author_id=0)))     # системный
check("5.3 бот/свои/системные сообщения игнорируются",
      len(plugin._load_settings()["queue"]) == 2)

plugin.post_delivery_handler(crd, SimpleNamespace(delivered=True, order=mk_order(oid="BB22")))
check("5.4 выдача → событие delivery",
      plugin._load_settings()["queue"][-1]["kind"] == "delivery")
plugin.post_delivery_handler(crd, SimpleNamespace(delivered=False, order=mk_order(oid="CC33")))
check("5.5 выдача не состоялась → игнор",
      all(i["kind"] != "delivery" or "CC33" not in i["text"] for i in plugin._load_settings()["queue"]))

reset_settings(url=GOOD_URL, events={"new_order": False, "new_message": True, "delivery": True})
plugin.new_order_handler(crd, SimpleNamespace(order=mk_order(oid="DD44")))
check("5.6 отключенное событие не попадает в очередь",
      all("DD44" not in i["text"] for i in plugin._load_settings()["queue"]))

crd.plugins[plugin.UUID].enabled = False
plugin.new_order_handler(crd, SimpleNamespace(order=mk_order(oid="EE55")))
check("5.7 выключенный в ПУ плагин молчит",
      all("EE55" not in i["text"] for i in plugin._load_settings()["queue"]))
crd.plugins[plugin.UUID].enabled = True

plugin.new_order_handler(crd, SimpleNamespace())          # без order
plugin.new_message_handler(crd, SimpleNamespace())        # без message
check("5.8 пустые события не ломают плагин", True)

# ═══════════════════════ ТЕСТ 6: отправка очереди (queue_tick) ═══════════════════════
now = time.time()
posts = []


def ok_poster(url, payload):
    posts.append((url, payload))
    return True


def fail_poster(url, payload):
    posts.append((url, payload))
    return False


reset_settings(url=GOOD_URL, format="discord", queue=[
    {"ts": now, "kind": "new_order", "text": "Событие 1", "tries": 0},
    {"ts": now, "kind": "delivery", "text": "Событие 2", "tries": 0},
])
sent, failed = plugin.queue_tick(crd, poster=ok_poster, now_ts=now)
check("6.1 оба события отправлены", (sent, failed) == (2, 0))
check("6.2 очередь пуста, статистика sent=2",
      plugin._load_settings()["queue"] == [] and plugin._load_settings()["stats"]["sent"] == 2)
check("6.3 poster получил discord-payload",
      posts and posts[0][1] == {"content": "Событие 1"} and posts[0][0] == GOOD_URL)

posts.clear()
reset_settings(url=GOOD_URL, queue=[{"ts": now, "kind": "k", "text": "t", "tries": 0}])
sent, failed = plugin.queue_tick(crd, poster=fail_poster, now_ts=now)
check("6.4 ошибка → событие остается, tries=1",
      (sent, failed) == (0, 0) and plugin._load_settings()["queue"][0]["tries"] == 1)

reset_settings(url=GOOD_URL, queue=[{"ts": now, "kind": "k", "text": "t", "tries": plugin.MAX_TRIES - 1}])
sent, failed = plugin.queue_tick(crd, poster=fail_poster, now_ts=now)
check("6.5 после MAX_TRIES ошибок событие отброшено, failed=1",
      (sent, failed) == (0, 1) and plugin._load_settings()["queue"] == []
      and plugin._load_settings()["stats"]["failed"] == 1)

reset_settings(url=GOOD_URL, queue=[{"ts": now - plugin.PRUNE_AFTER - 10, "kind": "k", "text": "t", "tries": 0}])
posts.clear()
sent, failed = plugin.queue_tick(crd, poster=ok_poster, now_ts=now)
check("6.6 устаревшее (>1 ч) событие отброшено без отправки",
      (sent, failed) == (0, 1) and posts == [] and plugin._load_settings()["queue"] == [])

reset_settings(url="", queue=[{"ts": now, "kind": "k", "text": "t", "tries": 0}])
sent, failed = plugin.queue_tick(crd, poster=ok_poster, now_ts=now)
check("6.7 без URL очередь не обрабатывается", (sent, failed) == (0, 0)
      and len(plugin._load_settings()["queue"]) == 1)

plugin._stop_event.set()
reset_settings(url=GOOD_URL, queue=[{"ts": now, "kind": "k", "text": "t", "tries": 0}])
sent, failed = plugin.queue_tick(crd, poster=ok_poster, now_ts=now)
check("6.8 stop_event → очередь не трогается",
      (sent, failed) == (0, 0) and len(plugin._load_settings()["queue"]) == 1)
plugin._stop_event.clear()
reset_settings()

# ═══════════════════════ ТЕСТ 7: Telegram UI ═══════════════════════
crd, acc, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_settings(url=GOOD_URL)
check("7.1 команда /notifier зарегистрирована",
      any(kw.get("commands") == ["notifier"] for _, kw in tg.msg_handlers))
check("7.2 add_telegram_commands вызван", crd.registered_commands
      and crd.registered_commands[0][0] == "notifier")


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id), text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("7.3 страница настроек открывает меню", name == "open_home" and "ExternalNotifier" in last_text())
check("7.4 в меню URL и статистика", GOOD_URL[:40] in last_text() and "Доставлено" in last_text())

dispatch_cb(plugin.CB_TOG)
check("7.5 вкл/выкл переключается", plugin._load_settings()["enabled"] is False)
dispatch_cb(plugin.CB_TOG)

dispatch_cb(plugin.CB_URL)
check("7.6 «Вебхук» ставит состояние",
      tg.user_states.get((111, 222), {}).get("state") == plugin.ST_URL)
dispatch_msg("не url")
check("7.7 невалидный URL отклоняется", "❌" in bot.replies[-1]
      and plugin._load_settings()["url"] == GOOD_URL)
dispatch_cb(plugin.CB_URL)
dispatch_msg("https://hooks.slack.com/services/T00/B00/XYZ")
check("7.8 валидный URL сохранен",
      plugin._load_settings()["url"] == "https://hooks.slack.com/services/T00/B00/XYZ")

dispatch_cb(f"{plugin.CB_FMT}:slack")
check("7.9 формат slack выбран", plugin._load_settings()["format"] == "slack")
dispatch_cb(f"{plugin.CB_FMT}:bogus")
check("7.10 неизвестный формат отклоняется", plugin._load_settings()["format"] == "slack"
      and "❌" in (bot.answers[-1][1] or ""))

dispatch_cb(f"{plugin.CB_EV}:new_order")
check("7.11 событие new_order переключилось",
      plugin._load_settings()["events"]["new_order"] is False)
dispatch_cb(f"{plugin.CB_EV}:new_order")
dispatch_cb(f"{plugin.CB_EV}:bogus")
check("7.12 неизвестное событие отклоняется", "❌" in (bot.answers[-1][1] or ""))

# тест-кнопка (мокаем _post, чтобы не ходить в сеть)
real_post = plugin._post
plugin._post = lambda url, payload, timeout=10: True
dispatch_cb(f"{plugin.CB_URL}")
dispatch_msg(GOOD_URL)
dispatch_cb(plugin.CB_TEST)
check("7.13 «Тест» при успехе → ✅ и sent+1",
      "✅" in (bot.answers[-1][1] or "") and plugin._load_settings()["stats"]["sent"] >= 1)
plugin._post = lambda url, payload, timeout=10: False
dispatch_cb(plugin.CB_TEST)
check("7.14 «Тест» при ошибке → ❌ и failed+1",
      "❌" in (bot.answers[-1][1] or "") and plugin._load_settings()["stats"]["failed"] >= 1)
plugin._post = real_post

reset_settings(url="")
dispatch_cb(plugin.CB_TEST)
check("7.15 «Тест» без URL → просьба указать вебхук", "❌" in (bot.answers[-1][1] or ""))

reset_settings(url=GOOD_URL, queue=[{"ts": time.time(), "kind": "k", "text": "t", "tries": 0}])
dispatch_cb(plugin.CB_CLEAR)
check("7.16 «Очистить очередь» работает", plugin._load_settings()["queue"] == [])

dispatch_cb(plugin.CB_NOOP)
check("7.17 noop отвечает", True)

# ═══════════════════════ ТЕСТ 8: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"enabled": 1, "url": 123, "format": "telegram", "events": "x",
               "queue": {}, "stats": None}, f)
s = plugin._load_settings()
check("8.1 битые значения нормализуются", s["enabled"] is True and s["url"] == ""
      and s["format"] == "discord" and s["events"]["new_order"] is True
      and s["queue"] == [] and s["stats"]["sent"] == 0)
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("8.2 битый JSON → дефолты", s["format"] == "discord" and s["queue"] == [])
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
