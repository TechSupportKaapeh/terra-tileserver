"""Lo que el tileserver escribe al arrancar: quién es, con qué config, y si conecta.

POR QUÉ EXISTE
--------------
Es el tercero de la familia, después de `utils_pkg/arranque.py` en el worker y
`StartupBanner.cs` en Geocore, y por el mismo motivo: **los deploys de esta
plataforma se caen por configuración, y el síntoma llega tarde y disfrazado.**

Acá el disfraz es particularmente malo. Con la configuración mal, el tileserver
arranca perfecto y responde `/health` sin quejarse; lo único que pasa es que
los tiles salen 401, 403 o 503, uno por uno, en el navegador de otra persona.
El servicio nunca se entera de que está roto.

QUÉ APORTA SOBRE `/health/ready`
--------------------------------
Las comprobaciones **son las mismas**: se reusan las de `health.py` en vez de
reimplementarlas, porque dos criterios para lo mismo divergen (es el bug que
dejó al worker sin verificación de firma con `ENVIRONMENT=prod`).

Lo que cambia es **el público, y por lo tanto qué se puede mostrar**.
`/health/ready` es un endpoint sin token: su `detalle` omite el endpoint de
MinIO a propósito, porque el hostname de la red privada es justo lo que alguien
querría enumerar. El log del contenedor no es público, así que acá **sí** se
imprime el endpoint — y eso es media batalla, porque tres de los cuatro errores
recurrentes de este despliegue son sobre la forma del endpoint:

    bucket.railway.internal        falta el :9000
    bucket.railway.internal:9000   con MINIO_SECURE=True -> WRONG_VERSION_NUMBER
    bucket.up.railway.app:9000     sobra el puerto -> ConnectionReset
    bucket.up.railway.app          con MINIO_SECURE=False

Ninguno de los cuatro se ve leyendo la variable de a una. Se ven cuando el
endpoint y el modo están impresos en la misma línea.

QUÉ NO IMPRIME
--------------
Ningún carácter de ningún secreto: de `MAP_TOKEN_SECRET` y de la secret key sale
sólo el largo. De la access key sale la máscara que ya usa `health.py`.
"""

import logging

from terra_tiles.health import (
    DEGRADADO,
    ERROR,
    comprobar_credenciales,
    comprobar_endpoint,
    comprobar_minio,
    comprobar_token_secret,
)
from terra_tiles.settings import Settings

logger = logging.getLogger("terra_tiles.arranque")

ANCHO = 74

ARTE = (
    '____________________________________ _        _______  _______',
    '\\__   __/\\__   __/\\__   __/\\__   __/( \\      (  ____ \\(  ____ )',
    '   ) (      ) (      ) (      ) (   | (      | (    \\/| (    )|',
    '   | |      | |      | |      | |   | |      | (__    | (____)|',
    '   | |      | |      | |      | |   | |      |  __)   |     __)',
    '   | |      | |      | |      | |   | |      | (      | (\\ (',
    '   | |   ___) (___   | |   ___) (___| (____/\\| (____/\\| ) \\ \\__',
    '   )_(   \\_______/   )_(   \\_______/(_______/(_______/|/   \\__/',
    '',
    ' ______              _______  _______           _        _______  _',
    '(  ___ \\ |\\     /|  (       )(  ___  )|\\     /|| \\    /\\(  ___  )( \\',
    '| (   ) )( \\   / )_ | () () || (   ) |( \\   / )|  \\  / /| (   ) || (',
    '| (__/ /  \\ (_) /(_)| || || || (___) | \\ (_) / |  (_/ / | |   | || |',
    '|  __ (    \\   /    | |(_)| ||  ___  |  \\   /  |   _ (  | |   | || |',
    '| (  \\ \\    ) (   _ | |   | || (   ) |   ) (   |  ( \\ \\ | |   | || |',
    '| )___) )   | |  (_)| )   ( || )   ( |   | |   |  /  \\ \\| (___) || (____/\\',
    '|/ \\___/    \\_/     |/     \\||/     \\|   \\_/   |_/    \\/(_______)(_______/',
)


def describir_secreto(valor: str | None, minimo: int = 0) -> str:
    """Describe un secreto sin mostrar ni un carácter de su contenido."""
    if not valor:
        return "AUSENTE"
    texto = f"definida ({len(valor)} chars)"
    if minimo and len(valor) < minimo:
        texto += f"  DEMASIADO CORTO (minimo {minimo})"
    if valor != valor.strip():
        # Nadie recorta las variables de Railway. El mismo secreto anda en un
        # `.env` local —que python-dotenv sí recorta— y falla desplegado.
        texto += "  OJO: ESPACIOS EN LOS BORDES"
    return texto


def reporte_de_configuracion(settings: Settings, entorno: dict) -> list[str]:
    """Arma las líneas de configuración. **Función pura.**"""
    lineas = [
        "",
        "=" * ANCHO,
        "CONFIGURACION",
        "=" * ANCHO,
        "",
        "MINIO",
        # El endpoint y el modo, juntos y en ese orden: la incoherencia entre
        # los dos es el error mas repetido de este despliegue y solo se ve
        # mirandolos al lado.
        f"  MINIO_ENDPOINT        {settings.minio_endpoint}",
        f"  MINIO_SECURE          {settings.minio_secure}"
        f"   -> {'https' if settings.minio_secure else 'http'}",
        f"  MINIO_BUCKET          {settings.minio_bucket}",
        f"  MINIO_ACCESS_KEY      {settings.minio_access_key}",
        f"  MINIO_SECRET_KEY      {describir_secreto(settings.minio_secret_key)}",
        f"  AWS_REGION            {settings.aws_region}",
        "",
        "TOKEN DE MAPA",
        f"  MAP_TOKEN_SECRET      {describir_secreto(settings.map_token_secret, 32)}",
        "        (tiene que valer lo mismo que GeoData__MapTokenSecret en Geocore)",
        "",
        "HTTP",
        f"  CORS_ALLOW_ORIGINS    {', '.join(settings.cors_origins)}",
        f"  TILE_CACHE_SECONDS    {settings.tile_cache_seconds}",
        f"  LOG_LEVEL             {entorno.get('LOG_LEVEL') or 'INFO (por defecto)'}",
        f"  PORT                  {entorno.get('PORT') or 'sin definir'}",
    ]
    if settings.cors_origins == ["*"]:
        lineas.append(
            "        CORS abierto a cualquier origen. Aceptable aca: no hay cookies"
        )
        lineas.append(
            "        ni credenciales de sesion, asi que CORS no autoriza nada que el"
        )
        lineas.append(
            "        token de mapa no controle ya."
        )
    elif not settings.cors_origins:
        lineas.append(
            "        VACIO no es lo mismo que ausente: son CERO origenes permitidos"
        )
        lineas.append(
            "        y el navegador bloquea todo. Borra la variable para volver a '*'."
        )
    return lineas


def reporte_de_conexiones(comprobaciones) -> list[str]:
    """Formatea las comprobaciones de `health.py`. **Función pura.**"""
    lineas = ["", "=" * ANCHO, "CONEXIONES", "=" * ANCHO]
    for c in comprobaciones:
        lineas.append(f"  {c.estado.upper():<10} {c.nombre:<16} {c.detalle}")

    rotas = [c for c in comprobaciones if c.estado == ERROR]
    dudosas = [c for c in comprobaciones if c.estado == DEGRADADO]
    lineas.append("=" * ANCHO)
    if rotas:
        lineas.append("NO FUNCIONAN: " + ", ".join(c.nombre for c in rotas))
        lineas.append(
            "El servicio igual queda arriba: /cog/* va a responder 401/403/503 por"
        )
        lineas.append("tile, en el navegador de otra persona y sin avisar aca.")
    elif dudosas:
        # `degradado` significa "no se pudo concluir", y mezclarlo con un fallo
        # convierte el reporte en ruido que la gente aprende a ignorar. Es el
        # mismo criterio con el que `informe()` devuelve 200 y no 503.
        lineas.append("SIN CONCLUIR: " + ", ".join(c.nombre for c in dudosas))
    else:
        lineas.append("Todo verificado responde.")
    lineas.append("=" * ANCHO)
    return lineas


def registrar_arranque(settings: Settings, entorno: dict, *,
                       crear_cliente=None, log=None) -> list:
    """Escribe el banner completo. **Nunca levanta.**

    Es la regla de los tres servicios, y en este repo importa igual: un
    diagnóstico que tumba el proceso al diagnosticar es peor que no tenerlo.
    Devuelve las comprobaciones por si alguien las quiere inspeccionar.
    """
    log = log or logger
    for linea in ARTE:
        log.info("%s", linea)

    try:
        for linea in reporte_de_configuracion(settings, entorno):
            log.info("%s", linea)
    except Exception as e:  # noqa: BLE001 - el reporte no puede tumbar el arranque
        log.error("No se pudo armar el reporte de configuracion: %s", e)

    try:
        comprobaciones = [
            comprobar_token_secret(settings),
            comprobar_endpoint(settings),
            comprobar_credenciales(settings, entorno),
            comprobar_minio(settings, crear_cliente=crear_cliente),
        ]
    except Exception as e:  # noqa: BLE001
        log.error("No se pudieron correr las comprobaciones de arranque: %s", e)
        return []

    for linea in reporte_de_conexiones(comprobaciones):
        log.info("%s", linea)

    # Y aparte como ERROR, para que sobreviva a cualquier LOG_LEVEL y para que
    # Railway lo destaque. Mismo criterio que el worker y que Geocore.
    for c in comprobaciones:
        if c.estado == ERROR:
            log.error("Configuracion %s: %s", c.nombre, c.detalle)

    return comprobaciones
