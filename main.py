"""Servidor de tiles de Terra: TiTiler sobre MinIO.

Traduce peticiones XYZ/WMTS a lecturas parciales (HTTP Range) de los COG
guardados en MinIO, usando credenciales de SOLO LECTURA (`tiler-ro`).

**Este servicio no escribe nada.** No sube archivos, no toca el catálogo, no
habla con la base de datos. Quien escribe los COG es el worker.

Este archivo es la *composición*: lee la config, arma las dependencias y las
conecta. La lógica vive en `terra_tiles/`, sin importar TiTiler, para que se
pueda testear sin la pila geoespacial.

Controles de acceso y su categoría OWASP:
  A01 Broken Access Control  -> `crear_validador_de_token` en cada `/cog/*`
  A10 SSRF                   -> `crear_validador_de_ruta` sobre `?url=`
  A05 Security Misconfig.    -> sin secreto por defecto; CORS explícito
"""
from __future__ import annotations

from base64 import b64decode
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # antes de leer la config

from terra_tiles.settings import Settings, configure_gdal

settings = Settings.from_env()

# GDAL lee estas variables al cargarse. Fijarlas después de importar TiTiler
# no tiene efecto y las lecturas fallan con errores que no mencionan la causa.
configure_gdal(settings)

# >>>>> A PARTIR DE ACÁ SÍ SE PUEDE IMPORTAR TiTiler <<<<<
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from rio_tiler.errors import TileOutsideBounds
from titiler.core.factory import TilerFactory

from terra_tiles.caching import CacheControlMiddleware
from terra_tiles.health import informe
from terra_tiles.security import crear_validador_de_ruta, crear_validador_de_token

# Rutas absolutas respecto de este archivo: con rutas relativas, `StaticFiles`
# y `FileResponse` dependen del directorio desde el que se lanzó uvicorn.
_AQUI = Path(__file__).resolve().parent
_PUBLIC = _AQUI / "public"

_TRANSPARENT_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7l3l8AAAAASUVORK5CYII="
)

app = FastAPI(title="Tileserver Terra (TiTiler + MinIO)")

app.add_middleware(
    CacheControlMiddleware,
    prefijo="/cog/tiles",
    max_age=settings.tile_cache_seconds,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],   # el servicio es de solo lectura
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(_PUBLIC)), name="static")


@app.get("/health")
def health():
    """Liveness para Railway. Sin token: tiene que responder aunque falte config.

    **No consulta MinIO a propósito.** Este es el chequeo que Railway usa para
    decidir si reinicia el contenedor: si dependiera del storage, un parpadeo de
    MinIO reiniciaría un tileserver sano, y reiniciarlo no arregla nada de lo
    que falló. Para saber si el servicio puede trabajar, `/health/ready`.
    """
    return {"status": "ok", "service": "tileserver-titiler"}


@app.get("/health/ready")
def ready():
    """Readiness: config completa y MinIO alcanzable. Para diagnosticar un deploy.

    Declarada `def` y no `async def`: el sondeo a MinIO es una llamada de red
    bloqueante, y FastAPI corre las funciones síncronas en un threadpool. Como
    `async def` bloquearía el event loop entero mientras espera el timeout.

    No lleva token. Solo publica el nombre del bucket —que ya viaja en cada URL
    de tile— y nunca el endpoint interno ni las credenciales.
    """
    cuerpo, codigo = informe(settings)
    return JSONResponse(cuerpo, status_code=codigo)


@app.get("/viewer")
def viewer():
    """Visor de diagnóstico. Los tiles que pida igual necesitan token."""
    return FileResponse(_PUBLIC / "leaflet-cog.html")


@app.exception_handler(TileOutsideBounds)
async def tile_outside_bounds_handler(request: Request, exc: TileOutsideBounds) -> Response:
    """Fuera de los bordes del raster: PNG transparente, no error.

    Los clientes de mapa dibujan el ícono de "tile roto" ante un error HTTP, y
    en todo el borde del raster eso es ruido visual constante.
    """
    return Response(status_code=200, content=_TRANSPARENT_PNG, media_type="image/png")


# Endpoints COG: /cog/tiles/{TileMatrixSetId}/{z}/{x}/{y}, /cog/info, /cog/bounds…
cog = TilerFactory(path_dependency=crear_validador_de_ruta(settings.prefijo_valido))
app.include_router(
    cog.router,
    prefix="/cog",
    tags=["COG"],
    dependencies=[Depends(crear_validador_de_token(settings.map_token_secret,
                                                   settings.token_leeway_seconds))],
)
