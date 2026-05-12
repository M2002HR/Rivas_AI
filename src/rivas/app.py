from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import OrderedDict
from dataclasses import dataclass

import bale

from . import ui
from .config import BotConfig
from .health import HealthServer
from .logging_utils import get_logger, log_event
from .mira_service.client import MiraServiceClient, MiraServiceRemoteError, MiraServiceUnavailableError
from .models import InputType, MiraMediaPart, RequestPayload
from .storage import Storage
from .text_utils import split_text

MODE_CHAT = "chat"
MODE_IMAGE = "image"
MODE_TRANSCRIBE = "transcribe"
MODE_WEB_SEARCH = "web_search"

MAX_INPUT_TEXT_CHARS = 8_000
MAX_RECENT_EVENTS = 8_000


@dataclass(slots=True)
class QueueJob:
    payload: RequestPayload
    chat_id: str
    status_message_id: str | int | None = None


class RivasApp:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.log = get_logger("rivas.app")
        self.bot = bale.Bot(token=config.bale_bot_token)
        self.storage = Storage(config.sqlite_path, config.retention_days)
        self.mira_client = MiraServiceClient(
            base_url=config.mira_service_url,
            api_key=config.mira_service_api_key,
            timeout_seconds=config.request_timeout_seconds,
        )
        self.health = HealthServer(config.health_host, config.health_port, self.is_ready)

        self.queue: asyncio.Queue[QueueJob] = asyncio.Queue()
        self._runtime_started = False
        self._runtime_lock = asyncio.Lock()
        self._ready = False
        self._mira_ready = False

        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._service_probe_task: asyncio.Task | None = None

        self._recent_inbound: OrderedDict[tuple[str, str], None] = OrderedDict()

        self._register_events()

    def run(self) -> None:
        self.bot.run()

    def is_ready(self) -> bool:
        return self._ready and self._runtime_started and self._mira_ready

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
            await self._handle_message(message)

        @self.bot.event
        async def on_message_edit(_: bale.Message):
            return

        @self.bot.event
        async def on_error(event_name, error):
            log_event(self.log, "bale_event_error", event_name=event_name, error=str(error))

    async def start_runtime(self) -> None:
        async with self._runtime_lock:
            if self._runtime_started:
                return

            await self.storage.init()
            await self.storage.cleanup_expired()
            await self.mira_client.start()
            self._mira_ready = await self.mira_client.is_ready()
            await self.health.start()

            self._worker_task = asyncio.create_task(self._worker_loop(), name="rivas-worker")
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="rivas-cleaner")
            self._service_probe_task = asyncio.create_task(self._probe_service_loop(), name="rivas-service-probe")

            self._runtime_started = True
            self._ready = True
            log_event(self.log, "runtime_started", db=str(self.config.sqlite_path), mira_service_url=self.config.mira_service_url)

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

    async def _probe_service_loop(self) -> None:
        while True:
            try:
                self._mira_ready = await self.mira_client.is_ready()
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._mira_ready = False
                log_event(self.log, "service_probe_error", error=str(exc))
                await asyncio.sleep(5)

    async def _handle_message(self, message: bale.Message) -> None:
        if not self._runtime_started:
            await message.reply("سرویس هنوز آماده نیست. لطفاً چند ثانیه بعد دوباره تلاش کن.")
            return

        if message.author and message.author.is_bot:
            return

        if self._is_duplicate_inbound(message):
            return

        user_id = str(message.author.user_id) if message.author else str(message.chat_id)
        chat_id = str(message.chat_id)
        content = (message.content or "").strip()

        if content.startswith("/start"):
            await self._send_welcome(chat_id)
            return

        if content.startswith("/help"):
            await self.bot.send_message(chat_id, ui.HELP_TEXT)
            return

        if content.startswith("/settings"):
            await self.bot.send_message(chat_id, ui.SETTINGS_TEXT)
            return

        if content.startswith("/search"):
            query = content.partition(" ")[2].strip()
            if not query:
                await self.bot.send_message(chat_id, "بعد از /search متن جستجو رو بنویس.")
                return
            payload = RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                input_type=InputType.WEB_SEARCH,
                text=query,
                user_mode=MODE_WEB_SEARCH,
            )
            await self._queue_with_status(chat_id, payload)
            return

        if content in ui.ALL_MENU_LABELS:
            await self._handle_menu_action(user_id, chat_id, content)
            return

        payload = await self._build_payload(message, user_id, chat_id, content)
        if payload is None:
            return

        await self._queue_with_status(chat_id, payload)

    async def _handle_menu_action(self, user_id: str, chat_id: str, action: str) -> None:
        if action == ui.MENU_CHAT:
            await self.storage.set_user_mode(user_id, MODE_CHAT)
            await self.bot.send_message(chat_id, "حالت چت متنی فعال شد. متن بفرست.")
            return

        if action == ui.MENU_IMAGE:
            await self.storage.set_user_mode(user_id, MODE_IMAGE)
            await self.bot.send_message(chat_id, "حالت تحلیل عکس فعال شد. یک عکس بفرست.")
            return

        if action == ui.MENU_TRANSCRIBE:
            await self.storage.set_user_mode(user_id, MODE_TRANSCRIBE)
            await self.bot.send_message(chat_id, "حالت تبدیل ویس به متن فعال شد. ویس یا صوت بفرست.")
            return

        if action == ui.MENU_SEARCH:
            await self.storage.set_user_mode(user_id, MODE_WEB_SEARCH)
            await self.bot.send_message(chat_id, "حالت جستجو فعال شد. متن درخواست جستجو رو بفرست.")
            return

        if action == ui.MENU_HELP:
            await self.bot.send_message(chat_id, ui.HELP_TEXT)
            return

        if action == ui.MENU_SETTINGS:
            await self.bot.send_message(chat_id, ui.SETTINGS_TEXT)
            return

        if action == ui.MENU_TTS:
            settings = await self.storage.get_user_settings(user_id)
            enabled = not bool(settings.get("tts_enabled", False))
            await self.storage.set_user_tts(user_id, enabled)
            state = "روشن" if enabled else "خاموش"
            await self.bot.send_message(
                chat_id,
                f"وضعیت پاسخ صوتی روی {state} ذخیره شد. این قابلیت در نسخه فعلی غیرفعال است و در فاز ۵ فعال می‌شود.",
            )
            return

    async def _build_payload(self, message: bale.Message, user_id: str, chat_id: str, content: str) -> RequestPayload | None:
        settings = await self.storage.get_user_settings(user_id)
        mode = str(settings.get("mode") or MODE_CHAT)

        if message.photos:
            media = await self._download_photo(message)
            if media is None:
                await self.bot.send_message(chat_id, ui.ERR_MEDIA_SIZE)
                return None
            return RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
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
            return None

        if content:
            if len(content) > MAX_INPUT_TEXT_CHARS:
                await self.bot.send_message(chat_id, ui.ERR_TOO_LONG)
                return None

            if mode == MODE_IMAGE:
                await self.bot.send_message(chat_id, "لطفاً یک عکس بفرست تا تحلیل کنم.")
                return None
            if mode == MODE_TRANSCRIBE:
                await self.bot.send_message(chat_id, "لطفاً فایل صوتی بفرست تا تبدیل به متن کنم.")
                return None

            input_type = InputType.WEB_SEARCH if mode == MODE_WEB_SEARCH else InputType.TEXT
            return RequestPayload(
                request_id=_new_request_id(),
                bale_user_id=user_id,
                bale_chat_id=chat_id,
                input_type=input_type,
                text=content,
                user_mode=mode,
            )

        await self.bot.send_message(chat_id, ui.ERR_UNSUPPORTED)
        return None

    async def _queue_with_status(self, chat_id: str, payload: RequestPayload) -> None:
        status_message_id = await self._send_ack(chat_id)
        queue_after = await self._enqueue_request(payload, status_message_id=status_message_id)
        if queue_after > 1:
            await self._set_status_message(chat_id, status_message_id, ui.ERR_QUEUE_BUSY)

    async def _enqueue_request(self, payload: RequestPayload, status_message_id: str | int | None) -> int:
        await self.storage.create_request(payload)
        queue_depth = self.queue.qsize()
        await self.queue.put(QueueJob(payload=payload, chat_id=payload.bale_chat_id, status_message_id=status_message_id))
        log_event(
            self.log,
            "request_queued",
            request_id=payload.request_id,
            input_type=payload.input_type.value,
            queue_before=queue_depth,
        )
        return queue_depth + 1

    async def _send_ack(self, chat_id: str) -> str | int | None:
        try:
            message = await self.bot.send_message(chat_id, ui.ACK_TEXT)
            return message.message_id
        except Exception as exc:
            log_event(self.log, "ack_send_failed", chat_id=chat_id, error=str(exc))
            return None

    async def _worker_loop(self) -> None:
        while True:
            job = await self.queue.get()
            stop_wait_signal = asyncio.Event()
            wait_task = asyncio.create_task(self._status_ping(job.chat_id, job.status_message_id, stop_wait_signal))

            try:
                await self.storage.mark_processing(job.payload.request_id)
                response = await self.mira_client.relay(job.payload)

                await self.storage.mark_completed(job.payload.request_id, response)
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self._send_response(job.chat_id, response.text, response.media_parts)
                log_event(self.log, "request_completed", request_id=job.payload.request_id)

            except MiraServiceRemoteError as exc:
                await self.storage.mark_failed(job.payload.request_id, exc.error_code, exc.error_message)
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self._send_remote_error(job.chat_id, exc)
                log_event(
                    self.log,
                    "request_failed_remote",
                    request_id=job.payload.request_id,
                    status_code=exc.status_code,
                    error_code=exc.error_code,
                    error_message=exc.error_message,
                )

            except MiraServiceUnavailableError as exc:
                await self.storage.mark_failed(job.payload.request_id, "service_unavailable", str(exc))
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self.bot.send_message(job.chat_id, ui.ERR_TG_TEMP_DOWN)
                log_event(self.log, "request_failed_unavailable", request_id=job.payload.request_id, error=str(exc))

            except Exception as exc:
                await self.storage.mark_failed(job.payload.request_id, "internal_error", str(exc))
                await self._stop_status_ping(stop_wait_signal, wait_task)
                await self._dismiss_status_message(job.chat_id, job.status_message_id)
                await self.bot.send_message(job.chat_id, "خطای داخلی رخ داد. لطفاً دوباره امتحان کن.")
                log_event(self.log, "request_failed_internal", request_id=job.payload.request_id, error=str(exc))

            finally:
                if not wait_task.done():
                    stop_wait_signal.set()
                    wait_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await wait_task
                self.queue.task_done()

    async def _send_remote_error(self, chat_id: str, exc: MiraServiceRemoteError) -> None:
        if exc.error_code == "mira_rate_limit":
            await self.bot.send_message(chat_id, ui.ERR_MIRA_LIMIT)
            return
        if exc.error_code == "mira_no_response":
            await self.bot.send_message(chat_id, ui.ERR_NO_RESPONSE)
            return
        if exc.error_code == "telegram_connection":
            await self.bot.send_message(chat_id, ui.ERR_TG_TEMP_DOWN)
            return
        if exc.error_code == "invalid_input":
            await self.bot.send_message(chat_id, ui.ERR_UNSUPPORTED)
            return

        await self.bot.send_message(chat_id, "فعلاً پاسخ‌گیری از Mira با خطا مواجه شد. کمی بعد دوباره امتحان کن.")

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
            await self.bot.send_message(chat_id, text)
            return
        try:
            await self.bot.edit_message(chat_id, status_message_id, text)
        except Exception:
            await self.bot.send_message(chat_id, text)

    async def _dismiss_status_message(self, chat_id: str, status_message_id: str | int | None) -> None:
        if status_message_id is None:
            return
        with contextlib.suppress(Exception):
            await self.bot.delete_message(chat_id, status_message_id)

    async def _send_response(self, chat_id: str, text: str, media_parts: list[MiraMediaPart]) -> None:
        text_chunks = split_text(text, self.config.max_text_chunk_chars)
        for chunk in text_chunks:
            await self.bot.send_message(chat_id, chunk)

        for part in media_parts:
            await self._send_media_part(chat_id, part)

        if not text_chunks and not media_parts:
            await self.bot.send_message(chat_id, ui.ERR_NO_RESPONSE)

    async def _send_media_part(self, chat_id: str, part: MiraMediaPart) -> None:
        file_name = part.file_name or _default_out_name(part.media_type)
        input_file = bale.InputFile(part.data, file_name=file_name)

        if part.media_type == "photo":
            await self.bot.send_photo(chat_id, input_file)
            return
        if part.media_type in {"audio", "voice"}:
            await self.bot.send_audio(chat_id, input_file)
            return
        if part.media_type == "video":
            await self.bot.send_video(chat_id, input_file)
            return

        await self.bot.send_document(chat_id, input_file)

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
            await self.bot.send_message(str(message.chat_id), ui.ERR_MEDIA_SIZE)
            return None

        data = await audio_obj.get()
        if len(data) > self.config.max_audio_bytes:
            await self.bot.send_message(str(message.chat_id), ui.ERR_MEDIA_SIZE)
            return None

        file_name = getattr(audio_obj, "file_name", None) or "audio.bin"
        mime = getattr(audio_obj, "mime_type", None) or "application/octet-stream"
        return (bytes(data), file_name, len(data), mime)

    async def _send_welcome(self, chat_id: str) -> None:
        keyboard = _build_menu_keyboard()
        await self.bot.send_message(chat_id, ui.WELCOME_TEXT, components=keyboard)

    def _is_duplicate_inbound(self, message: bale.Message) -> bool:
        key = (str(message.chat_id), str(message.message_id))
        if key in self._recent_inbound:
            return True

        self._recent_inbound[key] = None
        self._recent_inbound.move_to_end(key)
        if len(self._recent_inbound) > MAX_RECENT_EVENTS:
            self._recent_inbound.popitem(last=False)
        return False


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


def _default_out_name(media_type: str) -> str:
    if media_type == "photo":
        return "image.jpg"
    if media_type == "audio":
        return "audio.mp3"
    if media_type == "voice":
        return "voice.ogg"
    if media_type == "video":
        return "video.mp4"
    return "file.bin"


def _is_audio_document(document: bale.Document | None) -> bool:
    if document is None:
        return False
    mime = (document.mime_type or "").lower()
    name = (document.file_name or "").lower()
    return mime.startswith("audio/") or name.endswith((".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac"))
