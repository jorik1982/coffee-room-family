# Coffee Room Family — сайт + AI-администратор

Сервер раздаёт сайт и принимает запросы чата (`/api/chat`),
передавая их в LLM (Groq / Gemini) с инструкцией из `system_prompt.md`.

## Файлы
- `index.html` — сайт (витрина + чат)
- `server.py` — сервер (Python, только стандартная библиотека)
- `system_prompt.md` — инструкция для AI (можно править без перезапуска)
- `requirements.txt` — зависимостей нет (для сборки Render)
- `render.yaml` — конфиг деплоя на Render.com

## Запуск локально
```bash
export GROQ_API_KEY="ваш_ключ"
python3 server.py
```

## Деплой на Render
1. Загрузите файлы в GitHub-репозиторий
2. render.com → New → Web Service → выберите репозиторий
3. Blueprints подхватят `render.yaml` → введите `GROQ_API_KEY` → Deploy
