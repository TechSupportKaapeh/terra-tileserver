"""Configuración del tileserver, leída del entorno.

Este módulo **no importa TiTiler ni rasterio** a propósito: `configure_gdal()`
tiene que correr antes de que GDAL se cargue, y los tests tienen que poder
importar la configuración sin arrastrar la pila geoespacial entera.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Valores de configuración, resueltos una sola vez al arrancar.

    Es `frozen` para que nadie los mute en caliente: si cambia la config,
    se reinicia el proceso. Un servidor que cambia de secreto a mitad de
    vuelo es imposible de razonar.
    """

    # Secreto compartido con Geocore (`GeoData:MapTokenSecret`).
    # `None` es un estado válido y significa "no se puede servir nada":
    # ver `crear_validador_de_token`.
    map_token_secret: str | None

    # Único bucket del que se sirven COG. Tiene que coincidir con
    # `GeoData:MinioBucket` de Geocore.
    minio_bucket: str

    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool
    aws_region: str

    cors_origins: list[str] = field(default_factory=lambda: ["*"])

    # Los tiles de un raster en una fecha dada son inmutables, así que se
    # pueden cachear agresivamente. Un año es la práctica habitual para
    # contenido inmutable.
    tile_cache_seconds: int = 31_536_000

    # Margen para desfase de reloj al validar el JWT. Geocore y este servicio
    # corren en máquinas distintas.
    token_leeway_seconds: int = 30

    @property
    def prefijo_valido(self) -> str:
        """Prefijo que toda `?url=` tiene que respetar."""
        return f"s3://{self.minio_bucket}/"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            map_token_secret=os.getenv("MAP_TOKEN_SECRET") or None,
            minio_bucket=os.getenv("MINIO_BUCKET", "terra-assets"),
            minio_endpoint=_sin_esquema(os.getenv("MINIO_ENDPOINT", "localhost:9000")),
            minio_access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            minio_secure=os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes"),
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            cors_origins=[o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()],
            tile_cache_seconds=int(os.getenv("TILE_CACHE_SECONDS", "31536000")),
        )


def _sin_esquema(endpoint: str) -> str:
    """GDAL espera `host:puerto` en AWS_S3_ENDPOINT, sin `http://`.

    Se acepta con esquema porque es el error de configuración más común y no
    vale la pena que rompa el arranque por eso.
    """
    for esquema in ("http://", "https://"):
        if endpoint.startswith(esquema):
            endpoint = endpoint[len(esquema):]
            break
    # La barra final se quita SIEMPRE, con o sin esquema: pegar el dominio de
    # Railway con `/` al final es el error más natural del mundo, y GDAL falla
    # con un mensaje que no menciona la barra.
    return endpoint.rstrip("/")


def configure_gdal(settings: Settings) -> None:
    """Fija las variables que GDAL lee para hablar S3 con MinIO.

    **Tiene que llamarse ANTES de importar TiTiler o rasterio.** GDAL lee estas
    variables al cargarse; si se fijan después, se ignoran en silencio y las
    lecturas fallan con errores que no mencionan la causa.

    También configura **boto3**, que no comparte ninguna variable con GDAL. Lo
    usa `cogeo-mosaic` para leer un MosaicJSON guardado en el bucket
    (`S3Backend` crea su cliente sin `endpoint_url`), así que sin esto apuntaría
    a AWS de verdad en vez de a MinIO. Se fija acá, desde la misma `Settings`,
    para que las dos vías de acceso al bucket no puedan divergir.
    """
    os.environ["AWS_ACCESS_KEY_ID"] = settings.minio_access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = settings.minio_secret_key
    os.environ["AWS_REGION"] = settings.aws_region
    os.environ["AWS_S3_ENDPOINT"] = settings.minio_endpoint

    # boto3 quiere la URL completa con esquema; GDAL quiere host:puerto pelado.
    esquema = "https" if settings.minio_secure else "http"
    os.environ["AWS_ENDPOINT_URL_S3"] = f"{esquema}://{settings.minio_endpoint}"

    # MinIO usa rutas (`host/bucket/key`), no subdominios como S3 de AWS.
    os.environ["AWS_S3_ADDRESSING_STYLE"] = "path"
    os.environ["AWS_VIRTUAL_HOSTING"] = "FALSE"
    os.environ["AWS_HTTPS"] = "YES" if settings.minio_secure else "NO"

    # Evita que GDAL liste el "directorio" en cada apertura: en object storage
    # eso es una llamada de red extra por tile.
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff,.json,.xml")
