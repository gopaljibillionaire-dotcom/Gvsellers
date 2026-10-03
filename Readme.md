# Digital Store Bot

Files: `main.py`, `config.py` (the only Python files), plus `Procfile`, `requirements.txt`, `.python-version`.

## Run locally
    pip install -r requirements.txt
    export BOT_TOKEN=... ADMIN_IDS=... MONGO_URI=...   # or edit config.py
    python main.py

## Deploy (Heroku / Railway / Render worker)
1. Push the folder to Git; create a **worker** process (`Procfile` already defines it).
2. Set config vars from `.env.example` (BOT_TOKEN, ADMIN_IDS, MONGO_URI, BINANCE_*, SETTINGS_ENCRYPTION_KEY).
3. Generate the encryption key once and keep it safe:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
4. Optional webhook: run as a `web` process, set `WEBHOOK_PORT=$PORT` and `WEBHOOK_PUBLIC_URL`.
   Without it, payments are verified by the built-in poller every ~25 seconds.
