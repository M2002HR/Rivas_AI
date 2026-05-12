# Mira Telegram Service API (v1)

این سند مربوط به سرویس مستقل `mira-telegram-service` است.

## Base URL
- پیش‌فرض: `http://<host>:8090`

## Auth
- اگر `MIRA_SERVICE_API_KEY` ست شده باشد، هدر زیر الزامی است:
  - `X-API-Key: <your_key>`

## Health Endpoints
- `GET /health` -> `200` `{ "status": "ok" }`
- `GET /ready` -> `200/503` `{ "ready": bool, "queue_size": number }`
- `GET /v1/capabilities`

## Core Endpoint

### `POST /v1/relay`

Request JSON:

```json
{
  "request_id": "optional-string",
  "input_type": "text|photo|audio|web_search",
  "text": "optional",
  "caption": "optional",
  "file_name": "optional",
  "mime_type": "optional",
  "media_base64": "optional-base64",
  "meta": {
    "bale_user_id": "optional",
    "bale_chat_id": "optional",
    "user_mode": "optional"
  }
}
```

Rules:
- برای `photo` و `audio` وجود `media_base64` اجباری است.
- سقف payload با `MIRA_MAX_PAYLOAD_MB` کنترل می‌شود (پیش‌فرض 20MB).

Success Response (`200`):

```json
{
  "ok": true,
  "request_id": "...",
  "result": {
    "text": "final merged text",
    "text_blocks": ["..."],
    "source_message_ids": [123, 124],
    "media_parts": [
      {
        "media_type": "photo|audio|voice|video|document",
        "file_name": "optional",
        "mime_type": "optional",
        "source_message_id": 123,
        "data_base64": "...",
        "size_bytes": 12345
      }
    ]
  },
  "meta": {
    "queue_size": 0
  }
}
```

Error Responses:
- `400` -> `invalid_input`
- `401` -> `unauthorized`
- `429` -> `mira_rate_limit` (+ `retry_after_seconds`)
- `503` -> `telegram_connection`
- `504` -> `mira_no_response`
- `500` -> `internal_error`

## Convenience Endpoints
- `POST /v1/text` (forced `input_type=text`)
- `POST /v1/image` (forced `input_type=photo`)
- `POST /v1/audio` (forced `input_type=audio`)
- `POST /v1/search` (forced `input_type=web_search`)

## Curl Samples

### Text
```bash
curl -X POST http://127.0.0.1:8090/v1/text \
  -H 'Content-Type: application/json' \
  -d '{"text":"سلام، یک پاسخ کوتاه بده"}'
```

### Image
```bash
IMG_B64=$(base64 -w 0 sample.jpg)
curl -X POST http://127.0.0.1:8090/v1/image \
  -H 'Content-Type: application/json' \
  -d "{\"caption\":\"تحلیل کن\",\"media_base64\":\"$IMG_B64\",\"file_name\":\"sample.jpg\",\"mime_type\":\"image/jpeg\"}"
```

### Web Search
```bash
curl -X POST http://127.0.0.1:8090/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"text":"قیمت طلا امروز"}'
```

## Python Client

```python
from mira_telegram_service.client import MiraServiceClient
from mira_telegram_service.models import InputType, RequestPayload
```

## Logging

رویدادهای اصلی:
- `relay_accepted`
- `relay_completed`
- `relay_failed`

همه secretها sanitize می‌شوند.
