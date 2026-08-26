"""Tests del chequeo de estado.

Ninguno toca la red: `comprobar_minio` recibe una fábrica de cliente, así que
los modos de fallo se simulan con excepciones. Eso es justamente lo que hace
testeable el caso interesante — nadie va a apagar MinIO para probar el timeout.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from terra_tiles.health import (
    DEGRADADO,
    ERROR,
    OK,
    Comprobacion,
    comprobar_minio,
    comprobar_token_secret,
    informe,
)
from terra_tiles.settings import Settings

ENDPOINT_INTERNO = "minio.railway.internal:9000"
SECRETO = "secreto-compartido-con-geocore"

# Valores deliberadamente distintos del string "tiler-ro". Los mensajes del
# chequeo nombran a `tiler-ro` como el usuario documentado de MinIO —eso es
# documentación, no configuración— y usar el mismo string acá haría que el test
# de filtrado no distinguiera una cosa de la otra.
ACCESS_KEY = "access-key-de-prueba"
SECRET_KEY = "secret-key-de-prueba"


def _settings(**overrides) -> Settings:
    base = dict(
        map_token_secret=SECRETO,
        minio_bucket="terra-assets",
        minio_endpoint=ENDPOINT_INTERNO,
        minio_access_key=ACCESS_KEY,
        minio_secret_key=SECRET_KEY,
        minio_secure=False,
        aws_region="us-east-1",
    )
    base.update(overrides)
    return Settings(**base)


class _ErrorS3(Exception):
    """Imita el `S3Error` de minio-py: lo que importa es el atributo `code`."""

    def __init__(self, code: str) -> None:
        super().__init__(f"S3 dijo {code} sobre {ENDPOINT_INTERNO}")
        self.code = code


class _ClienteQueFalla:
    def __init__(self, excepcion: Exception) -> None:
        self._excepcion = excepcion

    def stat_object(self, bucket, key):
        raise self._excepcion


class _ClienteQueResponde:
    def stat_object(self, bucket, key):
        return object()


# --------------------------------------------------------------------------
# El secreto del token
# --------------------------------------------------------------------------

def test_secreto_presente_es_ok():
    assert comprobar_token_secret(_settings()).estado == OK


def test_sin_secreto_es_error():
    # Falta MAP_TOKEN_SECRET: el servicio arranca y /health da 200, pero todos
    # los tiles dan 503. Tiene que salir como error, no como advertencia.
    c = comprobar_token_secret(_settings(map_token_secret=None))
    assert c.estado == ERROR
    assert "MAP_TOKEN_SECRET" in c.detalle


# --------------------------------------------------------------------------
# El sondeo a MinIO
# --------------------------------------------------------------------------

def test_nosuchkey_es_ok():
    """La key de sondeo no existe: esa es la respuesta que se busca.

    Prueba conectividad, credenciales, bucket y `s3:GetObject` de una vez.
    """
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    assert c.estado == OK


def test_si_el_objeto_existe_tambien_es_ok():
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueResponde())
    assert c.estado == OK


@pytest.mark.parametrize(
    "codigo, pista",
    [
        ("NoSuchBucket", "MINIO_BUCKET"),
        ("InvalidAccessKeyId", "MINIO_ACCESS_KEY"),
        ("SignatureDoesNotMatch", "MINIO_SECRET_KEY"),
    ],
)
def test_errores_de_configuracion_nombran_la_variable(codigo, pista):
    """Cada fallo tiene que decir qué variable revisar, no solo que falló."""
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3(codigo)))
    assert c.estado == ERROR
    assert pista in c.detalle


def test_accessdenied_es_degradado_no_error():
    """AccessDenied no permite concluir, y decir "roto" sería mentir.

    Puede ser una policy sin `s3:GetObject` (roto de verdad) o MinIO ocultando
    que la key no existe (todo bien). El chequeo lo dice en vez de adivinar.
    """
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("AccessDenied")))
    assert c.estado == DEGRADADO
    assert c.ok is True


def test_codigo_desconocido_es_error():
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("TeapotError")))
    assert c.estado == ERROR
    assert "TeapotError" in c.detalle


def test_sin_respuesta_sugiere_endpoint_y_secure():
    """Una excepción sin `code` es de red: DNS, ruta o timeout."""
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(OSError("timed out")))
    assert c.estado == ERROR
    assert "MINIO_ENDPOINT" in c.detalle
    assert "MINIO_SECURE" in c.detalle


def test_sin_credenciales_es_error_sin_tocar_la_red():
    def _no_deberia_llamarse():
        raise AssertionError("no hay que construir el cliente sin credenciales")

    c = comprobar_minio(
        _settings(minio_access_key="", minio_secret_key=""),
        crear_cliente=_no_deberia_llamarse,
    )
    assert c.estado == ERROR


def test_fallo_al_construir_el_cliente_no_propaga():
    """Un chequeo de salud que revienta con 500 no informa nada."""
    def _revienta():
        raise ValueError("endpoint con formato inválido")

    c = comprobar_minio(_settings(), crear_cliente=_revienta)
    assert c.estado == ERROR
    assert "ValueError" in c.detalle


# --------------------------------------------------------------------------
# Que el informe no filtre nada
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "codigo",
    ["NoSuchKey", "NoSuchBucket", "AccessDenied", "SignatureDoesNotMatch", "TeapotError"],
)
def test_el_informe_nunca_publica_el_secreto_ni_el_endpoint(codigo):
    """`/health/ready` es público: no puede filtrar topología ni secretos.

    El mensaje crudo de minio-py incluye el endpoint, por eso no se devuelve.
    """
    cuerpo, _ = informe(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3(codigo)))
    texto = str(cuerpo)
    assert SECRETO not in texto
    assert ENDPOINT_INTERNO not in texto
    assert ACCESS_KEY not in texto
    assert SECRET_KEY not in texto


def test_el_informe_publica_el_bucket():
    # El bucket no es secreto: viaja en cada `?url=s3://bucket/...` de tile, y
    # es justo el dato que hace falta para diagnosticar un 400 URL_NO_PERMITIDA.
    cuerpo, _ = informe(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    assert cuerpo["bucket"] == "terra-assets"


# --------------------------------------------------------------------------
# Códigos HTTP
# --------------------------------------------------------------------------

def test_todo_bien_da_200_y_ok():
    cuerpo, codigo = informe(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    assert (codigo, cuerpo["status"]) == (200, OK)


def test_degradado_da_200():
    """Ante la duda no se grita fallo: un chequeo ruidoso se aprende a ignorar."""
    cuerpo, codigo = informe(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("AccessDenied")))
    assert (codigo, cuerpo["status"]) == (200, DEGRADADO)


def test_error_da_503():
    cuerpo, codigo = informe(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchBucket")))
    assert (codigo, cuerpo["status"]) == (503, ERROR)


def test_un_error_arrastra_aunque_lo_otro_este_bien():
    """MinIO sano no compensa que falte el secreto: los tiles darían 503 igual."""
    cuerpo, codigo = informe(
        _settings(map_token_secret=None),
        crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")),
    )
    assert codigo == 503
    estados = {c["name"]: c["status"] for c in cuerpo["checks"]}
    assert estados == {"map_token_secret": ERROR, "minio": OK}


# --------------------------------------------------------------------------
# El endpoint, montado como en main.py
# --------------------------------------------------------------------------

def _app(cliente_factory) -> TestClient:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "tileserver-titiler"}

    @app.get("/health/ready")
    def ready():
        cuerpo, codigo = informe(_settings(), crear_cliente=cliente_factory)
        return JSONResponse(cuerpo, status_code=codigo)

    return TestClient(app, raise_server_exceptions=False)


def test_liveness_responde_200_aunque_minio_este_caido():
    """El punto entero de separar los dos chequeos.

    Si `/health` dependiera de MinIO, Railway reiniciaría el contenedor ante
    cada parpadeo del storage — reiniciando un servicio que está sano.
    """
    cliente = _app(lambda: _ClienteQueFalla(OSError("connection refused")))
    assert cliente.get("/health").status_code == 200
    assert cliente.get("/health/ready").status_code == 503


def test_readiness_devuelve_json_con_los_checks():
    cliente = _app(lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    r = cliente.get("/health/ready")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["status"] == OK
    assert [c["name"] for c in cuerpo["checks"]] == ["map_token_secret", "minio"]


def test_comprobacion_ok_es_falso_solo_en_error():
    assert Comprobacion("x", OK, "").ok is True
    assert Comprobacion("x", DEGRADADO, "").ok is True
    assert Comprobacion("x", ERROR, "").ok is False
