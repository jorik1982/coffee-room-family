#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coffee Room Family — сервер v4: сайт + AI-администратор + CRM загрузки зала
  • ЦЕПОЧКА ПРОВАЙДЕРОВ: Groq → Gemini (авто-переключение при лимитах)
  • стриминг (SSE), SSL-фолбэк, память гостя (sid), дата в инструкции
  • CRM: каждая [ЗАЯВКА] автоматически сохраняется в bookings.json
  • АИДА ВИДИТ ЗАГРУЗКУ ЗАЛА: занятые слоты подмешиваются в инструкцию,
    на переполненное время она честно отвечает «столиков нет»
  • /crm — дашборд менеджера (PIN, по умолчанию 1234, см. env CRM_PIN)

Переменные окружения:
  GROQ_API_KEY / GEMINI_API_KEY — ключи провайдеров (порядок = приоритет)
  CRM_PIN      — пин-код дашборда (по умолчанию 1234)
  CRM_TABLES   — столов в зале на один часовой слот (по умолчанию 6)
  PORT         — порт (8080)
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8080"))
PROMPT_FILE = os.path.join(SITE, "system_prompt.md")
DB_FILE = os.path.join(SITE, "bookings.json")

CRM_PIN = os.environ.get("CRM_PIN", "1234")
CRM_TABLES = int(os.environ.get("CRM_TABLES", "6"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
THINKING = os.environ.get("AI_THINKING", "low")

PROVIDERS = []
if GROQ_API_KEY:
    PROVIDERS.append({"name": "groq", "provider": "openai", "key": GROQ_API_KEY,
                      "base": "https://api.groq.com/openai/v1", "model": GROQ_MODEL})
if GEMINI_API_KEY:
    PROVIDERS.append({"name": "gemini", "provider": "gemini", "key": GEMINI_API_KEY,
                      "model": GEMINI_MODEL})

MSG_LIMIT = 2000
HIST_LIMIT = 16
SAFE_RPM = 25
_rpm_window = [0.0, 0]

BRANCHES = ("ул. Бауыржана Момышулы, 11", "ул. Мангелик Ел, 14")
HOURS = list(range(10, 23))  # 10:00–22:00 слоты

# ============================== CRM: БАЗА БРОНЕЙ ==============================

_DB_LOCK = threading.Lock()


def _norm_branch(b: str) -> str:
    b = (b or "").lower()
    if "мангелик" in b or "14" in b:
        return BRANCHES[1]
    return BRANCHES[0]


def _norm_date(d: str) -> str:
    """Приводим дату к дд.мм."""
    d = (d or "").strip()
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})", d)
    if m:
        return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}"
    return d[:5] if len(d) >= 5 else d


def _norm_slot(t: str) -> str:
    m = re.search(r"(\d{1,2})", t or "")
    if not m:
        return "19:00"
    h = max(HOURS[0], min(HOURS[-1], int(m.group(1))))
    return f"{h:02d}:00"


def _load_db():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"next_id": 1, "bookings": []}


def _save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)


def add_booking(name, phone, branch, date, time_, guests, prefs="", source="AI-чат", kind="столик"):
    """Сохраняем бронь. Возвращает запись."""
    with _DB_LOCK:
        db = _load_db()
        rec = {
            "id": db["next_id"],
            "created": datetime.now().strftime("%d.%m %H:%M"),
            "name": (name or "-").strip()[:60],
            "phone": (phone or "").strip()[:30],
            "branch": _norm_branch(branch),
            "date": _norm_date(date),
            "slot": _norm_slot(time_),
            "time": (time_ or "").strip()[:12],
            "guests": str(guests or "-").strip()[:40],
            "prefs": (prefs or "").strip()[:160],
            "kind": kind,
            "source": source,
            "status": "новая",
        }
        db["bookings"].append(rec)
        db["next_id"] += 1
        _save_db(db)
        return rec


def _active(db):
    return [b for b in db["bookings"] if b.get("status") != "отменена"]


def slot_load(branch_dd, date_dd, slot):
    """Сколько столов занято в слоте."""
    with _DB_LOCK:
        db = _load_db()
        return sum(1 for b in _active(db)
                   if b["branch"] == branch_dd and b["date"] == date_dd and b["slot"] == slot)


def availability_text():
    """Компактная сводка загрузки для инструкции AI: только непустые слоты на 3 дня."""
    with _DB_LOCK:
        db = _load_db()
    now = datetime.now()
    days = [(now + timedelta(days=i)).strftime("%d.%m") for i in range(7)]
    lines = []
    for br in BRANCHES:
        short = "Момышулы 11" if "Момышулы" in br else "Мангелик Ел 14"
        busy = []
        for d in days:
            for h in HOURS:
                slot = f"{h:02d}:00"
                n = sum(1 for b in _active(db)
                        if b["branch"] == br and b["date"] == d and b["slot"] == slot)
                if n >= CRM_TABLES:
                    busy.append(f"{d} {slot} — ПОЛНО ({n}/{CRM_TABLES})")
                elif n > 0:
                    busy.append(f"{d} {slot} — {n}/{CRM_TABLES}")
        lines.append(f"{short}: " + ("; ".join(busy) if busy else "всё свободно"))
    return ("\n[ЗАГРУЗКА ЗАЛА (занято/всего столов на час), ближайшие 7 дней — "
            "проверяй её ПЕРЕД подтверждением времени:\n"
            + "\n".join(lines)
            + f"\nЕсли слот ПОЛНО — столиков нет: вежливо скажи и предложи ближайший свободный час. "
              f"Подтверждённую бронь оформляй [ЗАЯВКА]-блоком — она автоматически сохранится в CRM.]")

# ============================== ПАМЯТЬ ГОСТЯ ==============================

SESSIONS = {}
SESSIONS_TTL = 2 * 3600


def _session_facts(sid: str) -> str:
    f = SESSIONS.get(sid or "", {})
    parts = []
    if f.get("name"):
        parts.append(f"имя: {f['name']}")
    if f.get("phone"):
        parts.append(f"телефон: {f['phone']}")
    if not parts:
        return ""
    return ("\n[ИЗВЕСТНО О ГОСТЕ — сообщал ранее в этом визите: "
            + ", ".join(parts)
            + ". НЕ переспрашивай эти данные — подставляй сам, в т.ч. в [ЗАЯВКА].]")


def _remember(sid: str, **kv):
    if not sid:
        return
    rec = SESSIONS.setdefault(sid, {})
    rec.update({k: v for k, v in kv.items() if v})
    rec["_t"] = time.time()
    now = time.time()
    for k in list(SESSIONS):
        if now - SESSIONS[k].get("_t", 0) > SESSIONS_TTL:
            SESSIONS.pop(k, None)

# ============================== LLM ==============================

_CTX_OK = ssl.create_default_context()
_CTX_ANY = ssl._create_unverified_context()
_SSL_FALLBACK = False


def _urlopen(req, timeout):
    global _SSL_FALLBACK
    if _SSL_FALLBACK:
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX_ANY)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX_OK)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(getattr(e, "reason", "")):
            print("[!] SSL-сертификаты не настроены — включаю обход")
            _SSL_FALLBACK = True
            return urllib.request.urlopen(req, timeout=timeout, context=_CTX_ANY)
        raise


class ApiError(Exception):
    pass


class RateLimited(ApiError):
    pass


def load_prompt() -> str:
    try:
        with open(PROMPT_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "Ты — дружелюбный виртуальный администратор кафе Coffee Room Family в Семее."


def _strip_think(text: str) -> str:
    return re.sub(r"<think>[\s\S]*?(</think>|$)", "", text).strip()


def openai_call(p, prompt, history, message) -> str:
    msgs = [{"role": "system", "content": prompt}]
    msgs += [{"role": h.get("role", "user"), "content": str(h.get("content", ""))[:4000]}
             for h in history[-HIST_LIMIT:]]
    msgs.append({"role": "user", "content": message})
    body = {"model": p["model"], "messages": msgs, "temperature": 0.5, "max_tokens": 700}
    req = urllib.request.Request(
        p["base"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + p["key"],
                 "User-Agent": "CoffeeRoomAI/4.0"})
    try:
        with _urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RateLimited("quota") if e.code == 429 else ApiError(f"{p['name']} http {e.code}")
    return _strip_think(data["choices"][0]["message"]["content"] or "")


def _gemini_body(p, prompt, history, message, with_thinking=True):
    contents = []
    for h in history[-HIST_LIMIT:]:
        role = "model" if h.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(h.get("content", ""))[:4000]}]})
    contents.append({"role": "user", "parts": [{"text": message}]})
    cfg = {"temperature": 0.5, "maxOutputTokens": 700}
    if with_thinking:
        cfg["thinkingConfig"] = {"thinkingLevel": THINKING}
    return {"system_instruction": {"parts": [{"text": prompt}]},
            "contents": contents, "generationConfig": cfg}


def _gemini_open(p, body):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{p['model']}:streamGenerateContent?alt=sse&key={p['key']}")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "CoffeeRoomAI/4.0"})
    try:
        return _urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited("quota")
        if e.code == 400 and "thinkingConfig" in json.dumps(body):
            return None
        raise ApiError(f"{p['name']} http {e.code}")


def gemini_stream(p, prompt, history, message):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{p['model']}:streamGenerateContent?alt=sse&key={p['key']}")
    resp = _gemini_open(p, _gemini_body(p, prompt, history, message))
    if resp is None:
        resp = _gemini_open(p, _gemini_body(p, prompt, history, message, with_thinking=False))
    with resp:
        buf = b""
        for raw in resp:
            buf += raw
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip().decode("utf-8", "ignore")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("text"):
                            yield part["text"]

# ============================== HTTP ==============================

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml",
        ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
        ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CoffeeRoomCRM/4.0"

    # ---------- статика ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        if path == "/crm":
            path = "/crm.html"
        if path == "/bookings.json":  # базу наружу не отдаём
            return self.send_error(403)
        safe = os.path.normpath(path).lstrip("/\\")
        if safe.startswith(".."):
            return self.send_error(403)
        full = os.path.join(SITE, safe)
        if not os.path.isfile(full):
            return self.send_error(404)
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1].lower(),
                                                  "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = min(int(self.headers.get("Content-Length", "0")), 100_000)
        return json.loads(self.rfile.read(length).decode() or "{}")

    # ---------- CRM API ----------
    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/chat":
            return self._chat()

        if path == "/api/bookings":  # создать бронь (форма на сайте / CRM)
            try:
                b = self._body()
            except Exception:
                return self._json({"ok": False, "error": "bad_json"}, 400)
            if b.get("pin") != CRM_PIN and (b.get("source") or "") != "форма":
                return self._json({"ok": False, "error": "bad_pin"}, 403)
            rec = add_booking(b.get("name"), b.get("phone"), b.get("branch"),
                              b.get("date"), b.get("time"), b.get("guests"),
                              b.get("prefs") or b.get("note"),
                              source=b.get("source") or "CRM",
                              kind=b.get("kind") or "столик")
            return self._json({"ok": True, "booking": rec})

        if path == "/api/bookings-list":  # список брони (только с PIN)
            if self.headers.get("X-CRM-PIN") != CRM_PIN:
                return self._json({"ok": False, "error": "bad_pin"}, 403)
            with _DB_LOCK:
                db = _load_db()
                return self._json({"ok": True, "bookings": db["bookings"]})

        return self.send_error(404)

    def do_PATCH(self):
        if self.path.split("?")[0] != "/api/bookings":
            return self.send_error(404)
        if self.headers.get("X-CRM-PIN") != CRM_PIN:
            return self._json({"ok": False, "error": "bad_pin"}, 403)
        try:
            b = self._body()
        except Exception:
            return self._json({"ok": False, "error": "bad_json"}, 400)
        with _DB_LOCK:
            db = _load_db()
            for rec in db["bookings"]:
                if rec["id"] == b.get("id"):
                    rec["status"] = b.get("status", rec["status"])
                    _save_db(db)
                    return self._json({"ok": True, "booking": rec})
        return self._json({"ok": False, "error": "not_found"}, 404)

    def do_DELETE(self):
        if self.path.split("?")[0] != "/api/bookings":
            return self.send_error(404)
        if self.headers.get("X-CRM-PIN") != CRM_PIN:
            return self._json({"ok": False, "error": "bad_pin"}, 403)
        try:
            bid = int(self.headers.get("Booking-Id", "0"))
        except ValueError:
            return self._json({"ok": False, "error": "bad_id"}, 400)
        with _DB_LOCK:
            db = _load_db()
            before = len(db["bookings"])
            db["bookings"] = [b for b in db["bookings"] if b["id"] != bid]
            if len(db["bookings"]) == before:
                return self._json({"ok": False, "error": "not_found"}, 404)
            _save_db(db)
        print(f"[CRM] -бронь #{bid} удалена")
        return self._json({"ok": True})

    # ---------- чат ----------
    def _chat(self):
        try:
            payload = self._body()
        except Exception:
            return self._json({"fallback": True, "reason": "bad_json"})
        message = str(payload.get("message", "")).strip()[:MSG_LIMIT]
        history = [h for h in payload.get("history", []) if isinstance(h, dict)
                   and h.get("role") in ("user", "assistant")][:30]
        sid = str(payload.get("sid") or "")[:64]
        if not message:
            return self._json({"fallback": True, "reason": "empty"})
        if not PROVIDERS:
            return self._json({"fallback": True, "reason": "no_api_key"})

        now = time.time()
        if now - _rpm_window[0] > 60:
            _rpm_window[0], _rpm_window[1] = now, 0
        _rpm_window[1] += 1
        if _rpm_window[1] > SAFE_RPM:
            return self._json({"fallback": True, "reason": "rate_limited"})

        prompt = load_prompt()
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        t = time.gmtime(time.time() + 5 * 3600)
        today = f"{t.tm_mday:02d}.{t.tm_mon:02d}.{t.tm_year}, {days_ru[t.tm_wday]}"
        prompt += (f"\n\n[СИСТЕМА: сегодня {today}. Относительные даты («завтра», "
                   f"«в выходные») считай от этой даты и пиши конкретно: дд.мм.]")
        prompt += _session_facts(sid)
        prompt += availability_text()

        last_reason = "all_failed"
        first, stream = None, None
        for p in PROVIDERS:
            try:
                if p["provider"] == "gemini":
                    stream = gemini_stream(p, prompt, history, message)
                    first = next(stream, None)
                    if first is None:
                        last_reason = "empty_reply"
                        continue
                else:
                    first = openai_call(p, prompt, history, message)
                    stream = iter(())
                    if not first:
                        last_reason = "empty_reply"
                        continue
                print(f"[AI] {p['name']}: {message[:50]!r}")
                break
            except RateLimited:
                last_reason = "rate_limited"
            except ApiError as e:
                last_reason = str(e)
        else:
            return self._json({"fallback": True, "reason": last_reason})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        full_text = first
        try:
            emit({"t": first})
            for chunk in stream:
                emit({"t": chunk})
                full_text += chunk
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                emit({"err": str(e)[:120]})
            except Exception:
                pass
        finally:
            # авто-сохранение заявки в CRM + память гостя
            m = re.search(r"\[ЗАЯВКА\]([\s\S]*?)\[/ЗАЯВКА\]", full_text)
            if m:
                try:
                    b = json.loads(m.group(1))
                    _remember(sid, name=b.get("name"), phone=b.get("phone"))
                    rec = add_booking(b.get("name"), b.get("phone"), b.get("branch"),
                                      b.get("date"), b.get("time"), b.get("guests"),
                                      b.get("prefs"), source="AI-чат",
                                      kind="праздник" if "пакет" in full_text.lower() or "дет" in full_text.lower() else "столик")
                    print(f"[CRM] +бронь #{rec['id']}: {rec['name']} {rec['date']} {rec['slot']} {rec['branch']}")
                except Exception as exc:
                    print("[CRM] ошибка сохранения:", exc)

    def log_message(self, fmt, *args):
        pass


def _free_port(port):
    import subprocess
    me = os.getpid()
    pids = []
    try:
        out = subprocess.run(["lsof", "-ti", f":{port}"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        pids += [int(x) for x in out if x.strip().isdigit()]
    except Exception:
        pass
    pids = [p for p in dict.fromkeys(pids) if p != me]
    if pids:
        print(f"⚠️  Порт {port} занят ({', '.join(map(str, pids))}) — останавливаю старый сервер...")
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        time.sleep(1.2)
        return
    try:
        out = subprocess.run(["pgrep", "-f", r"server\.py"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        extra = [int(x) for x in out if x.strip().isdigit() and int(x) not in (me, os.getppid())]
        for pid in extra:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    _free_port(PORT)
    chain = " → ".join(p["name"] for p in PROVIDERS) if PROVIDERS else "нет ключей — чат на встроенных сценариях"
    print(f"☕ Coffee Room Family v4 · AI: {chain} · CRM: /crm (PIN {CRM_PIN}) · зал: {CRM_TABLES} столов/час")
    print(f"📖 Инструкция AI: {PROMPT_FILE} (правится без перезапуска)")
    print(f"🚀 http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
