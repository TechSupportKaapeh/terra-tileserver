"""El aislamiento por tenant contra la app de verdad (M.8.1).

`test_security.py` prueba las dos fábricas como funciones. Acá se prueba **lo
que ninguna de las dos puede probar sola**: que `main.py` las conecte bien y que
FastAPI resuelva la dependencia anidada —el `path_dependency` que depende del
validador de token— en un pedido HTTP real, con TiTiler montado.

Es la parte que más fácil se rompe en silencio. Si alguien le pasara a
`crear_validador_de_ruta` *otra* instancia del validador de token en vez de la
misma, todo seguiría funcionando y el claim se leería dos veces; si el
`path_dependency` no se aplicara a algún router, ese router serviría cualquier
COG del bucket. Los dos casos pasan los tests unitarios.

**El entorno se fija antes de importar `main`**: `load_dotenv()` no pisa lo que
ya está en el entorno, así que esto gana sobre un `.env` de la máquina y el test
no depende de cómo esté configurada. El endpoint de MinIO apunta a un puerto
cerrado a propósito: acá no se lee ningún COG, y si alguna vez se intentara,
tiene que fallar en el acto y no colgarse esperando a la red.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRETO = "un-secreto-de-produccion-de-48-caracteres-abcdefgh"
BUCKET = "terra-assets"
TENANT = "a1b2c3d4-1111-4111-8111-111111111111"
OTRO_TENANT = "b2c3d4e5-2222-4222-8222-222222222222"

os.environ.update({
    "MAP_TOKEN_SECRET": SECRETO,
    "MINIO_BUCKET": BUCKET,
    "MINIO_ENDPOINT": "127.0.0.1:1",
    "MINIO_ACCESS_KEY": "no-se-usa",
    "MINIO_SECRET_KEY": "no-se-usa",
    # Un solo test llega hasta GDAL, y tiene que rebotar rápido: sin esto son
    # varios reintentos de dos segundos cada uno contra un puerto cerrado.
    "GDAL_HTTP_CONNECTTIMEOUT": "1",
    "GDAL_HTTP_TIMEOUT": "2",
    "GDAL_HTTP_MAX_RETRY": "0",
})

import main  # noqa: E402  — después de fijar el entorno, a propósito


def _token(tenant: str | None = TENANT, *, tipo: str = "map-access") -> str:
    ahora = dt.datetime.now(dt.timezone.utc)
    carga = {"aud": "TiTiler", "iss": "Geocore", "type": tipo,
             "iat": ahora, "exp": ahora + dt.timedelta(hours=1)}
    if tenant is not None:
        carga["tenant_id"] = tenant
    return jwt.encode(carga, SECRETO, algorithm="HS256")


def _key(tenant: str) -> str:
    return (f"s3://{BUCKET}/tenants/{tenant}/ranchos/"
            "3f1c9a7e-0000-4000-8000-000000000001/s2-mensual-v1/ndvi/2026-08.tif")


@pytest.fixture(scope="module")
def cliente() -> TestClient:
    # `raise_server_exceptions=False`: el único test que llega a abrir el COG
    # necesita **la respuesta**, no la excepción de rasterio. Con el default, un
    # error del servidor se relanza y no se puede mirar su status.
    return TestClient(main.app, raise_server_exceptions=False)


# El único router montado. Hasta el 2026-09-24 eran dos: `/mosaic` se borró con
# el hallazgo T-3 adentro (`DECISIONS #64` del worker), y `test_el_router_mosaic_
# ya_no_existe` es el control de que se fue de verdad.
#
# Sigue siendo una lista y un `parametrize` de un elemento a propósito: el día
# que se monte otro router, el que lo monte tiene que sumarlo acá y heredar los
# cuatro controles de una línea. Con los tests escritos contra `/cog` a mano,
# nacería sin ninguno.
RUTAS = ["/cog/info"]


@pytest.mark.parametrize("ruta", RUTAS)
def test_sin_token_da_401(cliente, ruta):
    r = cliente.get(ruta, params={"url": _key(TENANT)})
    assert r.status_code == 401


@pytest.mark.parametrize("ruta", RUTAS)
def test_el_cog_de_otro_tenant_da_403(cliente, ruta):
    """El criterio de aceptación de M.8.1, de punta a punta.

    Token legítimo, vigente y bien firmado; key de otro tenant. Antes esto
    devolvía el ráster.
    """
    r = cliente.get(ruta, params={"url": _key(OTRO_TENANT), "token": _token()})

    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "TENANT_AJENO"
    # Ni el tenant ajeno ni la key vuelven en el cuerpo.
    assert OTRO_TENANT not in r.text


@pytest.mark.parametrize("ruta", RUTAS)
def test_un_token_de_antes_de_M81_no_abre_ni_lo_suyo(cliente, ruta):
    r = cliente.get(ruta, params={"url": _key(TENANT), "token": _token(tenant=None)})

    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "TOKEN_SIN_TENANT"


@pytest.mark.parametrize("ruta", RUTAS)
def test_una_capa_vieja_sin_tenant_en_la_key_ya_no_se_sirve(cliente, ruta):
    """Las de antes del pipeline mensual (Geocore `DECISIONS #43`)."""
    r = cliente.get(ruta, params={"url": f"s3://{BUCKET}/parcelas/abc/2026-08-19_ndvi.tif",
                                  "token": _token()})

    assert r.status_code == 403


@pytest.mark.parametrize("ruta", RUTAS)
def test_el_ssrf_sigue_dando_400_y_no_403(cliente, ruta):
    """Que la capa nueva no tape la vieja: son dos preguntas distintas."""
    r = cliente.get(ruta, params={"url": "http://minio.railway.internal:9000/x.tif",
                                  "token": _token()})

    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "URL_NO_PERMITIDA"


def test_el_cog_propio_pasa_los_controles_y_recien_ahi_falla_al_abrirlo(cliente):
    """El control negativo del control: que no esté rechazando todo.

    Con la key de su propio tenant, el pedido **atraviesa** las dos compuertas y
    lo que falla es la lectura del COG, que no existe y cuyo MinIO es un puerto
    cerrado. Sin esta prueba, un validador que devolviera 403 siempre dejaría
    verdes todos los tests de arriba.
    """
    r = cliente.get("/cog/info", params={"url": _key(TENANT), "token": _token()})

    assert r.status_code not in (400, 401, 403), r.text


def test_el_router_mosaic_ya_no_existe(cliente):
    """El control del borrado (`DECISIONS #64` del worker).

    Sacar `/mosaic` de `RUTAS` deja verdes los tests de arriba **aunque el router
    siga montado**: lo único que pasaría es que nadie lo mira. Este test falla si
    vuelve, y con él volvería el hallazgo T-3 —el tenant se compara contra la URL
    del MosaicJSON y no contra los assets que lista adentro—.

    Se pide **con token**: un 401 también sería "no pasa", y probaría otra cosa.
    Lo que tiene que decir es que la ruta no existe.
    """
    r = cliente.get("/mosaic/info", params={"url": _key(TENANT), "token": _token()})

    assert r.status_code == 404


def test_health_sigue_sin_pedir_token(cliente):
    assert cliente.get("/health").status_code == 200
