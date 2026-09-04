# IBKR через Tailscale (бесплатно)

Gateway остаётся на **вашем Mac**. Railway backend подключается к Mac по **Tailscale** — без VPS.

```
Mac (IB Gateway :4002)  ←── Tailscale ──→  Railway (Traido backend)
     100.x.x.x                                  100.y.y.y
```

Локально Traido может использовать `127.0.0.1` (Gateway на том же Mac).  
На Railway — **Tailscale IP вашего Mac** (`100.x.x.x`).

## 1. Tailscale на Mac

1. Установите: [tailscale.com/download/mac](https://tailscale.com/download/mac)
2. Войдите в аккаунт (Google/GitHub — бесплатно).
3. Запустите **IB Gateway** (paper, порт 4002).
4. Узнайте IP Mac:

```bash
/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
```

Запишите адрес вида `100.x.x.x` — это `TRAIDO_IBKR_HOST` для Railway.

## 2. IB Gateway — разрешить Tailscale

В Gateway: **Configure → Settings → API → Settings**

- ✅ Enable ActiveX and Socket Clients
- ❌ снять **Allow connections from localhost only**
- **Trusted IPs** — после первого деплоя backend добавьте IP Railway из Tailscale (шаг 4)

Socket port: **4002** (paper).

## 3. Ключ для Railway

1. [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys)
2. **Generate auth key** → Reusable, без expiry (или 90 days + продлевать).
3. Railway → **backend** → Variables:

| Переменная | Значение |
|------------|----------|
| `TAILSCALE_AUTHKEY` | ключ из шага 3 |
| `TRAIDO_BROKER` | `ibkr` |
| `TRAIDO_IBKR_HOST` | `100.x.x.x` (IP Mac из шага 1) |
| `TRAIDO_IBKR_PORT` | `4002` |
| `TRAIDO_IBKR_CLIENT_ID` | `1` |
| `TRAIDO_IBKR_ACCOUNT` | ваш paper account |
| `TRAIDO_IBKR_ENV` | `paper` |

Redeploy backend.

## 4. Trusted IP Railway на Gateway

После деплоя в логах backend будет строка `tailscale: backend node 100.y.y.y`.

Добавьте этот IP в **Trusted IPs** в IB Gateway на Mac.

## 5. Локальный `.env`

Можно оставить как сейчас:

```env
TRAIDO_IBKR_HOST=127.0.0.1
TRAIDO_IBKR_PORT=4002
```

Или для единообразия — тот же `100.x.x.x` Mac (работает и через Tailscale loopback).

## 6. Проверка

```bash
# Локально
curl -s http://127.0.0.1:8000/health/ready

# Production
curl -s https://ВАШ-BACKEND.railway.app/health/ready
```

В обоих: `"broker": { "ok": true, ... }`.

## Важно

- Mac должен быть **включён**, Gateway **запущен**, Tailscale **активен**.
- Не ставьте `TRAIDO_IBKR_HOST=127.0.0.1` на Railway — backend откажется (это localhost контейнера).
- `TAILSCALE_AUTHKEY` — секрет, только в Railway Variables, не в git.

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| `ib_async is not installed` | Redeploy backend (образ с `[ibkr]`) |
| `loopback` на Railway | `TRAIDO_IBKR_HOST` = IP Mac в Tailscale |
| `BrokerUnreachable` | Gateway выключен или Mac спит |
| Connection refused | Trusted IPs: добавь IP backend из логов |
| `clientId in use` | Один backend на `client_id=1`; остановите дубликат uvicorn |
