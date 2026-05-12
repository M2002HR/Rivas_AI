from __future__ import annotations

from collections.abc import Callable

from aiohttp import web


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        readiness_probe: Callable[[], bool],
    ) -> None:
        self._host = host
        self._port = port
        self._readiness_probe = readiness_probe
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/ready", self._ready)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _health(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _ready(self, _: web.Request) -> web.Response:
        is_ready = self._readiness_probe()
        status = 200 if is_ready else 503
        return web.json_response({"ready": is_ready}, status=status)
