from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import bale
from bale.error import NetworkError

from . import ui
from .config import BotConfig
from .health import HealthServer
from .log_channel import TelegramLogSink
from .logging_utils import get_logger, log_event
from .mira_service.client import MiraServiceClient, MiraServiceRemoteError, MiraServiceUnavailableError
from .models import InputType, MiraMediaPart, RegistrationRequest, RegistrationStatus, RequestPayload, TenantBinding
from .storage import Storage
from .text_utils import split_text

MODE_CHAT = "chat"
MODE_IMAGE = "image"
MODE_TRANSCRIBE = "transcribe"
MODE_WEB_SEARCH = "web_search"

MAX_INPUT_TEXT_CHARS = 8_000
MAX_RECENT_EVENTS = 8_000
PHONE_RE = re.compile(r"^\+?[1-9]\d{9,14}$")
BALE_RETRY_ATTEMPTS = 4
_BALE_HTTP_PATCHED = False


@dataclass(slots=True)
class QueueJob:
    payload: RequestPayload
    chat_id: str
    tenant_id: str
    endpoint_base_url: str
    status_message_id: str | int | None = None


class RivasApp:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.log = get_logger("rivas.app")
        _patch_bale_http_client()
        self.bot = bale.Bot(token=config.bale_bot_token)
        self.storage = Storage(config.db, config.retention_days)
        self.health = HealthServer(config.health_host, config.health_port, self.is_ready)
        self.log_sink = TelegramLogSink(
            enabled=config.log_tg_enabled,
            bot_token=config.log_tg_bot_token,
            chat_target=config.log_tg_chat_target,
            min_level=config.log_tg_min_level,
        )

        self._runtime_started = False
        self._runtime_lock = asyncio.Lock()
        self._ready = False

        self._cleanup_task: asyncio.Task | None = None
        self._tenant_health_task: asyncio.Task | None = None

        self._tenant_queues: dict[str, asyncio.Queue[QueueJob]] = {}
        self._tenant_workers: dict[str, asyncio.Task] = {}

        self._client_cache: dict[str, MiraServiceClient] = {}
        self._client_lock = asyncio.Lock()

        self._recent_inbound: OrderedDict[tuple[str, str], None] = OrderedDict()

        self._register_events()

    def run(self) -> None:
        self.bot.run()

    def is_ready(self) -> bool:
        return self._ready and self._runtime_started

    def _register_events(self) -> None:
        @self.bot.event
        async def on_before_ready():
            await self.bot.delete_webhook()
            await self.start_runtime()

        @self.bot.event
        async def on_ready():
            log_event(self.log, "bot_ready", username=getattr(self.bot.user, "username", None))

        @self.bot.event
        async def on_message(message: bale.Message):
            try:
                await self._handle_message(message)
            except Exception as exc:
                log_event(self.log, "on_message_handler_failed", error=str(exc), chat_id=str(getattr(message, "chat_id", "")))
                self.log_sink.send_background("ERROR", "On Message Handler Failed", str(exc))

        @self.bot.event
        async def on_message_edit(_: bale.Message):
            return

        @self.bot.event
        async def on_error(event_name, error):
            log_event(self.log, "bale_event_error", event_name=event_name, error=str(error))
            self.log_sink.send_background("ERROR", "Bale Event Error", str(error), {"event": event_name})

    async def start_runtime(self) -> None:
        async with self._runtime_lock:
            if self._runtime_started:
                return

            await self.storage.init()
            recovered = await self.storage.fail_stale_active_requests(stale_seconds=max(120, self.config.request_timeout_seconds))
            if recovered:
                log_event(self.log, "stale_requests_recovered", count=recovered)
            await self.storage.cleanup_expired()
            await self.health.start()

            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="rivas-cleaner")
            self._tenant_health_task = asyncio.create_task(self._tenant_health_loop(), name="rivas-tenant-health")

            self._runtime_started = True
            self._ready = True
            log_event(self.log, "runtime_started", db_url=self.config.db.db_url)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                deleted = await self.storage.cleanup_expired()
                if deleted:
                    log_event(self.log, "cleanup_expired", deleted=deleted)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log_event(self.log, "cleanup_error", error=str(exc))
                self.log_sink.send_background("WARNING", "Cleanup Loop Error", str(exc))

    async def _tenant_health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.tenant_health_interval_seconds)
                await self._probe_all_tenants_health()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log_event(self.log, "tenant_health_loop_error", error=str(exc))
                self.log_sink.send_background("WARNING", "Tenant Health Loop Error", str(exc))

    async def _probe_all_tenants_health(self) -> None:
        tenants = await self.storage.list_tenants()
        timeout = aiohttp.ClientTimeout(total=float(self.config.tenant_health_timeout_seconds))
        headers = {}
        if self.config.mira_service_api_key:
            headers["X-API-Key"] = self.config.mira_service_api_key

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for row in tenants:
                if str(row.get("status") or "").lower() != "active":
                    continue
                tenant_id = str(row.get("id") or "")
                container_name = str(row.get("container_name") or "")
                endpoint = str(row.get("endpoint_base_url") or "").rstrip("/")
                if not tenant_id or not endpoint:
                    continue

                runtime_status = "running"
                last_error: str | None = None
                try:
                    async with session.get(f"{endpoint}/ready", headers=headers) as response:
                        body = await response.json(content_type=None)
                        ready = response.status == 200 and bool(body.get("ready"))
                        if not ready:
                            runtime_status = "starting"
                            last_error = f"ready={body.get('ready')} queue_size={body.get('queue_size')}"
                except Exception as exc:
                    runtime_status = "unhealthy"
                    last_error = str(exc)

                await self.storage.upsert_tenant_runtime(
                    tenant_id=tenant_id,
                    container_name=container_name,
                    endpoint_base_url=endpoint,
                    runtime_status=runtime_status,
                    service_port=8090,
                    last_error=last_error,
                )

                if runtime_status != "running":
                    self.log_sink.send_background(
                        "WARNING",
                        "Tenant Health Degraded",
                        "Tenant service reported not-ready state.",
                        {"tenant_id": tenant_id, "container_name": container_name, "status": runtime_status, "error": last_error or ""},
                    )

    async def _call_bale_with_retry(
        self,
        op_name: str,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        for attempt in range(1, BALE_RETRY_ATTEMPTS + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                if attempt >= BALE_RETRY_ATTEMPTS or not _is_transient_bale_error(exc):
                    raise
                backoff_seconds = min(6, 2 ** (attempt - 1))
                log_event(
                    self.log,
                    "bale_call_retry",
                    op=op_name,
                    attempt=attempt,
                    backoff_seconds=backoff_seconds,
                    error=str(exc),
                )
                await asyncio.sleep(backoff_seconds)
        return None

    async def _safe_send_message(self, chat_id: str, text: str, **kwargs: Any) -> Any:
        try:
            return await self._call_bale_with_retry("send_message", self.bot.send_message, chat_id, text, **kwargs)
        except Exception as exc:
            log_event(self.log, "bale_send_message_failed", chat_id=chat_id, error=str(exc))
            self.log_sink.send_background("ERROR", "Bale Send Message Failed", str(exc), {"chat_id": chat_id})
            return None

    async def _safe_edit_message(self, chat_id: str, message_id: str | int, text: str, **kwargs: Any) -> bool:
        try:
            await self._call_bale_with_retry("edit_message", self.bot.edit_message, chat_id, message_id, text, **kwargs)
            return True
        except Exception as exc:
            log_event(self.log, "bale_edit_message_failed", chat_id=chat_id, message_id=str(message_id), error=str(exc))
            return False

    async def _safe_delete_message(self, chat_id: str, message_id: str | int, **kwargs: Any) -> bool:
        try:
            await self._call_bale_with_retry("delete_message", self.bot.delete_message, chat_id, message_id, **kwargs)
            return True
        except Exception as exc:
            log_event(self.log, "bale_delete_message_failed", chat_id=chat_id, message_id=str(message_id), error=str(exc))
            return False

    async def _safe_send_photo(self, chat_id: str, file_obj: Any) -> bool:
        try:
            await self._call_bale_with_retry("send_photo", self.bot.send_photo, chat_id, file_obj)
            return True
        except Exception as exc:
            log_event(self.log, "bale_send_photo_failed", chat_id=chat_id, error=str(exc))
            return False

    async def _safe_send_audio(self, chat_id: str, file_obj: Any) -> bool:
        try:
            await self._call_bale_with_retry("send_audio", self.bot.send_audio, chat_id, file_obj)
            return True
        except Exception as exc:
            log_event(self.log, "bale_send_audio_failed", chat_id=chat_id, error=str(exc))
            return False

    async def _safe_send_video(self, chat_id: str, file_obj: Any) -> bool:
        try:
            await self._call_bale_with_retry("send_video", self.bot.send_video, chat_id, file_obj)
            return True
        except Exception as exc:
            log_event(self.log, "bale_send_video_failed", chat_id=chat_id, error=str(exc))
            return False

    async def _safe_send_document(self, chat_id: str, file_obj: Any) -> bool:
        try:
            await self._call_bale_with_retry("send_document", self.bot.send_document, chat_id, file_obj)
            return True
        except Exception as exc:
            log_event(self.log, "bale_send_document_failed", chat_id=chat_id, error=str(exc))
            return False

    async def _handle_message(self, message: bale.Message) -> None:
        if not self._runtime_started:
            await self._safe_send_message(str(message.chat_id), "سرویس هنوز آماده نیست. لطفاً چند ثانیه بعد دوباره تلاش کن.")
            self.log_sink.send_background(
                "WARNING",
                "Service Not Ready",
                "User message received before runtime initialization.",
                {"chat_id": str(message.chat_id), "message_id": str(message.message_id)},
            )
            return

        if message.author and message.author.is_bot:
            return

        if self._is_duplicate_inbound(message):
            return

        user_id = str(message.author.user_id) if message.author else str(message.chat_id)
        chat_id = str(message.chat_id)
        content = (message.content or "").strip()

        if content.startswith("/start"):
            await self._handle_start(user_id, chat_id)
            return

        binding = await self.storage.resolve_tenant_binding(user_id, chat_id)
        if binding is None:
            await self._handle_registration_flow(message, user_id, chat_id, content)
            return

        if content.startswith("/help"):
            await self._safe_send_message(chat_id, ui.HELP_TEXT)
            return

        if content.startswith("/settings"):
            await self._safe_send_message(chat_id, ui.SETTINGS_TEXT)
            return

        if binding.tenant_status != "active":
            await self._safe_send_message(chat_id, "سرویس اختصاصی شما موقتاً غیرفعال است. با ادمین تماس بگیرید.")
            self.log_sink.send_background(
                "WARNING",
                "Tenant Disabled",
                "User tried to use an inactive tenant.",
                {"tenant_id": binding.tenant_id, "tenant_status": binding.tenant_status, "bale_user_id": user_id},
            )
            return

        if binding.runtime_status not in {"running", "healthy", "active", "starting", "created", "recreated"}:
            await self._safe_send_message(chat_id, "سرویس اختصاصی شما در حال حاضر در دسترس نیست. لطفاً کمی بعد دوباره تلاش کن.")
            self.log_sink.send_background(
                "ERROR",
                "Tenant Runtime Unavailable",
                "User hit an unavailable tenant runtime.",
                {"tenant_id": binding.tenant_id, "runtime_status": binding.runtime_status, "bale_user_id": user_id},
            )
            return

        if not binding.endpoint_base_url:
            await self._safe_send_message(chat_id, "سرویس اختصاصی شما هنوز آماده نشده. لطفاً کمی بعد دوباره تلاش کن.")
            self.log_sink.send_background(
                "ERROR",
                "Tenant Endpoint Missing",
                "Tenant binding has no endpoint URL.",
                {"tenant_id": binding.tenant_id, "runtime_status": binding.runtime_status, "bale_user_id": user_id},
            )
            return

        await self._mark_registration_provisioned(user_id, chat_id, binding.tenant_id)

        if content.startswith("/search"):
            query = content.partition(" ")[2].strip()
            if not query:
                await self._safe_send_message(chat_id, "بعد از /search متن جستجو رو بنویس.")
                return
            payload = RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                tenant_id=binding.tenant_id,
                input_type=InputType.WEB_SEARCH,
                text=query,
                user_mode=MODE_WEB_SEARCH,
            )
            await self._queue_with_status(chat_id, binding, payload)
            return

        if content in ui.ALL_MENU_LABELS:
            await self._handle_menu_action(user_id, chat_id, content)
            return

        payload = await self._build_payload(message, user_id, chat_id, content, binding.tenant_id)
        if payload is None:
            return

        await self._queue_with_status(chat_id, binding, payload)

    async def _handle_menu_action(self, user_id: str, chat_id: str, action: str) -> None:
        if action == ui.MENU_CHAT:
            await self.storage.set_user_mode(user_id, MODE_CHAT)
            await self._safe_send_message(chat_id, "حالت چت متنی فعال شد. متن بفرست.")
            return

        if action == ui.MENU_IMAGE:
            await self.storage.set_user_mode(user_id, MODE_IMAGE)
            await self._safe_send_message(chat_id, "حالت تحلیل عکس فعال شد. یک عکس بفرست.")
            return

        if action == ui.MENU_TRANSCRIBE:
            await self.storage.set_user_mode(user_id, MODE_TRANSCRIBE)
            await self._safe_send_message(chat_id, "حالت تبدیل ویس به متن فعال شد. ویس یا صوت بفرست.")
            return

        if action == ui.MENU_SEARCH:
            await self.storage.set_user_mode(user_id, MODE_WEB_SEARCH)
            await self._safe_send_message(chat_id, "حالت جستجو فعال شد. متن درخواست جستجو رو بفرست.")
            return

        if action == ui.MENU_HELP:
            await self._safe_send_message(chat_id, ui.HELP_TEXT)
            return

        if action == ui.MENU_SETTINGS:
            await self._safe_send_message(chat_id, ui.SETTINGS_TEXT)
            return

        if action == ui.MENU_TTS:
            settings = await self.storage.get_user_settings(user_id)
            enabled = not bool(settings.get("tts_enabled", False))
            await self.storage.set_user_tts(user_id, enabled)
            state = "روشن" if enabled else "خاموش"
            await self._safe_send_message(
                chat_id,
                f"وضعیت پاسخ صوتی روی {state} ذخیره شد. این قابلیت در نسخه فعلی غیرفعال است و در فاز ۵ فعال می‌شود.",
            )
            return

    async def _build_payload(
        self,
        message: bale.Message,
        user_id: str,
        chat_id: str,
        content: str,
        tenant_id: str,
    ) -> RequestPayload | None:
        settings = await self.storage.get_user_settings(user_id)
        mode = str(settings.get("mode") or MODE_CHAT)

        if message.photos:
            media = await self._download_photo(message)
            if media is None:
                await self._safe_send_message(chat_id, ui.ERR_MEDIA_SIZE)
                self.log_sink.send_background(
                    "WARNING",
                    "Photo Rejected",
                    "Photo was rejected due to size or download constraints.",
                    {"bale_user_id": user_id, "bale_chat_id": chat_id},
                )
                return None
            return RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                tenant_id=tenant_id,
                input_type=InputType.PHOTO,
                text=content or None,
                caption=message.caption,
                media_bytes=media[0],
                file_name=media[1],
                file_size=media[2],
                mime_type=media[3],
                user_mode=mode,
            )

        has_audio_candidate = message.audio is not None or _is_audio_document(message.document)
        audio_data = await self._download_audio(message)
        if audio_data is not None:
            return RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                tenant_id=tenant_id,
                input_type=InputType.AUDIO,
                text=content or None,
                caption=message.caption,
                media_bytes=audio_data[0],
                file_name=audio_data[1],
                file_size=audio_data[2],
                mime_type=audio_data[3],
                user_mode=mode,
            )
        if has_audio_candidate:
            self.log_sink.send_background(
                "WARNING",
                "Audio Rejected",
                "Audio/voice payload was rejected due to size or download constraints.",
                {"bale_user_id": user_id, "bale_chat_id": chat_id},
            )
            return None

        if content:
            if len(content) > MAX_INPUT_TEXT_CHARS:
                await self._safe_send_message(chat_id, ui.ERR_TOO_LONG)
                return None

            if mode == MODE_IMAGE:
                await self._safe_send_message(chat_id, "لطفاً یک عکس بفرست تا تحلیل کنم.")
                return None
            if mode == MODE_TRANSCRIBE:
                await self._safe_send_message(chat_id, "لطفاً فایل صوتی بفرست تا تبدیل به متن کنم.")
                return None

            input_type = InputType.WEB_SEARCH if mode == MODE_WEB_SEARCH else InputType.TEXT
            return RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                tenant_id=tenant_id,
                input_type=input_type,
                text=content,
                user_mode=mode,
            )

        await self._safe_send_message(chat_id, ui.ERR_UNSUPPORTED)
        return None

    async def _queue_with_status(self, chat_id: str, binding: TenantBinding, payload: RequestPayload) -> None:
        status_message_id = await self._send_ack(chat_id)
        queue_after = await self._enqueue_request(binding, payload, status_message_id=status_message_id)
        if queue_after > 1:
            await self._set_status_message(chat_id, status_message_id, ui.ERR_QUEUE_BUSY)

    async def _enqueue_request(self, binding: TenantBinding, payload: RequestPayload, status_message_id: str | int | None) -> int:
        await self.storage.create_request(payload)

        queue = self._tenant_queues.get(binding.tenant_id)
        if queue is None:
            queue = asyncio.Queue()
            self._tenant_queues[binding.tenant_id] = queue

        queue_depth = queue.qsize()
        await queue.put(
            QueueJob(
                payload=payload,
                chat_id=payload.bale_chat_id,
                tenant_id=binding.tenant_id,
                endpoint_base_url=binding.endpoint_base_url,
                status_message_id=status_message_id,
            )
        )
        self._ensure_tenant_worker(binding.tenant_id)

        log_event(
            self.log,
            "request_queued",
            request_id=payload.request_id,
            tenant_id=binding.tenant_id,
            input_type=payload.input_type.value,
            queue_before=queue_depth,
        )
        return queue_depth + 1

    def _ensure_tenant_worker(self, tenant_id: str) -> None:
        current = self._tenant_workers.get(tenant_id)
        if current and not current.done():
            return
        self._tenant_workers[tenant_id] = asyncio.create_task(self._tenant_worker_loop(tenant_id), name=f"tenant-worker-{tenant_id}")

    async def _tenant_worker_loop(self, tenant_id: str) -> None:
        queue = self._tenant_queues[tenant_id]
        while True:
            job = await queue.get()
            stop_wait_signal = asyncio.Event()
            wait_task = asyncio.create_task(self._status_ping(job.chat_id, job.status_message_id, stop_wait_signal))

            try:
                await self.storage.mark_processing(job.payload.request_id)
                client = await self._get_client(job.endpoint_base_url)
                response = await client.relay(job.payload)

                await self.storage.mark_completed(job.payload.request_id, response)
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self._send_response(job.chat_id, response.text, response.media_parts)
                log_event(self.log, "request_completed", request_id=job.payload.request_id, tenant_id=tenant_id)

            except MiraServiceRemoteError as exc:
                await self.storage.mark_failed(job.payload.request_id, exc.error_code, exc.error_message)
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self._send_remote_error(job.chat_id, exc)
                log_event(
                    self.log,
                    "request_failed_remote",
                    request_id=job.payload.request_id,
                    tenant_id=tenant_id,
                    status_code=exc.status_code,
                    error_code=exc.error_code,
                    error_message=exc.error_message,
                )
                self.log_sink.send_background(
                    "WARNING",
                    "Tenant Relay Remote Error",
                    exc.error_message,
                    {"tenant_id": tenant_id, "error_code": exc.error_code, "request_id": job.payload.request_id},
                )

            except MiraServiceUnavailableError as exc:
                await self.storage.mark_failed(job.payload.request_id, "service_unavailable", str(exc))
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self._safe_send_message(job.chat_id, ui.ERR_TG_TEMP_DOWN)
                log_event(self.log, "request_failed_unavailable", request_id=job.payload.request_id, tenant_id=tenant_id, error=str(exc))
                self.log_sink.send_background(
                    "ERROR",
                    "Tenant Service Unavailable",
                    str(exc),
                    {"tenant_id": tenant_id, "request_id": job.payload.request_id},
                )

            except Exception as exc:
                await self.storage.mark_failed(job.payload.request_id, "internal_error", str(exc))
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self._safe_send_message(job.chat_id, "خطای داخلی رخ داد. لطفاً دوباره امتحان کن.")
                log_event(self.log, "request_failed_internal", request_id=job.payload.request_id, tenant_id=tenant_id, error=str(exc))
                self.log_sink.send_background(
                    "ERROR",
                    "Tenant Worker Internal Error",
                    str(exc),
                    {"tenant_id": tenant_id, "request_id": job.payload.request_id},
                )

            finally:
                if not wait_task.done():
                    stop_wait_signal.set()
                    wait_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await wait_task
                queue.task_done()

    async def _get_client(self, endpoint_base_url: str) -> MiraServiceClient:
        endpoint = endpoint_base_url.rstrip("/")
        async with self._client_lock:
            client = self._client_cache.get(endpoint)
            if client is not None:
                return client

            client = MiraServiceClient(
                base_url=endpoint,
                api_key=self.config.mira_service_api_key,
                timeout_seconds=self.config.request_timeout_seconds,
            )
            await client.start()
            self._client_cache[endpoint] = client
            return client

    async def _send_ack(self, chat_id: str) -> str | int | None:
        try:
            message = await self._safe_send_message(chat_id, ui.ACK_TEXT)
            return message.message_id
        except Exception as exc:
            log_event(self.log, "ack_send_failed", chat_id=chat_id, error=str(exc))
            return None

    async def _send_remote_error(self, chat_id: str, exc: MiraServiceRemoteError) -> None:
        if exc.error_code == "mira_rate_limit":
            await self._safe_send_message(chat_id, ui.ERR_MIRA_LIMIT)
            return
        if exc.error_code == "mira_no_response":
            await self._safe_send_message(chat_id, ui.ERR_NO_RESPONSE)
            return
        if exc.error_code == "telegram_connection":
            await self._safe_send_message(chat_id, ui.ERR_TG_TEMP_DOWN)
            return
        if exc.error_code == "invalid_input":
            await self._safe_send_message(chat_id, ui.ERR_UNSUPPORTED)
            return

        await self._safe_send_message(chat_id, "فعلاً پاسخ‌گیری از Mira با خطا مواجه شد. کمی بعد دوباره امتحان کن.")

    async def _status_ping(self, chat_id: str, status_message_id: str | int | None, stop_event: asyncio.Event) -> None:
        wait_published = False
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.status_ping_seconds)
            except asyncio.TimeoutError:
                if wait_published:
                    continue
                await self._set_status_message(chat_id, status_message_id, ui.WAIT_TEXT)
                wait_published = True

    async def _stop_status_ping(self, stop_event: asyncio.Event, wait_task: asyncio.Task) -> None:
        stop_event.set()
        wait_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wait_task

    async def _set_status_message(self, chat_id: str, status_message_id: str | int | None, text: str) -> None:
        if status_message_id is None:
            await self._safe_send_message(chat_id, text)
            return
        edited = await self._safe_edit_message(chat_id, status_message_id, text)
        if not edited:
            await self._safe_send_message(chat_id, text)

    async def _dismiss_status_message(self, chat_id: str, status_message_id: str | int | None) -> None:
        if status_message_id is None:
            return
        with contextlib.suppress(Exception):
            await self._safe_delete_message(chat_id, status_message_id)

    async def _send_response(self, chat_id: str, text: str, media_parts: list[MiraMediaPart]) -> None:
        text_chunks = split_text(text, self.config.max_text_chunk_chars)
        for chunk in text_chunks:
            await self._safe_send_message(chat_id, chunk)

        for part in media_parts:
            await self._send_media_part(chat_id, part)

        if not text_chunks and not media_parts:
            await self._safe_send_message(chat_id, ui.ERR_NO_RESPONSE)

    async def _send_media_part(self, chat_id: str, part: MiraMediaPart) -> None:
        file_name = part.file_name or _default_out_name(part.media_type)
        input_file = bale.InputFile(part.data, file_name=file_name)

        if part.media_type == "photo":
            await self._safe_send_photo(chat_id, input_file)
            return
        if part.media_type in {"audio", "voice"}:
            await self._safe_send_audio(chat_id, input_file)
            return
        if part.media_type == "video":
            await self._safe_send_video(chat_id, input_file)
            return

        await self._safe_send_document(chat_id, input_file)

    async def _download_photo(self, message: bale.Message) -> tuple[bytes, str, int, str] | None:
        photos = list(message.photos or [])
        if not photos:
            return None

        selected = max(
            photos,
            key=lambda item: ((item.file_size or 0), getattr(item, "width", 0), getattr(item, "height", 0)),
        )
        file_size = int(selected.file_size or 0)
        if file_size and file_size > self.config.max_photo_bytes:
            return None

        data = await selected.get()
        if len(data) > self.config.max_photo_bytes:
            return None

        return (bytes(data), "image.jpg", len(data), "image/jpeg")

    async def _download_audio(self, message: bale.Message) -> tuple[bytes, str, int, str] | None:
        audio_obj = message.audio
        if audio_obj is None and message.document is not None:
            if _is_audio_document(message.document):
                audio_obj = message.document

        if audio_obj is None:
            return None

        file_size = int(audio_obj.file_size or 0)
        if file_size and file_size > self.config.max_audio_bytes:
            await self._safe_send_message(str(message.chat_id), ui.ERR_MEDIA_SIZE)
            return None

        data = await audio_obj.get()
        if len(data) > self.config.max_audio_bytes:
            await self._safe_send_message(str(message.chat_id), ui.ERR_MEDIA_SIZE)
            return None

        file_name = getattr(audio_obj, "file_name", None) or "audio.bin"
        mime = getattr(audio_obj, "mime_type", None) or "application/octet-stream"
        return (bytes(data), file_name, len(data), mime)

    async def _send_welcome(self, chat_id: str) -> None:
        keyboard = _build_menu_keyboard()
        await self._safe_send_message(chat_id, ui.WELCOME_TEXT, components=keyboard)

    async def _handle_start(self, user_id: str, chat_id: str) -> None:
        binding = await self.storage.resolve_tenant_binding(user_id, chat_id)
        if binding is None:
            await self._handle_registration_flow(None, user_id, chat_id, "/start")
            return
        await self._send_welcome(chat_id)

    async def _handle_registration_flow(self, message: bale.Message | None, user_id: str, chat_id: str, content: str) -> None:
        reg = await self.storage.get_registration_request(user_id, chat_id)
        text = content.strip()

        if reg is None:
            reg = RegistrationRequest(
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                status=RegistrationStatus.AWAITING_USERNAME,
            )
            await self.storage.upsert_registration_request(reg)
            await self._safe_send_message(chat_id, ui.REG_START_TEXT)
            return

        if reg.status == RegistrationStatus.AWAITING_USERNAME:
            if not text or text.startswith("/"):
                await self._safe_send_message(chat_id, ui.REG_START_TEXT)
                return
            reg.desired_username = text[:100]
            reg.status = RegistrationStatus.AWAITING_PHONE
            await self.storage.upsert_registration_request(reg)
            await self._safe_send_message(chat_id, ui.REG_ASK_PHONE_TEXT)
            return

        if reg.status == RegistrationStatus.AWAITING_PHONE:
            normalized_phone = self._normalize_phone(text)
            if normalized_phone is None:
                await self._safe_send_message(chat_id, ui.REG_INVALID_PHONE_TEXT)
                self.log_sink.send_background(
                    "WARNING",
                    "Invalid Registration Phone",
                    "User submitted an invalid phone number format.",
                    {"bale_user_id": user_id, "bale_chat_id": chat_id},
                )
                return

            reg.phone_e164 = normalized_phone
            reg.status = RegistrationStatus.PENDING_ADMIN
            await self.storage.upsert_registration_request(reg)
            await self.storage.insert_audit_event(
                tenant_id="registration",
                actor="bale-user",
                event_type="registration_submitted",
                payload={
                    "bale_user_id": user_id,
                    "bale_chat_id": chat_id,
                    "desired_username": reg.desired_username,
                    "phone_e164": normalized_phone,
                },
            )
            self.log_sink.send_background(
                "WARNING",
                "New Registration Request",
                "A new Bale user requested onboarding",
                {
                    "bale_user_id": user_id,
                    "bale_chat_id": chat_id,
                    "desired_username": reg.desired_username or "",
                    "phone_e164": normalized_phone,
                },
            )
            await self._safe_send_message(chat_id, ui.REG_PENDING_TEXT)
            return

        if reg.status == RegistrationStatus.PENDING_ADMIN:
            await self._safe_send_message(chat_id, ui.REG_ALREADY_PENDING_TEXT)
            return

        await self._safe_send_message(chat_id, ui.REG_PENDING_TEXT)

    async def _mark_registration_provisioned(self, user_id: str, chat_id: str, tenant_id: str) -> None:
        reg = await self.storage.get_registration_request(user_id, chat_id)
        if reg is None:
            return
        if reg.status == RegistrationStatus.PROVISIONED and reg.tenant_id == tenant_id:
            return
        reg.status = RegistrationStatus.PROVISIONED
        reg.tenant_id = tenant_id
        await self.storage.upsert_registration_request(reg)

    def _normalize_phone(self, text: str) -> str | None:
        cleaned = text.strip().replace(" ", "")
        if cleaned.startswith("00"):
            cleaned = f"+{cleaned[2:]}"
        if cleaned.startswith("0"):
            cleaned = f"+98{cleaned[1:]}"
        if not PHONE_RE.match(cleaned):
            return None
        if not cleaned.startswith("+"):
            cleaned = f"+{cleaned}"
        return cleaned

    def _is_duplicate_inbound(self, message: bale.Message) -> bool:
        key = (str(message.chat_id), str(message.message_id))
        if key in self._recent_inbound:
            return True

        self._recent_inbound[key] = None
        self._recent_inbound.move_to_end(key)
        if len(self._recent_inbound) > MAX_RECENT_EVENTS:
            self._recent_inbound.popitem(last=False)
        return False


def _is_transient_bale_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "temporary failure in name resolution",
        "cannot connect to host",
        "networkerror",
        "timeout",
        "connection reset",
        "clientconnectorerror",
    )
    return any(marker in text for marker in markers)


def _patch_bale_http_client() -> None:
    global _BALE_HTTP_PATCHED
    if _BALE_HTTP_PATCHED:
        return

    import bale.request.http as bale_http

    original_request = bale_http.HTTPClient.request
    proxy_url = (
        os.getenv("BALE_HTTP_PROXY_URL")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or None
    )

    async def patched_start(self):  # type: ignore[no-untyped-def]
        if self._HTTPClient__session:  # noqa: SLF001
            raise RuntimeError("HTTPClient has already started.")
        self._HTTPClient__session = aiohttp.ClientSession(  # noqa: SLF001
            loop=self.loop,
            connector=aiohttp.TCPConnector(keepalive_timeout=20.0, **self._HTTPClient__extra),  # noqa: SLF001
            trust_env=True,
        )

    def patched_reload(self):  # type: ignore[no-untyped-def]
        if self._HTTPClient__session and self._HTTPClient__session.closed:  # noqa: SLF001
            self._HTTPClient__session = aiohttp.ClientSession(  # noqa: SLF001
                loop=self.loop,
                connector=aiohttp.TCPConnector(keepalive_timeout=20.0, **self._HTTPClient__extra),  # noqa: SLF001
                trust_env=True,
            )

    async def patched_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if proxy_url and "proxy" not in kwargs:
            kwargs["proxy"] = proxy_url
        for attempt in range(1, BALE_RETRY_ATTEMPTS + 1):
            try:
                return await original_request(self, *args, **kwargs)
            except NetworkError as exc:
                if attempt >= BALE_RETRY_ATTEMPTS or not _is_transient_bale_error(exc):
                    raise
                await asyncio.sleep(min(6, 2 ** (attempt - 1)))

    bale_http.HTTPClient.start = patched_start
    bale_http.HTTPClient.reload_session = patched_reload
    bale_http.HTTPClient.request = patched_request
    _BALE_HTTP_PATCHED = True


def _build_menu_keyboard() -> bale.MenuKeyboardMarkup:
    keyboard = bale.MenuKeyboardMarkup()
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_CHAT), row=1)
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_IMAGE), row=1)
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_TRANSCRIBE), row=2)
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_SEARCH), row=2)
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_TTS), row=3)
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_HELP), row=3)
    keyboard.add(bale.MenuKeyboardButton(ui.MENU_SETTINGS), row=4)
    return keyboard


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _is_audio_document(document: object | None) -> bool:
    if document is None:
        return False
    mime = str(getattr(document, "mime_type", "") or "").lower()
    if mime.startswith("audio/"):
        return True
    name = str(getattr(document, "file_name", "") or "").lower()
    return name.endswith((".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac"))


def _default_out_name(media_type: str) -> str:
    if media_type == "photo":
        return "image.jpg"
    if media_type in {"audio", "voice"}:
        return "audio.ogg"
    if media_type == "video":
        return "video.mp4"
    return "file.bin"
