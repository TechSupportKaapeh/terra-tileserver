"""Controles de acceso del tileserver.

Dos controles, y las dos capas hacen falta:

- `crear_validador_de_token` — *quién* puede pedir un tile.   OWASP A01
- `crear_validador_de_ruta`  — *qué* puede pedir.             OWASP A01 + A10

**Desde M.8.1 el segundo depende del primero.** El token dice de qué tenant es
quien pide, y la ruta tiene que caer bajo `tenants/{ese tenant}/`. Antes eran
independientes, y ahí estaba el agujero: el token autorizaba "pedir tiles" sin
decir cuáles, y la key del COG viaja a la vista en la URL del tile, así que
cualquier usuario con su token legítimo y la key de otro tenant veía los rásters
de ese otro tenant.

Ambas son fábricas: reciben la configuración y devuelven la dependencia. Eso
las hace inyectables y testeables sin variables globales ni recargar módulos
(inversión de dependencias: quien las usa no sabe de dónde salió la config).

Este módulo no importa TiTiler ni rasterio, así que los tests corren sin la
pila geoespacial instalada.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Query

logger = logging.getLogger("terra_tiles.security")

# Códigos que el cliente puede ramificar. `MAP_TOKEN_UNAVAILABLE` es el mismo
# que devuelve Geocore ante la misma causa (DECISIONS #16), a propósito: los
# dos lados del contrato fallan igual y el diagnóstico es uno solo.
CODIGO_SIN_SECRETO = "MAP_TOKEN_UNAVAILABLE"
CODIGO_URL_NO_PERMITIDA = "URL_NO_PERMITIDA"
# El COG existe y la URL es del bucket, pero es de otro tenant. Se distingue de
# `URL_NO_PERMITIDA` a propósito: aquello es una ruta que nadie puede pedir
# (400, SSRF); esto es una ruta que *este* no puede pedir (403, autorización).
CODIGO_TENANT_AJENO = "TENANT_AJENO"
# El token valida pero no dice de qué tenant es: uno emitido antes de M.8.1.
CODIGO_TOKEN_SIN_TENANT = "TOKEN_SIN_TENANT"

# El claim que Geocore firma (`MapsController.TenantClaim`).
CLAIM_TENANT = "tenant_id"

# Los uuid canónicos —minúsculas y con guiones— que el worker escribe en la key
# (`pipeline/claves.py`) y que Geocore firma con `Guid.ToString()`. La
# comparación de prefijo es textual, así que las dos puntas tienen que coincidir
# carácter por carácter.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def crear_validador_de_token(secreto: str | None, leeway: int = 30) -> Callable[..., str]:
    """Devuelve la dependencia que valida el token de mapa que firma Geocore.

    **Falla cerrado.** Si `secreto` es None, todo pedido a `/cog/*` recibe 503
    y no se sirve nada. Antes había un secreto por defecto en el código, con lo
    cual un deploy sin configurar fallaba *abierto*: cualquiera que leyera el
    repo podía forjar un token válido. Un servicio que no puede autenticar debe
    negarse a responder, no responder que sí.

    **Devuelve el tenant del token** (M.8.1), que es lo que `dataset_path` usa
    para decidir qué COG puede abrirse. Devolverlo en vez de dejarlo adentro es
    lo que hace imposible que los dos controles se desincronicen: no hay dos
    lugares donde se lea el claim.
    """
    if not secreto:
        logger.error(
            "MAP_TOKEN_SECRET no está configurada: /cog/* devolverá 503. "
            "Tiene que valer lo mismo que GeoData__MapTokenSecret en Geocore."
        )

    def verify_map_token(token: str = Query(None, description="JWT emitido por Geocore")) -> str:
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

        # De acá para abajo, M.8.1. Un token sin tenant es uno emitido antes de
        # este cambio: se rechaza en vez de servir todo el bucket, que es lo que
        # hacía antes. El orden de despliegue —Geocore antes que este servicio—
        # es lo que hace que esa ventana dure lo que dura un token: una hora.
        tenant = payload.get(CLAIM_TENANT)
        if not isinstance(tenant, str) or not tenant:
            raise HTTPException(
                status_code=403,
                detail={"code": CODIGO_TOKEN_SIN_TENANT,
                        "message": "El token de mapa no dice de qué tenant es. Pedí uno nuevo."},
            )
        # El claim viene firmado, así que esto no defiende de un atacante: defiende
        # de un cambio del otro lado. Un tenant que no tiene la forma de la key no
        # puede coincidir con ninguna, y fallar diciéndolo es mejor que 403 en todo
        # sin explicación.
        if not _UUID.match(tenant):
            raise HTTPException(
                status_code=403,
                detail={"code": CODIGO_TOKEN_SIN_TENANT,
                        "message": "El tenant del token no tiene la forma de un uuid canónico."},
            )
        return tenant

    return verify_map_token


def prefijo_del_tenant(prefijo_del_bucket: str, tenant: str) -> str:
    """`s3://{bucket}/tenants/{tenant}/`, con la barra final.

    La barra no es decorativa: sin ella, el prefijo de un tenant sería también el
    comienzo de cualquier otro id que empezara igual. Con uuid de largo fijo no
    puede pasar, pero la comparación no tiene por qué depender de ese detalle.

    Es la misma forma que arma `prefijo_de_tenant()` en `pipeline/claves.py` del
    worker, del otro lado del contrato.
    """
    return f"{prefijo_del_bucket}tenants/{tenant}/"


def crear_validador_de_ruta(
    prefijo_valido: str,
    tenant_del_token: Callable[..., str],
) -> Callable[..., str]:
    """Devuelve el `path_dependency` de TiTiler: qué COG puede abrirse.

    Dos controles, en este orden, porque responden preguntas distintas:

    1. **¿Es una ruta que alguien pueda pedir?** (OWASP A10, SSRF). TiTiler abre
       con GDAL lo que le pasen en `?url=`. Sin acotarlo, cualquiera con un token
       válido puede usar este servicio como proxy de lectura: un `https://…` sale
       por /vsicurl/ y alcanza hosts internos de la red privada. El servicio corre
       con credenciales que el atacante no tiene. Se responde **400**: no es una
       cuestión de permisos, esa URL no la puede pedir nadie.
    2. **¿Es una ruta que pueda pedir *éste*?** (OWASP A01, M.8.1). El COG tiene
       que colgar de `tenants/{tenant del token}/`. Se responde **403**: la URL es
       legítima, el que pide no. La key del COG viaja a la vista en la URL del
       tile, así que sin esto un usuario con su token y la key de otro tenant veía
       los rásters de ese otro tenant.

    El tenant llega por `Depends` del mismo validador de token que ya protege el
    router, y no leyendo el claim otra vez: FastAPI lo resuelve una sola vez por
    pedido, y **no hay dos lugares que puedan discrepar** sobre de quién es el
    token. Como efecto, un token inválido da 401 antes de llegar a esta función.

    Lista blanca, no lista negra, en las dos: se acepta un único prefijo conocido
    y todo lo demás se rechaza. Enumerar lo peligroso siempre deja huecos.
    """

    def dataset_path(
        url: str = Query(..., description="Ruta del COG dentro del bucket"),
        tenant: str = Depends(tenant_del_token),
    ) -> str:
        if not url.startswith(prefijo_valido):
            raise HTTPException(
                status_code=400,
                detail={"code": CODIGO_URL_NO_PERMITIDA,
                        "message": f"Solo se sirven COG bajo {prefijo_valido}"},
            )
        # `..` no escapa del bucket en S3 —las keys son texto plano— pero GDAL
        # normaliza algunas rutas y no vale la pena depender de ese detalle. Va
        # **antes** de la comparación por tenant, que sí podría burlarse con un
        # `tenants/{mio}/../{ajeno}/`.
        if ".." in url:
            raise HTTPException(
                status_code=400,
                detail={"code": CODIGO_URL_NO_PERMITIDA,
                        "message": "La ruta no puede contener '..'"},
            )
        if not url.startswith(prefijo_del_tenant(prefijo_valido, tenant)):
            # El mensaje no repite el tenant del token ni el de la key: quien
            # pregunta ya sabe el suyo, y el ajeno no se lo decimos.
            raise HTTPException(
                status_code=403,
                detail={"code": CODIGO_TENANT_AJENO,
                        "message": "Ese COG no es de tu tenant."},
            )
        return url

    return dataset_path
