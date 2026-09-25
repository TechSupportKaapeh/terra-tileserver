"""Servidor de tiles de Terra: TiTiler sobre MinIO.

Traduce peticiones XYZ/WMTS a lecturas parciales (HTTP Range) de los COG
guardados en MinIO, usando credenciales de SOLO LECTURA (`tiler-ro`).

**Este servicio no escribe nada.** No sube archivos, no toca el catálogo, no
habla con la base de datos. Quien escribe los COG es el worker.

Este archivo es la *composición*: lee la config, arma las dependencias y las
conecta. La lógica vive en `terra_tiles/`, sin importar TiTiler, para que se
pueda testear sin la pila geoespacial.

Controles de acceso y su categoría OWASP:
  A01 Broken Access Control  -> `crear_validador_de_token` en cada `/cog/*`, y
                                la key acotada al tenant del token (M.8.1)
  A10 SSRF                   -> `crear_validador_de_ruta` sobre `?url=`
  A05 Security Misconfig.    -> sin secreto por defecto; CORS explícito
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # antes de leer la config

# Antes de cualquier otra cosa del servicio: hasta que esto no corre, un
# `logger.info` de `terra_tiles.*` se descarta y un `logger.error` sale sin
# formato por `logging.lastResort`. Uvicorn no configura el root — solo sus
# tres loggers propios — y lo hace en `Config.__init__`, o sea **antes** de
# importar este archivo, asi que aca se le agrega lo que falta sin pisarlo.
from terra_tiles.logging_config import configurar_logging

configurar_logging()

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
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from titiler.core.factory import TilerFactory

from terra_tiles.arranque import registrar_arranque
from terra_tiles.caching import CacheControlMiddleware
from terra_tiles.health import informe
from terra_tiles.png import PNG_TRANSPARENTE
from terra_tiles.security import crear_validador_de_ruta, crear_validador_de_token

# Rutas absolutas respecto de este archivo: con rutas relativas, `StaticFiles`
# y `FileResponse` dependen del directorio desde el que se lanzó uvicorn.
_AQUI = Path(__file__).resolve().parent
_PUBLIC = _AQUI / "public"

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


@app.get("/piloto")
def piloto():
    """Piloto para el front: cómo pintar un COG en un mapa, paso a paso.

    Se sirve desde acá y no sólo como archivo suelto por dos motivos: queda en
    el mismo origen que TiTiler —así las llamadas a `/cog/*` no dependen de
    CORS— y hereda el HTTPS de Railway sin configurar nada. Sin token, igual
    que `/viewer`: la página es pública, los tiles que pide no.
    """
    return FileResponse(_PUBLIC / "piloto.html")


@app.exception_handler(TileOutsideBounds)
async def tile_outside_bounds_handler(request: Request, exc: TileOutsideBounds) -> Response:
    """Fuera de los bordes del raster: PNG transparente, no error.

    Los clientes de mapa dibujan el ícono de "tile roto" ante un error HTTP, y
    en todo el borde del raster eso es ruido visual constante.

    **Transparente de verdad desde el 2026-09-11.** El literal base64 que había
    acá era blanco opaco, y pintaba cuadrados blancos al lado de los rasters
    alineados a la grilla de tiles. Ver `terra_tiles/png.py`.
    """
    return Response(status_code=200, content=PNG_TRANSPARENTE, media_type="image/png")


@app.exception_handler(PointOutsideBounds)
async def point_outside_bounds_handler(request: Request, exc: PointOutsideBounds) -> JSONResponse:
    """Un click fuera del raster es un 404, no un error del servidor.

    TiTiler 0.18 no lo mapea y salía como un 500 pelado, que en la consola del
    front se lee como que algo se rompió.

    Se atiende **sólo este caso, con mensaje fijo**, en vez de registrar
    `titiler.core.errors.add_exception_handlers` entero. Ese registra también un
    manejador para cualquier `Exception` que devuelve `str(exc)` en la
    respuesta, y los errores de GDAL traen el endpoint de S3 adentro —
    verificado: "CURL error: Failed to connect to <host> port <puerto>". Con el
    MinIO privado de Railway eso publicaría `bucket.railway.internal:9000` en
    una respuesta HTTP, que es justo lo que `/health/ready` se cuida de no
    mostrar.
    """
    return JSONResponse(status_code=404, content={"detail": "El punto está fuera del raster."})


# Los dos routers comparten los mismos controles: el token decide QUIÉN pide, y
# el validador de ruta decide QUÉ puede pedirse. Se construyen una sola vez para
# que no puedan divergir entre endpoints.
#
# Desde M.8.1 el segundo **depende** del primero: el token dice de qué tenant es
# quien pide, y la ruta tiene que caer bajo `tenants/{ese tenant}/`. Se le pasa
# la misma función —no otra igual— para que FastAPI la resuelva una sola vez por
# pedido y no haya dos lecturas del claim que puedan discrepar.
validador_token = crear_validador_de_token(settings.map_token_secret,
                                           settings.token_leeway_seconds)
verificar_token = Depends(validador_token)
validar_ruta = crear_validador_de_ruta(settings.prefijo_valido, validador_token)

# Endpoints COG: /cog/tiles/{TileMatrixSetId}/{z}/{x}/{y}, /cog/info, /cog/bounds…
cog = TilerFactory(path_dependency=validar_ruta)
app.include_router(cog.router, prefix="/cog", tags=["COG"], dependencies=[verificar_token])

# El router `/mosaic` se borró el 2026-09-24 (`DECISIONS #64` del worker). Servía
# MosaicJSON —componer varias pasadas al vuelo, `DECISIONS #19` y `#20`— y **no lo
# usaba nadie**: desde el pipeline mensual el mapa de un rancho es un COG por
# índice y por mes, que es lo que pide `/cog`.
#
# Se borra en vez de arreglarse, y eso cierra el hallazgo **T-3** del mapeo OWASP.
# T-3 decía que el validador de ruta acota el MosaicJSON pero **no los assets que
# ese documento lista adentro**: `cogeo-mosaic` los abre tal como vengan, así que
# un documento que listara COG de otro tenant los serviría, salteando el
# aislamiento que M.8.1 puso. Validar los assets era la otra salida; borrar la
# superficie es más barato y no deja nada que se pueda volver a romper.


# Lo ultimo del archivo, cuando los routers ya estan montados: recien aca el
# reporte describe el servicio que efectivamente va a atender.
#
# Va a nivel de modulo y no en un evento de startup **a proposito**. En el
# worker, `serve()` de Inngest levantaba durante el import y el evento
# `startup` nunca llegaba a dispararse, asi que el reporte escrito para
# explicar la falla no se imprimia. Un diagnostico que solo aparece cuando todo
# anda no sirve de nada.
#
# `registrar_arranque` no levanta: atrapa lo suyo y devuelve.
registrar_arranque(settings, os.environ)
