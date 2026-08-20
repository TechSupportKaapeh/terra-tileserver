"""
tileserver-titiler/main.py

Servidor de tiles dinámicos. Lee directamente desde MinIO vía HTTP Range
Requests (GDAL/VSIS3) usando credenciales de SOLO LECTURA (usuario tiler-ro,
policy readonly en MinIO).

Este servicio NO escribe nada. No sube archivos, no toca el catálogo.
Su única responsabilidad es traducir peticiones de tiles XYZ/WMTS a lecturas
parciales de los COG almacenados en MinIO.
"""
from __future__ import annotations

from base64 import b64decode
import logging
import os

from dotenv import load_dotenv


load_dotenv()  # debe ir ANTES de fijar variables GDAL/VSIS3 y de importar TiTiler

# ============================================================
#  BLOQUE ENV PARA GDAL/VSIS3 + MINIO (ANTES de importar TiTiler)
# ============================================================
os.environ["AWS_ACCESS_KEY_ID"] = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
os.environ["AWS_SECRET_ACCESS_KEY"] = os.getenv("MINIO_SECRET_KEY", "minioadmin")
os.environ["AWS_REGION"] = os.getenv("AWS_REGION", "us-east-1")

_endpoint_raw = os.getenv("MINIO_ENDPOINT", "localhost:9000")
if _endpoint_raw.startswith("http://"):
    _endpoint_raw = _endpoint_raw[len("http://"):]
elif _endpoint_raw.startswith("https://"):
    _endpoint_raw = _endpoint_raw[len("https://"):]
os.environ["AWS_S3_ENDPOINT"] = _endpoint_raw

os.environ["AWS_S3_ADDRESSING_STYLE"] = "path"
os.environ["AWS_VIRTUAL_HOSTING"] = "FALSE"
os.environ["AWS_HTTPS"] = "YES" if os.getenv("MINIO_SECURE", "false").lower() == "true" else "NO"

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff,.json,.xml")

# >>>>> IMPORTAR TiTiler DESPUÉS de fijar las variables de entorno <<<<<
from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from rio_tiler.errors import TileOutsideBounds
from titiler.core.factory import TilerFactory
from fastapi.staticfiles import StaticFiles
import jwt

app = FastAPI(title="Tileserver Terra IO (TiTiler + MinIO)")

_TRANSPARENT_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7l3l8AAAAASUVORK5CYII="
)


# Esto servirá todo lo que pongas en la carpeta 'public'
app.mount("/static", StaticFiles(directory="public"), name="static")


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "tileserver-titiler"}


@app.exception_handler(TileOutsideBounds)
async def tile_outside_bounds_handler(request: Request, exc: TileOutsideBounds):
    return Response(status_code=404, content="Tile outside bounds")

from fastapi.responses import FileResponse

@app.get("/viewer")
def viewer():
    return FileResponse("public/leaflet-cog.html")


# ============================================================
#  Validación del map-token (la otra mitad de DECISIONS #16)
# ============================================================
# Geocore firma un JWT HS256 de 1 h con `GeoData:MapTokenSecret`; acá se valida
# con el MISMO valor, bajo el nombre MAP_TOKEN_SECRET. Si los dos lados no
# tienen el mismo secreto, todos los tiles devuelven 401.
#
# SIN DEFAULT, a propósito. Antes había uno hardcodeado en el repo, lo que hacía
# que un deploy mal configurado fallara ABIERTO: cualquiera que leyera el código
# podía forjar un token válido para cualquier COG del bucket. Geocore ya cerró
# ese mismo agujero de su lado (quitó `?? DevMapTokenSecret`) y devuelve 503
# cuando le falta el secreto; acá se replica esa conducta y ese código de error,
# para que los dos lados fallen igual y sea diagnosticable.
MAP_TOKEN_SECRET = os.getenv("MAP_TOKEN_SECRET")

if not MAP_TOKEN_SECRET:
    logging.getLogger("uvicorn.error").error(
        "MAP_TOKEN_SECRET no está configurada: /cog/* devolverá 503. "
        "Tiene que valer lo mismo que GeoData__MapTokenSecret en Geocore."
    )


def verify_map_token(token: str = None):
    # Falla cerrado: sin secreto no se valida nada, no se sirve nada.
    if not MAP_TOKEN_SECRET:
        raise HTTPException(
            status_code=503,
            detail={"code": "MAP_TOKEN_UNAVAILABLE",
                    "message": "El servidor de tiles no tiene configurado el secreto del token."},
        )
    if not token:
        raise HTTPException(status_code=401, detail="Falta el token de mapa. Agregá '?token=...' a la URL.")
    try:
        payload = jwt.decode(token, MAP_TOKEN_SECRET, algorithms=["HS256"], audience="TiTiler", issuer="Geocore")
        if payload.get("type") != "map-access":
            raise HTTPException(status_code=403, detail="Tipo de token inválido")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="El token de mapa expiró")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Token de mapa inválido: {e}")

# Endpoints COG: /cog/tiles/{TileMatrixSetId}/{z}/{x}/{y}, /cog/bounds, /cog/info, etc.
cog = TilerFactory()
app.include_router(cog.router, prefix="/cog", tags=["COG"], dependencies=[Depends(verify_map_token)])

# Ejecutar con: uvicorn main:app --reload --port 8000
