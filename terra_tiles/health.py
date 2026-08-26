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
    # Sub-código estable para ramificar sin parsear el texto en español.
    causa: str | None = None

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


def comprobar_endpoint(settings: Settings) -> Comprobacion:
    """Revisa la FORMA de MINIO_ENDPOINT, sin tocar la red.

    Los dominios de Railway tienen dos formas incompatibles y confundirlas es
    el error más repetido al configurar el servicio. Se detecta acá porque el
    sondeo de red solo puede decir "no hubo respuesta", que es cierto pero no
    dice cuál de las dos formas está mal.

    Sale como `degradado` y no como `error`: son heurísticas sobre el hostname,
    y no queremos que un 503 dependa de adivinar. Si además la red falla, el
    sondeo lo reporta como error por su cuenta y los dos mensajes se suman.
    """
    endpoint = settings.minio_endpoint
    host, puerto = _partir_host_y_puerto(endpoint)

    if host in ("localhost", "127.0.0.1", "::1"):
        return Comprobacion(
            "minio_endpoint", DEGRADADO,
            "MINIO_ENDPOINT apunta a esta misma máquina. Fuera de docker-compose "
            "no hay ningún MinIO ahí: dentro del contenedor, localhost es el "
            "propio contenedor.",
            causa="localhost",
        )

    if host.endswith(".railway.internal"):
        if puerto is None:
            return Comprobacion(
                "minio_endpoint", DEGRADADO,
                "El dominio privado de Railway necesita el PUERTO EXPLÍCITO "
                "(normalmente :9000). La red privada no hace mapeo de puertos: "
                "sin puerto se asume el 80 o el 443, donde MinIO no escucha. "
                "Es el dominio público el que va sin puerto, no este.",
                causa="falta_puerto",
            )
        if settings.minio_secure:
            return Comprobacion(
                "minio_endpoint", DEGRADADO,
                "El dominio privado de Railway va en texto plano: poné "
                "MINIO_SECURE=False. TLS es solo para el dominio público.",
                causa="tls_en_privado",
            )

    if host.endswith(".up.railway.app") or host.endswith(".railway.app"):
        if puerto is not None:
            return Comprobacion(
                "minio_endpoint", DEGRADADO,
                "El dominio público de Railway va SIN puerto: el edge escucha en "
                "443 y hace de proxy. El puerto explícito es del dominio privado.",
                causa="puerto_en_publico",
            )
        if not settings.minio_secure:
            return Comprobacion(
                "minio_endpoint", DEGRADADO,
                "El dominio público de Railway es solo TLS: poné MINIO_SECURE=True.",
                causa="sin_tls_en_publico",
            )

    return Comprobacion("minio_endpoint", OK, "Forma coherente con MINIO_SECURE.")


def _partir_host_y_puerto(endpoint: str) -> tuple[str, int | None]:
    """Separa `host:puerto`, tolerando IPv6 entre corchetes (`[::1]:9000`)."""
    resto = endpoint
    if resto.startswith("["):                      # literal IPv6
        cierre = resto.find("]")
        if cierre == -1:
            return resto, None
        host, resto = resto[1:cierre], resto[cierre + 1:]
        if resto.startswith(":") and resto[1:].isdigit():
            return host, int(resto[1:])
        return host, None

    # Más de un ':' sin corchetes es un IPv6 escrito sin la forma que exige la
    # URL. Partir por el último ':' convertiría `fd12::34` en host `fd12:` y
    # puerto 34, que es peor que no adivinar.
    if resto.count(":") == 1:
        host, _, cola = resto.rpartition(":")
        if cola.isdigit():
            return host, int(cola)
    return resto, None


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
        return _interpretar(e, settings.minio_secure)


def _interpretar(e: Exception, secure: bool) -> Comprobacion:
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

    # Sin `code` no hubo respuesta HTTP. Las causas posibles piden acciones
    # distintas, así que agruparlas en un solo mensaje obliga a adivinar.
    causa, detalle = _clasificar_fallo_de_red(e, secure)
    logger.warning("No hubo respuesta de MinIO (%s): %s", causa, type(e).__name__)
    return Comprobacion("minio", ERROR, detalle, causa=causa)


# Señales por causa, buscadas sobre los NOMBRES DE CLASE y los mensajes de la
# cadena de excepciones. Los nombres de clase van primero porque son estables:
# el texto de los errores de socket viene traducido al idioma del sistema
# operativo, así que "connection refused" no aparece en un Windows en español.
_SENALES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dns", ("nameresolutionerror", "gaierror", "name or service not known",
             "getaddrinfo failed", "nodename nor servname",
             "temporary failure in name resolution")),
    # Un error de SSL explícito. Ojo: el caso más común de TLS mal configurado
    # NO llega acá — ver `_corte`.
    ("tls", ("sslerror", "sslcertverificationerror", "wrong_version_number",
             "unexpected_eof", "record layer failure", "handshake")),
    ("timeout", ("connecttimeouterror", "readtimeouterror", "timeouterror", "timed out")),
    # Nada escuchando en ese puerto.
    ("rechazado", ("connectionrefusederror", "connection refused")),
    # Algo escuchando que cortó la conexión: es distinto de que no haya nadie.
    ("corte", ("connectionreseterror", "protocolerror", "connection aborted",
               "brokenpipeerror", "incompleteread")),
    ("inalcanzable", ("no route to host", "network is unreachable", "ehostunreach")),
)

_DETALLES_DE_RED: dict[str, str] = {
    "dns": (
        "El hostname de MINIO_ENDPOINT no resolvió: no hay ningún servicio con "
        "ese nombre en la red privada. El dominio privado se arma con el NOMBRE "
        "DEL SERVICIO en Railway (nombre-del-servicio.railway.internal), que no "
        "tiene por qué ser 'minio'. Verificá también que los dos servicios estén "
        "en el MISMO proyecto: la red privada no cruza proyectos."
    ),
    "tls": (
        "Falló el handshake TLS. Regla: el dominio privado "
        "(.railway.internal:9000) va sin TLS -> MINIO_SECURE=False; el público "
        "(.up.railway.app, sin puerto) va con TLS -> MINIO_SECURE=True."
    ),
    "rechazado": (
        "El hostname resolvió pero no hay nada escuchando en ese puerto. Dos "
        "causas: el puerto equivocado (la API S3 de MinIO es la 9000; la 9001 es "
        "la consola), o que MinIO escuche solo en IPv4. La red privada de Railway "
        "es solo IPv6, así que el destino tiene que escuchar en '::', no en "
        "'0.0.0.0'."
    ),
    "timeout": (
        "Conectó pero MinIO no respondió a tiempo. Puede estar arrancando o "
        "caído, o el puerto puede atender otro protocolo."
    ),
    "inalcanzable": (
        "No hay ruta hacia ese host. Suele ser la red privada de Railway: los "
        "dos servicios tienen que estar en el mismo proyecto, y el destino tiene "
        "que escuchar en IPv6 ('::')."
    ),
    "desconocido": (
        "No hubo respuesta de MinIO y la causa no se pudo clasificar. Revisá "
        "MINIO_ENDPOINT y MINIO_SECURE, y los logs del servicio de MinIO."
    ),
}

# El corte de conexión se interpreta según hayamos pedido TLS o no, porque la
# excepción sola no alcanza para distinguirlo: hablarle TLS a un puerto de texto
# plano no produce ningún error de SSL, produce un ConnectionReset envuelto en
# ProtocolError. Verificado contra urllib3, no supuesto.
_CORTE_CON_TLS = (
    "Se pidió TLS y el servidor cortó la conexión sin completar el handshake. "
    "Casi siempre es MINIO_SECURE=True contra un endpoint de texto plano: el "
    "dominio privado de Railway (.railway.internal:9000) no lleva TLS. Probá "
    "con MINIO_SECURE=False."
)
_CORTE_SIN_TLS = (
    "Algo respondió en ese puerto pero cortó la conexión. Si el endpoint es el "
    "dominio público de Railway, habla TLS y hay que poner MINIO_SECURE=True. "
    "Si es el privado, revisá que el puerto sea el de la API S3 (9000) y no el "
    "de la consola (9001)."
)


def _clasificar_fallo_de_red(e: Exception, secure: bool) -> tuple[str, str]:
    """Distingue DNS, TLS, puerto cerrado, corte de conexión y timeout.

    Cada una manda a revisar algo distinto —el nombre del servicio, el valor de
    `MINIO_SECURE`, el puerto, o el estado de MinIO—, así que un único mensaje
    genérico dejaría al que diagnostica probándolas todas a ciegas.

    Se recorre la cadena de excepciones porque minio-py envuelve todo en un
    `MaxRetryError` cuyo `.reason` es la causa real. **El texto de la cadena se
    usa solo para clasificar y nunca se devuelve**: incluye el endpoint.
    """
    texto = " ".join(f"{type(x).__name__} {x}" for x in _cadena(e)).lower()
    for causa, senales in _SENALES:
        if any(s in texto for s in senales):
            if causa == "corte":
                return "tls", (_CORTE_CON_TLS if secure else _CORTE_SIN_TLS)
            return causa, _DETALLES_DE_RED[causa]
    return "desconocido", _DETALLES_DE_RED["desconocido"]


def _cadena(e: BaseException, limite: int = 10) -> list[BaseException]:
    """Excepción y sus causas encadenadas, sin ciclos ni recursión infinita."""
    vistas: list[BaseException] = []
    actual: BaseException | None = e
    while actual is not None and len(vistas) < limite and actual not in vistas:
        vistas.append(actual)
        # `.reason` es donde urllib3 guarda la causa real dentro de MaxRetryError.
        siguiente = getattr(actual, "reason", None)
        if not isinstance(siguiente, BaseException):
            siguiente = actual.__cause__ or actual.__context__
        actual = siguiente
    return vistas


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
        # Antes del sondeo: si la forma del endpoint está mal, el fallo de red
        # es una consecuencia y conviene leer primero la causa.
        comprobar_endpoint(settings),
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
            # `cause` solo aparece cuando hay un sub-código que aporte algo, para
            # no llenar la respuesta de nulls cuando todo está bien.
            {"name": c.nombre, "status": c.estado, "detail": c.detalle}
            | ({"cause": c.causa} if c.causa else {})
            for c in comprobaciones
        ],
    }
    return cuerpo, (503 if estado_global == ERROR else 200)
