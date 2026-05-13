# Rivas | ریواس

Rivas یک ربات Bale است که برای هر کاربر، یک مسیر اختصاصی به Mira در تلگرام فراهم می‌کند.

در این نسخه، پروژه به‌صورت **multi-tenant** پیاده‌سازی شده است:
- یک ربات Bale مشترک
- برای هر کاربر، یک `mira-telegram-service` اختصاصی (کانتینر جدا)
- routing پیام‌ها با `bale_user_id`/`bale_chat_id` از روی MySQL

---

## Architecture

```text
Bale User
  -> Rivas Bot (shared)
  -> Tenant Registry (MySQL)
  -> Dedicated mira-service container (per tenant)
  -> Telethon user account (per tenant)
  -> @mira
  -> Dedicated response
```

### Core Components
- `rivas-bot`: دریافت پیام Bale + routing + UX
- `mira-telegram-service`: relay API برای Telethon/Mira
- `mysql`: registry و metadata tenantها + request logs + audit events
- `phpmyadmin`: مدیریت MySQL (localhost only)
- `rivas-admin`: onboarding و مدیریت tenantها

---

## Docker Compose

سرویس‌ها:
- `mysql`
- `phpmyadmin`
- `mira-service` (اختیاری، فقط profile `standalone`)
- `rivas-bot`
- `autoheal` (watchdog برای restart خودکار کانتینرهای unhealthy)

Run:

```bash
docker compose up -d --build
```

اگر بخواهی سرویس مستقل `mira-service` هم بالا باشد:

```bash
docker compose --profile standalone up -d --build
```

Health checks:

```bash
curl http://127.0.0.1:8088/ready
```

`/ready` سرویس مستقل Mira فقط وقتی profile `standalone` فعال باشد:

```bash
curl http://127.0.0.1:8090/ready
```

پایداری runtime:
- کانتینرهای tenant و سرویس‌های اصلی healthcheck دارند.
- سرویس `autoheal` هر چند ثانیه وضعیت health را چک می‌کند و اگر کانتینری unhealthy شود، خودکار ری‌استارتش می‌کند.
- `restart: unless-stopped` هم برای crash/exit فعال است.

phpMyAdmin:
- `http://127.0.0.1:${PMA_PORT}`

---

## Admin Onboarding (New User)

### users.json Sync (Optional)

برای مدیریت tenantها در مقیاس بالا، از `users.json` استفاده کن:
- فایل واقعی: `users.json` (در `.gitignore`)
- نمونه: `users.example.json` (داخل گیت)

اجرای سینک:

```bash
PYTHONPATH=src .venv/bin/python -m rivas.load_users --config users.json --write-back
```

یا با اسکریپت:

```bash
scripts/load_users.sh users.json
```

اگر خواستی فقط runtime کانتینرها با آخرین تنظیمات health/autoheal دوباره reconcile شوند:

```bash
scripts/runtime_reconcile.sh
```

رفتار `load_users`:
- یوزر جدید اضافه شود: tenant و کانتینر اختصاصی ساخته/آپدیت می‌شود.
- اگر `telegram.string_session` خالی باشد: OTP/2FA در CLI گرفته می‌شود.
- یوزر حذف شود: tenant غیرفعال می‌شود و کانتینرش حذف می‌شود.
- یوزر ویرایش شود: فقط همان tenant اعمال تغییر می‌شود.
- tenantهای بدون تغییر: ری‌استارت نمی‌شوند.
- اگر کاربر از داخل Bale ثبت‌نام کرده باشد، بعد از provision موفق پیام «فعال شد» خودکار برای همان `bale_chat_id` ارسال می‌شود.

### 1) Set env

```bash
cp .env.example .env
```

حتماً مقدارهای زیر را درست کن:
- `BALE_BOT_TOKEN`
- `TG_API_ID`
- `TG_API_HASH`
- `MASTER_ENCRYPTION_KEY`
- `MYSQL_*`
- برای اجرای `rivas-admin` روی host:
  - `ADMIN_DB_URL=mysql://rivas:rivas@127.0.0.1:3306/rivas`

### 2) Add tenant

```bash
rivas-admin tenant-add \
  --tenant-slug user001 \
  --owner-name "User 001" \
  --phone "+98912xxxxxxx" \
  --bale-user-id "123456" \
  --bale-chat-id "123456"
```

یا با اسکریپت:

```bash
scripts/onboard_tenant.sh user001 "User 001" +98912xxxxxxx 123456 123456
```

اگر `StringSession` آماده داری و نمی‌خواهی OTP بگیری:

```bash
export TG_STRING_SESSION='...'
scripts/onboard_tenant.sh user001 "User 001" +98912xxxxxxx 123456 123456
```

یا مستقیم:

```bash
rivas-admin tenant-add \
  --tenant-slug user001 \
  --owner-name "User 001" \
  --phone "+98912xxxxxxx" \
  --bale-user-id "123456" \
  --bale-chat-id "123456" \
  --string-session "$TG_STRING_SESSION"
```

در onboarding:
- کد لاگین تلگرام گرفته می‌شود
- اگر 2FA فعال باشد پسورد می‌خواهد
- `TG_STRING_SESSION` تولید و encrypted ذخیره می‌شود
- کانتینر tenant اختصاصی ساخته می‌شود
- mapping کاربر Bale به tenant ثبت می‌شود

### Session Recovery (when AuthKey duplicated)

اگر خطای `AUTH_KEY_DUPLICATED` گرفتی، اول یک سشن جدید بساز:

```bash
python scripts/create_string_session.py \
  --api-id "$TG_API_ID" \
  --api-hash "$TG_API_HASH" \
  --phone "+98912xxxxxxx" \
  --output data/new_tg_string_session.txt
```

بعد همان tenant را با `--tenant-id` و `--string-session` دوباره provision کن:

```bash
rivas-admin tenant-add \
  --tenant-id "tenant_xxx" \
  --tenant-slug "user001" \
  --owner-name "User 001" \
  --phone "+98912xxxxxxx" \
  --bale-user-id "123456" \
  --bale-chat-id "123456" \
  --string-session "$(cat data/new_tg_string_session.txt)"
```

### 3) Tenant management

```bash
rivas-admin tenant-list
rivas-admin tenant-disable --tenant-id tenant_xxx
rivas-admin tenant-enable --tenant-id tenant_xxx
```

### Interactive Registration Provisioning (No users.json)

اگر می‌خواهی کل onboarding فقط از دیتابیس انجام شود (بدون `users.json`)، از اسکریپت interactive استفاده کن:

```bash
scripts/registration_admin.sh
```

این اسکریپت:
- لیست درخواست‌های ثبت‌نام (`pending_admin`) را نشان می‌دهد
- با انتخاب یک مورد، اطلاعات لازم را مرحله‌ای از ادمین می‌گیرد
- OTP/2FA تلگرام را همان‌جا می‌گیرد (اگر `string_session` آماده نباشد)
- tenant/container اختصاصی را بالا می‌آورد
- DB را کامل آپدیت می‌کند
- پیام فعال‌سازی را برای کاربر بله ارسال می‌کند

اگر بخواهی فقط یک مورد را رسیدگی کنی و خارج شوی:

```bash
scripts/registration_admin.sh --once
```

---

## Bale Registration Flow

کاربر جدید بدون tenant فعال نمی‌تواند از چت استفاده کند.

جریان ثبت‌نام:
1. کاربر `/start` می‌زند و پیام ثبت‌نام می‌گیرد.
2. نام کاربری دلخواه می‌فرستد.
3. شماره تلگرام با فرمت `+98...` می‌فرستد.
4. درخواست در DB ذخیره می‌شود و پیام لاگ برای ادمین ارسال می‌شود.
5. ادمین `users.json` را آپدیت می‌کند و `load_users` را اجرا می‌کند.
6. بعد از provision شدن tenant، کاربر امکان چت خواهد داشت.
7. در لحظه فعال‌سازی، ربات برای همان کاربر پیام خودکار می‌فرستد که سرویسش فعال شده است.

---

## Environment Variables

فایل مرجع: `.env.example`

گروه‌های اصلی:
- Shared: `APP_ENV`, `LOG_LEVEL`, `MASTER_ENCRYPTION_KEY`
- Activation notify: `NOTIFY_USER_ACTIVATION` (default: `true`)
- Bot: `BALE_BOT_TOKEN`, `DB_URL`, `REQUEST_TIMEOUT_SECONDS`
- Admin CLI: `ADMIN_DB_URL`
- Tenant runtime: `TENANT_SERVICE_IMAGE`, `DOCKER_NETWORK_NAME`
- Mira: `TG_API_ID`, `TG_API_HASH`, `MIRA_*_SECONDS`, `MIRA_MAX_PAYLOAD_MB`
  Includes hard per-request guard timeout: `RELAY_HARD_TIMEOUT_SECONDS` (recommended > `OVERALL_TIMEOUT_SECONDS`)
- Telegram Proxy (optional): `TG_PROXY_ENABLED`, `TG_PROXY_TYPE`, `TG_PROXY_HOST`, `TG_PROXY_PORT`, `TG_PROXY_USERNAME`, `TG_PROXY_PASSWORD`, `TG_PROXY_RDNS`
  Optional split override: `TG_PROXY_HOST_RUNTIME` (inside containers), `TG_PROXY_HOST_ADMIN` (host-side scripts).
  For host-local proxy in Docker, use `TG_PROXY_HOST_RUNTIME=host.docker.internal` and `TG_PROXY_HOST_ADMIN=127.0.0.1`.
- Auto-heal watchdog: `AUTOHEAL_INTERVAL`, `AUTOHEAL_CURL_TIMEOUT`, `AUTOHEAL_START_PERIOD`
- MySQL: `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`
- Log channel: `LOG_TG_ENABLED`, `LOG_TG_BOT_TOKEN`, `LOG_TG_CHAT_USERNAME` (preferred), `LOG_TG_CHAT_ID` (fallback), `LOG_TG_MIN_LEVEL`

---

## Logging / Audit

سیستم لاگ:
- structured logs با `request_id` و `tenant_id`
- eventهای بحرانی به Telegram log channel (اختیاری)
- audit events در DB
- فرمت پیام‌های کانال لاگ: تمیز، دسته‌بندی‌شده، ایموجی‌دار (Level/Time/Message/Context)

رویدادهای مهم:
- `tenant_add_success`
- `registration_submitted`
- `request_queued`
- `request_completed`
- `request_failed_remote`
- `request_failed_unavailable`
- خطاهای تجربه کاربر مانند:
  - tenant غیرفعال
  - runtime در دسترس نیست
  - endpoint tenant ناقص
  - ورودی نامعتبر ثبت‌نام (شماره تلفن)
  - media رد شده به خاطر محدودیت حجم

---

## Testing

Unit tests:

```bash
source .venv/bin/activate
pytest -q
```

Smoke test Mira API:

```bash
source .venv/bin/activate
set -a && source .env && set +a
MIRA_SERVICE_URL=http://127.0.0.1:8090 python scripts/smoke_test_mira_service.py
```

---

## Security Notes

- هیچ secretی را داخل کد commit نکن.
- `TG_STRING_SESSION` و `TG_API_HASH` encrypted-at-rest ذخیره می‌شوند.
- اگر credential لو رفت، فوراً rotate کن.
- phpMyAdmin فقط localhost باشد.
- یک `TG_STRING_SESSION` را همزمان در چند کانتینر اجرا نکن؛ باعث `AUTH_KEY_DUPLICATED` و باطل‌شدن سشن می‌شود.
