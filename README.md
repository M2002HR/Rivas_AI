# Rivas | ریواس

**Rivas** یک ربات Bale است که پاسخ هوش مصنوعی را از طریق یک سرویس مستقل Telegram+Telethon می‌گیرد.

این پروژه عمداً به دو بخش جدا تقسیم شده است:
- `rivas-bot`: تجربه کاربری و منطق ربات بله
- `mira-telegram-service`: سرویس reusable برای اتصال به `@mira` در تلگرام

---

## 1) معماری نهایی

```text
Bale User
  -> Rivas Bale Bot (Polling)
  -> Mira Telegram Service (HTTP API)
  -> Telethon User Account
  -> @mira
  -> Full Response Collector
  -> Rivas Bale Bot
  -> Bale User
```

نکته مهم UX:
- پیام وضعیت در یک پیام واحد مدیریت می‌شود.
- پیام «درخواستت دریافت شد...» سریع نمایش داده می‌شود.
- در صورت طولانی شدن پردازش، همان پیام به «هنوز در حال پردازشه...» ویرایش می‌شود.
- بعد از آماده‌شدن خروجی، پیام وضعیت حذف می‌شود و فقط پاسخ نهایی می‌ماند.

---

## 2) Submodule واقعی (Git جدا)

سرویس Mira به‌صورت **Git Submodule** در این مسیر قرار دارد:
- `submodules/mira-telegram-service`

این یعنی می‌توانی دقیقاً همین سرویس را در هر پروژه‌ی دیگری reuse کنی.

فایل‌های کلیدی سرویس:
- `submodules/mira-telegram-service/src/mira_telegram_service/api.py`
- `submodules/mira-telegram-service/src/mira_telegram_service/bridge.py`
- `submodules/mira-telegram-service/src/mira_telegram_service/client.py`
- `submodules/mira-telegram-service/src/mira_telegram_service/main.py`

---

## 3) ساختار پروژه

```text
Mira_Bot_API/
  src/rivas/
    app.py
    main.py
    config.py
    storage.py
    ui.py
    text_utils.py
    health.py
    mira_service/            # compatibility layer (uses submodule package)
    mira_service_client.py   # compatibility layer
    mira_service_api.py      # compatibility layer
    mira_service_main.py     # compatibility layer

  submodules/
    mira-telegram-service/   # مستقل، قابل reuse

  docs/
    MIRA_SERVICE_API.md

  docker-compose.yml
  Dockerfile
```

---

## 4) API سرویس Mira

مستندات کامل endpointها:
- [docs/MIRA_SERVICE_API.md](docs/MIRA_SERVICE_API.md)

خلاصه endpointها:

| Method | Endpoint | توضیح |
|---|---|---|
| GET | `/health` | سلامت سرویس |
| GET | `/ready` | وضعیت آماده‌بودن + queue size |
| GET | `/v1/capabilities` | قابلیت‌های سرویس |
| POST | `/v1/relay` | relay عمومی |
| POST | `/v1/text` | متن |
| POST | `/v1/image` | تصویر |
| POST | `/v1/audio` | صوت |
| POST | `/v1/search` | جستجو |

---

## 5) راه‌اندازی (Docker Compose)

### 5.1 دریافت submodule

```bash
git submodule update --init --recursive
```

### 5.2 تنظیم env

```bash
cp .env.example .env
```

برای تست محلی یا CI:

```bash
cp .env.test.example .env.test
```

مقادیر ضروری:
- `BALE_BOT_TOKEN`
- `TG_API_ID`
- `TG_API_HASH`
- `TG_STRING_SESSION`

### 5.3 اجرا

```bash
docker compose up -d --build
```

### 5.4 بررسی سلامت

```bash
docker compose ps
curl http://127.0.0.1:8090/ready
curl http://127.0.0.1:8088/ready
```

پورت‌ها:
- Mira API: `8090`
- Bale Bot health: `8088` (داخل کانتینر: `8080`)

---

## 6) استفاده از سرویس در پروژه‌های دیگر

### 6.1 به‌صورت submodule

```bash
git submodule add <YOUR_MIRA_SERVICE_REPO_URL> submodules/mira-telegram-service
git submodule update --init --recursive
```

### 6.2 نصب پکیج سرویس

```bash
pip install ./submodules/mira-telegram-service
```

### 6.3 استفاده از Python Client سرویس

```python
import asyncio
from mira_telegram_service.client import MiraServiceClient
from mira_telegram_service.models import InputType, RequestPayload

async def run():
    client = MiraServiceClient(
        base_url="http://127.0.0.1:8090",
        api_key="YOUR_API_KEY",
        timeout_seconds=320,
    )
    await client.start()
    try:
        payload = RequestPayload(
            request_id="external-req-1",
            bale_user_id="external-user",
            bale_chat_id="external-chat",
            input_type=InputType.TEXT,
            text="سلام، یک خلاصه کوتاه بده",
        )
        result = await client.relay(payload)
        print(result.text)
    finally:
        await client.close()

asyncio.run(run())
```

---

## 7) لاگ و مانیتورینگ

لاگ زنده:

```bash
docker compose logs -f mira-service rivas-bot
```

رویدادهای مهم:
- `relay_accepted`
- `relay_completed`
- `relay_failed`
- `request_queued`
- `request_completed`

لاگ‌ها sanitize می‌شوند و secretها mask می‌گردند.

---

## 8) امنیت

- هیچ secretی در کد hardcode نشده است.
- تمام credentialها از env خوانده می‌شوند.
- اگر `token/api_hash/session` جایی لو رفت، حتماً rotate/regenerate انجام بده.

---

## 9) نکته برندینگ

در خروجی نهایی، اشاره‌های `Mira/میرا` به `ریواس` جایگزین می‌شود تا تجربه کاربری یکدست باقی بماند.
