"""Tests de las cabeceras de caché.

Se monta una app mínima con rutas de mentira en vez de la real: el middleware
no sabe nada de TiTiler, y probarlo contra la app entera obligaría a tener GDAL
instalado para verificar una cabecera HTTP.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from terra_tiles.caching import CacheControlMiddleware

MAX_AGE = 604_800


@pytest.fixture
def cliente() -> TestClient:
    app = FastAPI()
    app.add_middleware(CacheControlMiddleware, prefijo="/cog/tiles", max_age=MAX_AGE)

    @app.get("/cog/tiles/WebMercatorQuad/1/2/3")
    def tile():
        return Response(content=b"png", media_type="image/png")

    @app.get("/cog/tiles/falla")
    def tile_que_falla():
        return Response(status_code=500, content=b"boom")

    @app.get("/cog/tiles/no-encontrado")
    def tile_no_encontrado():
        return Response(status_code=404, content=b"nope")

    @app.get("/cog/info")
    def info():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


def test_un_tile_exitoso_se_marca_como_inmutable(cliente):
    """El COG de una fecha nunca cambia: el navegador puede quedárselo."""
    r = cliente.get("/cog/tiles/WebMercatorQuad/1/2/3")
    assert r.status_code == 200
    assert r.headers["cache-control"] == f"public, max-age={MAX_AGE}, immutable"


@pytest.mark.parametrize("ruta", ["/cog/tiles/falla", "/cog/tiles/no-encontrado"])
def test_los_errores_no_se_cachean(cliente, ruta):
    """Cachear un 5xx congelaría un fallo transitorio por un año.

    Es el caso que más duele: una caída momentánea de MinIO quedaría grabada
    en el navegador del usuario mucho después de que el problema se resolvió.
    """
    r = cliente.get(ruta)
    assert r.status_code in (404, 500)
    assert "cache-control" not in r.headers


@pytest.mark.parametrize("ruta", ["/cog/info", "/health"])
def test_fuera_del_prefijo_no_se_toca(cliente, ruta):
    """`/health` tiene que reflejar el estado actual, no uno cacheado."""
    r = cliente.get(ruta)
    assert r.status_code == 200
    assert "cache-control" not in r.headers


def test_el_prefijo_y_el_max_age_son_configurables():
    """Sin esto, cambiar la política de caché obligaría a tocar el código."""
    app = FastAPI()
    app.add_middleware(CacheControlMiddleware, prefijo="/otro", max_age=60)

    @app.get("/otro/cosa")
    def cosa():
        return {"ok": True}

    r = TestClient(app).get("/otro/cosa")
    assert r.headers["cache-control"] == "public, max-age=60, immutable"
