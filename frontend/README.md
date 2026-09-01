# Traido frontend (Vite + React)

Confirmation desk UI. Talks to the Python backend on `:8000` via Vite proxy.

## Run

```bash
# Terminal 1 — backend
cd .. && .venv312/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 — frontend (./npm.sh avoids Cursor’s npm_config_devdir warning)
./npm.sh install
./npm.sh run dev
# → http://127.0.0.1:3000
```

`npm_config_devdir` is injected by the Cursor sandbox and is not a valid npm key.
Use `./npm.sh …` (or `env -u npm_config_devdir npm …`) so the warning does not appear.

## Layout

```
frontend/
  src/components/desk/   # Desk UI
  src/lib/               # API client, messages, time
  src/styles/            # Locked tokens + globals
```

Optional API key (if backend has `TRAIDO_API_KEY`):

```js
localStorage.setItem("TRAIDO_API_KEY", "your-key")
```
