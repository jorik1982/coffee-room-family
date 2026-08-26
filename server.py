#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сервер сайта Coffee Room Family + AI-администратор «Аида» v3
  • ЦЕПОЧКА ПРОВАЙДЕРОВ: Groq → Gemini (лимит одного — авто-переключение на второго)
  • стриминг ответов (SSE), защита от <think>, User-Agent для Groq
  • инструкция AI живёт в system_prompt.md — правится без перезапуска

Переменные окружения (порядок = приоритет):
  GROQ_API_KEY    — бесплатный ключ console.groq.com
  GROQ_MODEL      — по умолчанию qwen/qwen3.8-27b
  GEMINI_API_KEY  — ключ aistudio.google.com (резерв)
  GEMINI_MODEL    — по умолчанию gemini-3.6-flash
  PORT            — порт (8080)

Нет ключей вообще — сервер работает, чат живёт на встроенных сценариях.
Запуск: python3 server.py
"""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8080"))
PROMPT_FILE = os.path.join(SITE, "system_prompt.md")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
THINKING = os.environ.get("AI_THINKING", "low")

# цепочка: первый живой и неупёршийся в лимит обслуживает гостя
PROVIDERS = []
if GROQ_API_KEY:
    PROVIDERS.append({"name": "groq", "provider": "openai", "key": GROQ_API_KEY,
                      "base": "https://api.groq.com/openai/v1", "model": GROQ_MODEL})
if GEMINI_API_KEY:
    PROVIDERS.append({"name": "gemini", "provider": "gemini", "key": GEMINI_API_KEY,
                      "model": GEMINI_MODEL})

MSG_LIMIT = 2000
HIST_LIMIT = 16
SAFE_RPM = 25                # наш собственный предохранитель
_rpm_window = [0.0, 0]


# --- лечение «CERTIFICATE_VERIFY_FAILED» на маках с Python от python.org ---
_CTX_OK = ssl.create_default_context()
_CTX_ANY = ssl._create_unverified_context()
_SSL_FALLBACK = False


def _urlopen(req, timeout):
    """Сначала с проверкой сертификата; если сертификаты не настроены —
    один раз предупредить и продолжить без проверки (иначе сервер не работает вовсе)."""
    global _SSL_FALLBACK
    if _SSL_FALLBACK:
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX_ANY)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX_OK)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(getattr(e, "reason", "")):
            print("[!] SSL-сертификаты Python не настроены — включаю обход. "
                  "Разово вылечить: запусти Install Certificates.command из папки Applications/Python 3.x")
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


# ------------------------------ вызовы провайдеров ------------------------------

def openai_call(p, prompt, history, message) -> str:
    """OpenAI-совместимый API (Groq и др.), одним куском."""
    msgs = [{"role": "system", "content": prompt}]
    msgs += [{"role": h.get("role", "user"), "content": str(h.get("content", ""))[:4000]}
             for h in history[-HIST_LIMIT:]]
    msgs.append({"role": "user", "content": message})
    body = {"model": p["model"], "messages": msgs, "temperature": 0.5, "max_tokens": 700}
    req = urllib.request.Request(
        p["base"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + p["key"],
                 "User-Agent": "CoffeeRoomAI/2.0"})
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
                                          "User-Agent": "CoffeeRoomAI/2.0"})
    try:
        return _urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited("quota")
        if e.code == 400 and "thinkingConfig" in json.dumps(body):
            return None  # модель не знает thinkingConfig — повторить без него
        raise ApiError(f"{p['name']} http {e.code}")


def gemini_stream(p, prompt, history, message):
    """Генератор кусочков ответа Gemini (SSE)."""
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


# ------------------------------ HTTP ------------------------------

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml",
        ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CoffeeRoomAI/3.0"

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
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

    def do_POST(self):
        if self.path.split("?")[0] != "/api/chat":
            return self.send_error(404)
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 100_000)
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return self._json({"fallback": True, "reason": "bad_json"})
        message = str(payload.get("message", "")).strip()[:MSG_LIMIT]
        history = [h for h in payload.get("history", []) if isinstance(h, dict)
                   and h.get("role") in ("user", "assistant")][:30]
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
        # подставляем сегодняшнюю дату (Алматы, UTC+5) — чтобы AI понимал «завтра» и «в выходные»
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        t = time.gmtime(time.time() + 5 * 3600)
        today = (f"{t.tm_mday:02d}.{t.tm_mon:02d}.{t.tm_year}, "
                 f"{days_ru[t.tm_wday]}")
        prompt += (f"\n\n[СИСТЕМА: сегодня {today}. Все относительные даты "
                   f"(«завтра», «в выходные») считай от этой даты и пиши конкретно: дд.мм.]")

        # ---- пробуем провайдеров по цепочке, пока кто-то не ответит ----
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
                print(f"[AI] отвечал {p['name']}")
                break
            except RateLimited:
                last_reason = "rate_limited"
            except ApiError as e:
                last_reason = str(e)
        else:
            return self._json({"fallback": True, "reason": last_reason})

        # ---- стримим клиенту ----
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            emit({"t": first})
            for chunk in stream:
                emit({"t": chunk})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                emit({"err": str(e)[:120]})
            except Exception:
                pass
        try:
            emit("[DONE]")
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass


def _free_port(port):
    """Если порт занят старым экземпляром сервера — останавливаем его (3 способа)."""
    import subprocess
    me = os.getpid()
    pids = []
    # способ 1: lsof (macOS и Linux с lsof)
    try:
        out = subprocess.run(["lsof", "-ti", f":{port}"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        pids += [int(x) for x in out if x.strip().isdigit()]
    except Exception:
        pass
    pids = [p for p in dict.fromkeys(pids) if p != me]
    if pids:
        print(f"⚠️  Порт {port} занят (процесс {', '.join(map(str, pids))}) — останавливаю старый сервер...")
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        time.sleep(1.2)
        print("✓ Старый сервер остановлен, порт свободен")
        return
    # способ 2: fuser
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=10)
        time.sleep(0.8)
        return
    except Exception:
        pass
    # способ 3: pgrep по имени скрипта (кроме себя и родителя)
    try:
        out = subprocess.run(["pgrep", "-f", "server\\.py"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        extra = [int(x) for x in out if x.strip().isdigit()
                 and int(x) not in (me, os.getppid())]
        if extra:
            print(f"⚠️  Порт {port} занят (процесс {', '.join(map(str, extra))}) — останавливаю...")
            for pid in extra:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            time.sleep(1)
    except Exception:
        pass


if __name__ == "__main__":
    _free_port(PORT)
    chain = " → ".join(p["name"] for p in PROVIDERS) if PROVIDERS else "нет ключей — чат на встроенных сценариях"
    print(f"☕ Coffee Room Family v3 · цепочка AI: {chain}")
    print(f"📖 Инструкция AI: {PROMPT_FILE} (правится без перезапуска)")
    print(f"🚀 http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
