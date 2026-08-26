"""Controles de acceso del tileserver.

Dos controles independientes, y las dos capas hacen falta:

- `crear_validador_de_token` — *quién* puede pedir un tile.   OWASP A01
- `crear_validador_de_ruta`  — *qué* puede pedir.             OWASP A10

Ambas son fábricas: reciben la configuración y devuelven la dependencia. Eso
las hace inyectables y testeables sin variables globales ni recargar módulos
(inversión de dependencias: quien las usa no sabe de dónde salió la config).

Este módulo no importa TiTiler ni rasterio, así que los tests corren sin la
pila geoespacial instalada.
"""
from __future__ import annotations

import logging
from typing import Callable

import jwt
from fastapi import HTTPException, Query

logger = logging.getLogger("terra_tiles.security")

# Códigos que el cliente puede ramificar. `MAP_TOKEN_UNAVAILABLE` es el mismo
# que devuelve Geocore ante la misma causa (DECISIONS #16), a propósito: los
# dos lados del contrato fallan igual y el diagnóstico es uno solo.
CODIGO_SIN_SECRETO = "MAP_TOKEN_UNAVAILABLE"
CODIGO_URL_NO_PERMITIDA = "URL_NO_PERMITIDA"


def crear_validador_de_token(secreto: str | None, leeway: int = 30) -> Callable[..., None]:
    """Devuelve la dependencia que valida el token de mapa que firma Geocore.

    **Falla cerrado.** Si `secreto` es None, todo pedido a `/cog/*` recibe 503
    y no se sirve nada. Antes había un secreto por defecto en el código, con lo
    cual un deploy sin configurar fallaba *abierto*: cualquiera que leyera el
    repo podía forjar un token válido. Un servicio que no puede autenticar debe
    negarse a responder, no responder que sí.
    """
    if not secreto:
        logger.error(
            "MAP_TOKEN_SECRET no está configurada: /cog/* devolverá 503. "
            "Tiene que valer lo mismo que GeoData__MapTokenSecret en Geocore."
        )

    def verify_map_token(token: str = Query(None, description="JWT emitido por Geocore")) -> None:
        if not secreto:
            raise HTTPException(
                status_code=503,
                detail={"code": CODIGO_SIN_SECRETO,
                        "message": "El servidor de tiles no tiene configurado el secreto del token."},
            )
        if not token:
            raise HTTPException(
                status_code=401,
                detail="Falta el token de mapa. Agregá '?token=...' a la URL.",
            )
        try:
            # `audience` e `issuer` se validan explícitamente: sin eso, un token
            # legítimo emitido para otro servicio con el mismo secreto serviría acá.
            # `leeway` cubre el desfase de reloj entre Geocore y este servicio;
            # sin margen aparecen 401 intermitentes imposibles de reproducir.
            payload = jwt.decode(
                token,
                secreto,
                algorithms=["HS256"],   # lista fija: nunca leer `alg` del propio token
                audience="TiTiler",
                issuer="Geocore",
                leeway=leeway,
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="El token de mapa expiró")
        except jwt.InvalidTokenError as e:
            raise HTTPException(status_code=401, detail=f"Token de mapa inválido: {e}")

        if payload.get("type") != "map-access":
            raise HTTPException(status_code=403, detail="Tipo de token inválido")

    return verify_map_token


def crear_validador_de_ruta(prefijo_valido: str) -> Callable[..., str]:
    """Devuelve el `path_dependency` de TiTiler, acotado a un único prefijo.

    TiTiler abre con GDAL lo que le pasen en `?url=`. Sin acotarlo, cualquiera
    con un token válido puede usar este servicio como proxy de lectura: un
    `https://…` sale por /vsicurl/ y alcanza hosts internos de la red privada.
    Eso es SSRF (OWASP A10), y el servicio corre con credenciales que el
    atacante no tiene.

    Es la segunda capa: la policy de `tiler-ro` ya acota el bucket del lado de
    MinIO. Esta cierra los demás esquemas y falla con un error explícito en vez
    de un timeout.
    """

    def dataset_path(url: str = Query(..., description="Ruta del COG dentro del bucket")) -> str:
        # Lista blanca, no lista negra: se acepta un único prefijo conocido y
        # todo lo demás se rechaza. Enumerar lo peligroso siempre deja huecos.
        if not url.startswith(prefijo_valido):
            raise HTTPException(
                status_code=400,
                detail={"code": CODIGO_URL_NO_PERMITIDA,
                        "message": f"Solo se sirven COG bajo {prefijo_valido}"},
            )
        # `..` no escapa del bucket en S3 —las keys son texto plano— pero GDAL
        # normaliza algunas rutas y no vale la pena depender de ese detalle.
        if ".." in url:
            raise HTTPException(
                status_code=400,
                detail={"code": CODIGO_URL_NO_PERMITIDA,
                        "message": "La ruta no puede contener '..'"},
            )
        return url

    return dataset_path
