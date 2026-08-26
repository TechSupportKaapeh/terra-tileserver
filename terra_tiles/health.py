"""Chequeos de estado del tileserver.

Dos preguntas distintas, y mezclarlas es el error clásico:

- **liveness** — ¿el proceso está vivo? Es lo que consulta Railway para decidir
  si reinicia el contenedor. **No puede depender de MinIO.** Si un parpadeo del
  storage tumbara este chequeo, Railway reiniciaría un servicio que está sano,
  y reiniciarlo no arregla nada de lo que falla. Además tiene que responder
  aunque falte configuración, o un deploy mal configurado queda en un bucle de
  reinicios sin llegar nunca a mostrar el motivo.

- **readiness** — ¿este servicio puede hacer su trabajo? Comprueba la config y
  habla con MinIO de verdad. Sirve para diagnosticar un deploy, no para que un
  orquestador reinicie nada.

Este módulo no importa TiTiler ni rasterio: se testea sin la pila geoespacial.

## Cómo se sondea MinIO, y por qué así

Se pide `stat_object` sobre una key que **no existe a propósito**. Suena raro,
pero es el sondeo más informativo disponible para un usuario de solo lectura:

- Necesita el permiso `s3:GetObject`, que es exactamente el que usa el
  tileserver para servir tiles. Un `bucket_exists()` (HEAD al bucket) necesita
  `s3:ListBucket`, que la policy `readonly` de MinIO **no incluye**: daría
  fallo en un servicio que sirve tiles perfectamente.
- No necesita que haya ningún COG subido. Hoy el bucket está vacío.
- La respuesta distingue las formas conocidas de romper el deploy, cada una con
  un código propio (ver `_MOTIVOS`).

**Lo que este sondeo NO prueba:** que la configuración de GDAL sea correcta.
El cliente de MinIO y GDAL/`vsis3` hablan con el mismo servidor pero por vías
distintas, y detalles como `AWS_S3_ADDRESSING_STYLE=path` solo los ejercita
GDAL. Salen de la misma `Settings`, así que es improbable que difieran, pero
"improbable" no es "verificado": eso lo cierra recién el primer tile real.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from terra_tiles.settings import Settings

logger = logging.getLogger("terra_tiles.health")

# Key deliberadamente inexistente. El prefijo con guion bajo la mantiene fuera
# del espacio de nombres real (ranchos/, parcelas/, heatmaps/, exports/).
KEY_DE_SONDEO = "_healthcheck/probe.tif"

# Segundos antes de rendirse con MinIO. Corto a propósito: este endpoint se
# consulta para diagnosticar, y un diagnóstico que tarda medio minuto en decir
# "no responde" es peor que uno que lo dice enseguida.
TIMEOUT_SEGUNDOS = 5.0

# Estados posibles. `degradado` existe porque hay una respuesta de MinIO que
# honestamente no permite concluir ni que funciona ni que está roto.
OK = "ok"
DEGRADADO = "degradado"
ERROR = "error"

# Códigos de error de S3 mapeados a la causa real. Son exactamente los modos de
# fallo que aparecen al configurar el deploy.
_MOTIVOS: dict[str, tuple[str, str]] = {
    "NoSuchKey": (
        OK,
        "MinIO respondió: el bucket existe y las credenciales pueden leer.",
    ),
    "NoSuchBucket": (
        ERROR,
        "El bucket no existe con ese nombre. Revisá MINIO_BUCKET: tiene que "
        "coincidir con GeoData__MinioBucket de Geocore, y los nombres de bucket "
        "no admiten guion bajo.",
    ),
    "InvalidAccessKeyId": (
        ERROR,
        "MINIO_ACCESS_KEY no corresponde a ningún usuario de MinIO.",
    ),
    "SignatureDoesNotMatch": (
        ERROR,
        "MINIO_SECRET_KEY es incorrecta para ese access key.",
    ),
    "AccessDenied": (
        DEGRADADO,
        "MinIO respondió y aceptó las credenciales, pero negó la lectura. "
        "Puede ser que la policy de tiler-ro no otorgue s3:GetObject sobre este "
        "bucket, o que solo esté ocultando que la key no existe. No se puede "
        "concluir desde acá: verificá la policy en la consola de MinIO.",
    ),
}


@dataclass(frozen=True)
class Comprobacion:
    """Resultado de un chequeo individual.

    `detalle` es para que lo lea una persona y **nunca lleva secretos ni el
    hostname interno**: este endpoint es público, y el hostname de la red
    privada es justamente lo que un atacante querría enumerar.
    """

    nombre: str
    estado: str
    detalle: str

    @property
    def ok(self) -> bool:
        return self.estado != ERROR


def comprobar_token_secret(settings: Settings) -> Comprobacion:
    """¿Está el secreto compartido con Geocore?

    Sin él el servicio arranca y `/health` da 200, pero **todos** los tiles dan
    503. Es el fallo más caro de diagnosticar del deploy, porque todo parece
    sano hasta que alguien abre el mapa.
    """
    if settings.map_token_secret:
        return Comprobacion("map_token_secret", OK, "Configurado.")
    return Comprobacion(
        "map_token_secret",
        ERROR,
        "Falta MAP_TOKEN_SECRET: /cog/* va a devolver 503 en todos los tiles. "
        "Tiene que valer lo mismo que GeoData__MapTokenSecret en Geocore.",
    )


def comprobar_minio(
    settings: Settings,
    *,
    crear_cliente: Callable[[], object] | None = None,
) -> Comprobacion:
    """Sondea MinIO con una lectura que ejercita el permiso que importa.

    `crear_cliente` se inyecta para poder testear sin red. En producción queda
    en None y el cliente se arma desde `settings`.
    """
    if not settings.minio_access_key or not settings.minio_secret_key:
        return Comprobacion("minio", ERROR, "Faltan MINIO_ACCESS_KEY / MINIO_SECRET_KEY.")

    try:
        cliente = crear_cliente() if crear_cliente else _cliente_por_defecto(settings)
    except Exception as e:  # noqa: BLE001
        logger.exception("No se pudo construir el cliente de MinIO")
        return Comprobacion("minio", ERROR, f"No se pudo inicializar el cliente: {type(e).__name__}")

    try:
        cliente.stat_object(settings.minio_bucket, KEY_DE_SONDEO)
        # Que exista es raro (la key es de sondeo) pero prueba lo mismo y mejor.
        return Comprobacion("minio", OK, "MinIO respondió y la lectura funciona.")
    except Exception as e:  # noqa: BLE001
        return _interpretar(e)


def _interpretar(e: Exception) -> Comprobacion:
    """Traduce la excepción a una causa accionable.

    Se ramifica por `code` en vez de por el tipo de excepción, para no acoplar
    este módulo a la jerarquía de errores de minio-py. Y no se devuelve el
    mensaje crudo: suele incluir el endpoint, que no queremos publicar.
    """
    codigo = getattr(e, "code", None)
    if codigo in _MOTIVOS:
        estado, detalle = _MOTIVOS[codigo]
        return Comprobacion("minio", estado, detalle)

    if codigo:
        logger.warning("MinIO devolvió un código no contemplado: %s", codigo)
        return Comprobacion("minio", ERROR, f"MinIO respondió con un error inesperado: {codigo}")

    # Sin `code` no hubo respuesta HTTP: no resolvió el nombre, no hubo ruta, o
    # se agotó el timeout. Es el fallo típico del dominio privado mal escrito.
    logger.warning("No hubo respuesta de MinIO: %s", type(e).__name__)
    return Comprobacion(
        "minio",
        ERROR,
        "No hubo respuesta de MinIO. Revisá MINIO_ENDPOINT y MINIO_SECURE: el "
        "dominio privado de Railway lleva puerto (:9000) y va sin TLS; el "
        "público va sin puerto y con TLS. La red privada de Railway es solo "
        "IPv6, así que el destino tiene que escuchar en la dirección '::'.",
    )


def _cliente_por_defecto(settings: Settings):
    """Cliente de MinIO con timeout acotado.

    Import perezoso: mantiene `terra_tiles` importable —y los tests corriendo—
    sin la librería instalada.
    """
    import urllib3
    from minio import Minio

    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        # Sin timeout explícito, minio-py reintenta y el endpoint se cuelga.
        http_client=urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=TIMEOUT_SEGUNDOS, read=TIMEOUT_SEGUNDOS),
            retries=urllib3.Retry(total=0),
        ),
    )


def informe(
    settings: Settings,
    *,
    crear_cliente: Callable[[], object] | None = None,
) -> tuple[dict, int]:
    """Arma el cuerpo de `/health/ready` y su código HTTP.

    Devuelve **503 solo si algo está roto de verdad**. `degradado` sale con 200
    a propósito: significa "no se pudo concluir", y un chequeo que grita fallo
    ante la duda se vuelve ruido que la gente aprende a ignorar.
    """
    comprobaciones = [
        comprobar_token_secret(settings),
        comprobar_minio(settings, crear_cliente=crear_cliente),
    ]

    if any(c.estado == ERROR for c in comprobaciones):
        estado_global = ERROR
    elif any(c.estado == DEGRADADO for c in comprobaciones):
        estado_global = DEGRADADO
    else:
        estado_global = OK

    cuerpo = {
        "status": estado_global,
        "service": "tileserver-titiler",
        "bucket": settings.minio_bucket,  # no es secreto: va en cada URL de tile
        "checks": [
            {"name": c.nombre, "status": c.estado, "detail": c.detalle}
            for c in comprobaciones
        ],
    }
    return cuerpo, (503 if estado_global == ERROR else 200)
