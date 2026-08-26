"""Cabeceras de caché para los tiles.

Un tile de un raster en una fecha dada **es inmutable**: el COG ya está escrito
y nunca cambia. Decírselo al navegador elimina la mayoría de las peticiones
repetidas durante el pan y el zoom, sin agregar infraestructura.

Es el paso barato antes de pensar en un caché compartido (Redis): resuelve el
caso dominante —un usuario moviéndose por el mismo mapa— a costo cero.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Marca como cacheables solo las respuestas exitosas bajo un prefijo.

    Dos restricciones deliberadas:

    - **Solo 200.** Cachear un 4xx o un 5xx congela un error transitorio en el
      navegador del usuario durante un año. Un fallo de red al leer el COG no
      debe volverse permanente.
    - **Solo bajo `prefijo`.** `/health` tiene que reflejar el estado actual, y
      `/cog/info` se pide una vez por capa: no aporta y complica el diagnóstico.
    """

    def __init__(self, app: ASGIApp, *, prefijo: str = "/cog/tiles", max_age: int = 31_536_000) -> None:
        super().__init__(app)
        self._prefijo = prefijo
        self._valor = f"public, max-age={max_age}, immutable"

    async def dispatch(self, request: Request, call_next) -> Response:
        respuesta = await call_next(request)
        if respuesta.status_code == 200 and request.url.path.startswith(self._prefijo):
            respuesta.headers["Cache-Control"] = self._valor
        return respuesta
