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
    comprobar_credenciales,
    comprobar_endpoint,
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
# De dónde salieron las credenciales
# --------------------------------------------------------------------------

_ENTORNO_COMPLETO = {"MINIO_ACCESS_KEY": "tiler-ro", "MINIO_SECRET_KEY": "s3cr3to"}


def test_credenciales_del_entorno_son_ok():
    c = comprobar_credenciales(_settings(), _ENTORNO_COMPLETO)
    assert c.estado == OK


def test_credenciales_por_defecto_avisan():
    """Un nombre de variable mal escrito se disfraza de error de permisos.

    `Settings.from_env` cae a 'minioadmin' sin decir nada, y el AccessDenied que
    sale después manda a revisar la policy, donde no hay nada malo.
    """
    c = comprobar_credenciales(_settings(minio_access_key="minioadmin"), {})
    assert (c.estado, c.causa) == (DEGRADADO, "credencial_por_defecto")
    assert "MINIO_ACCESS_KEY" in c.detalle
    assert "minioadmin" in c.detalle


def test_detecta_el_nombre_de_variable_parecido():
    """MINIO_ROOT_USER es como se llaman las variables DEL SERVICIO de MinIO.

    Copiarlas tal cual al tileserver es el error natural, y el mensaje tiene que
    nombrar la que sí encontró para que se vea el desfase.
    """
    c = comprobar_credenciales(
        _settings(),
        {"MINIO_ROOT_USER": "tiler-ro", "MINIO_ROOT_PASSWORD": "x"},
    )
    assert c.causa == "credencial_por_defecto"
    assert "MINIO_ROOT_USER" in c.detalle
    assert "MINIO_ROOT_PASSWORD" in c.detalle


def test_no_confunde_las_aws_que_pone_configure_gdal():
    """`configure_gdal()` define AWS_ACCESS_KEY_ID siempre: señalarla sería un
    falso positivo garantizado."""
    c = comprobar_credenciales(
        _settings(), {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
    )
    assert "AWS_ACCESS_KEY_ID" not in c.detalle


def test_una_sola_variable_faltante_se_nombra_sola():
    c = comprobar_credenciales(_settings(), {"MINIO_ACCESS_KEY": "tiler-ro"})
    assert "MINIO_SECRET_KEY" in c.detalle
    assert "MINIO_ACCESS_KEY y" not in c.detalle


def test_la_access_key_se_informa_enmascarada():
    """Sirve para distinguir tiler-ro de minioadmin, no para usarla."""
    c = comprobar_credenciales(_settings(minio_access_key="tiler-ro"), _ENTORNO_COMPLETO)
    assert "ti…" in c.detalle
    assert "tiler-ro" not in c.detalle


def test_el_secreto_nunca_aparece():
    c = comprobar_credenciales(
        _settings(minio_secret_key="secreto-larguisimo-de-minio"), _ENTORNO_COMPLETO
    )
    assert "secreto-larguisimo-de-minio" not in c.detalle


# --------------------------------------------------------------------------
# La forma del endpoint, sin tocar la red
# --------------------------------------------------------------------------

def test_privado_sin_puerto_es_el_error_mas_comun():
    """La red privada de Railway no mapea puertos: sin :9000 se asume el 80.

    Es al revés que el dominio público, y por eso se confunde.
    """
    c = comprobar_endpoint(_settings(minio_endpoint="minio.railway.internal"))
    assert (c.estado, c.causa) == (DEGRADADO, "falta_puerto")
    assert ":9000" in c.detalle


def test_privado_con_puerto_esta_bien():
    c = comprobar_endpoint(_settings(minio_endpoint="minio.railway.internal:9000"))
    assert c.estado == OK


def test_privado_con_tls_avisa():
    c = comprobar_endpoint(
        _settings(minio_endpoint="minio.railway.internal:9000", minio_secure=True)
    )
    assert c.causa == "tls_en_privado"


def test_publico_con_puerto_avisa():
    c = comprobar_endpoint(
        _settings(minio_endpoint="bucket-x.up.railway.app:9000", minio_secure=True)
    )
    assert c.causa == "puerto_en_publico"


def test_publico_sin_tls_avisa():
    c = comprobar_endpoint(
        _settings(minio_endpoint="bucket-x.up.railway.app", minio_secure=False)
    )
    assert c.causa == "sin_tls_en_publico"


def test_publico_bien_configurado():
    c = comprobar_endpoint(
        _settings(minio_endpoint="bucket-x.up.railway.app", minio_secure=True)
    )
    assert c.estado == OK


@pytest.mark.parametrize("host", ["localhost:9000", "127.0.0.1:9000", "localhost"])
def test_localhost_avisa_que_no_hay_nada_ahi(host):
    # Es el .env de docker-compose quedado de antes: dentro del contenedor,
    # localhost es el propio contenedor.
    assert comprobar_endpoint(_settings(minio_endpoint=host)).causa == "localhost"


def test_un_host_cualquiera_no_molesta():
    """Las heurísticas son solo para los dominios de Railway."""
    c = comprobar_endpoint(_settings(minio_endpoint="storage.interno.example:9000"))
    assert c.estado == OK


@pytest.mark.parametrize(
    "endpoint, esperado",
    [
        ("minio.railway.internal:9000", ("minio.railway.internal", 9000)),
        ("minio.railway.internal", ("minio.railway.internal", None)),
        ("[::1]:9000", ("::1", 9000)),
        ("[fd12::34]", ("fd12::34", None)),
        # Sin corchetes no se puede distinguir el último grupo de un puerto.
        # Partir por el último ':' daría host 'fd12:' y puerto 34: se prefiere
        # no adivinar antes que devolver algo mal partido.
        ("fd12::34", ("fd12::34", None)),
    ],
)
def test_partir_host_y_puerto(endpoint, esperado):
    from terra_tiles.health import _partir_host_y_puerto

    assert _partir_host_y_puerto(endpoint) == esperado


def test_la_forma_del_endpoint_no_tumba_el_informe_a_503():
    """Son heurísticas: no pueden provocar un 503 por sí solas."""
    cuerpo, codigo = informe(_settings(minio_endpoint="minio.railway.internal"), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")),
    )
    assert (codigo, cuerpo["status"]) == (200, DEGRADADO)


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


# --------------------------------------------------------------------------
# AccessDenied: el único caso ambiguo, y cómo se desambigua
# --------------------------------------------------------------------------

class _ClienteConDenegacion:
    """Niega el objeto y responde lo que se le diga sobre el bucket.

    Reproduce el escenario real: `stat_object` da AccessDenied y `bucket_exists`
    —que usa un permiso distinto, `s3:ListBucket`— decide qué significaba.
    """

    def __init__(self, bucket) -> None:
        self._bucket = bucket

    def stat_object(self, bucket, key):
        raise _ErrorS3("AccessDenied")

    def bucket_exists(self, bucket):
        if isinstance(self._bucket, BaseException):
            raise self._bucket
        return self._bucket


def test_denegado_pero_el_bucket_existe_es_ok():
    """S3 responde 403 en vez de 404 para no filtrar qué keys existen.

    Si el bucket se puede consultar, la negativa era eso y los tiles funcionan.
    """
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteConDenegacion(True))
    assert (c.estado, c.causa) == (OK, "key_de_sondeo_inexistente")


def test_denegado_y_el_bucket_no_existe_senala_el_nombre():
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteConDenegacion(False))
    assert (c.estado, c.causa) == (ERROR, "bucket_inexistente")
    assert "MINIO_BUCKET" in c.detalle


def test_nosuchbucket_no_se_confunde_con_falta_de_permiso():
    """Son diagnósticos distintos: en NoSuchBucket MinIO sí nos dejó preguntar.

    Estaban en la misma rama y el mensaje hablaba solo de la policy, mandando a
    revisar permisos cuando el problema era el nombre del bucket.
    """
    c = comprobar_minio(
        _settings(), crear_cliente=lambda: _ClienteConDenegacion(_ErrorS3("NoSuchBucket"))
    )
    assert c.causa == "bucket_inexistente"


# --------------------------------------------------------------------------
# El cliente real: qué permisos exige
# --------------------------------------------------------------------------

def test_el_cliente_lleva_region_fija():
    """Sin `region`, minio-py llama a GetBucketLocation antes de cada operación.

    Eso exige `s3:GetBucketLocation`, un permiso que GDAL NO necesita —/vsis3/
    usa AWS_REGION directamente—, así que una policy de solo lectura válida para
    servir tiles hacía fallar el chequeo con AccessDenied en las DOS operaciones
    a la vez, simulando un problema de fondo. El sondeo tiene que ejercitar los
    mismos permisos que el camino real, ni uno más.
    """
    from terra_tiles.health import _cliente_por_defecto

    cliente = _cliente_por_defecto(_settings(aws_region="us-east-1"))
    assert cliente._base_url.region == "us-east-1"


def test_denegado_dos_veces_senala_la_policy():
    """Denegar también la consulta del bucket descarta que sea el ocultamiento.

    Las credenciales son válidas (si no, sería SignatureDoesNotMatch), así que
    lo que falla es que la policy no llega a este usuario o a este bucket.
    """
    c = comprobar_minio(
        _settings(), crear_cliente=lambda: _ClienteConDenegacion(_ErrorS3("AccessDenied"))
    )
    assert (c.estado, c.causa) == (ERROR, "policy_no_asignada")
    assert "ASIGNADA" in c.detalle


def test_si_el_segundo_sondeo_falla_se_vuelve_al_estado_ambiguo():
    """No haber podido concluir no es haber concluido que está roto.

    Convertir un sondeo fallido en un diagnóstico distinto sería inventar: se
    devuelve el `degradado` original, que ya dice que no se puede concluir.
    """
    c = comprobar_minio(
        _settings(), crear_cliente=lambda: _ClienteConDenegacion(OSError("connection reset"))
    )
    assert c.estado == DEGRADADO


def test_un_cliente_sin_bucket_exists_tampoco_rompe():
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("AccessDenied")))
    assert c.estado == DEGRADADO


def test_codigo_desconocido_es_error():
    c = comprobar_minio(_settings(), crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("TeapotError")))
    assert c.estado == ERROR
    assert "TeapotError" in c.detalle


# --------------------------------------------------------------------------
# Fallos de red: cada causa manda a revisar algo distinto
# --------------------------------------------------------------------------

class _MaxRetryError(Exception):
    """Imita el envoltorio de urllib3: la causa real vive en `.reason`."""

    def __init__(self, reason: BaseException) -> None:
        super().__init__(f"max retries con {ENDPOINT_INTERNO}")
        self.reason = reason


class _ProtocolError(Exception):
    """Imita `urllib3.exceptions.ProtocolError`."""


def _causa_de(excepcion: Exception, *, secure: bool = False) -> Comprobacion:
    return comprobar_minio(
        _settings(minio_secure=secure),
        crear_cliente=lambda: _ClienteQueFalla(excepcion),
    )


def test_dns_apunta_al_nombre_del_servicio():
    """El hostname privado sale del nombre del servicio, no siempre es 'minio'."""
    c = _causa_de(_MaxRetryError(OSError("[Errno -2] Name or service not known")))
    assert (c.estado, c.causa) == (ERROR, "dns")
    assert "railway.internal" in c.detalle


def test_tls_apunta_a_minio_secure():
    """MINIO_SECURE=True contra el dominio privado, que va en texto plano."""
    c = _causa_de(_MaxRetryError(Exception("SSLError: WRONG_VERSION_NUMBER")))
    assert (c.estado, c.causa) == (ERROR, "tls")
    assert "MINIO_SECURE" in c.detalle


def test_conexion_rechazada_apunta_al_puerto_y_a_ipv6():
    """Puerto equivocado (9001 es la consola) o MinIO escuchando solo en IPv4."""
    c = _causa_de(_MaxRetryError(ConnectionRefusedError("[Errno 111] Connection refused")))
    assert (c.estado, c.causa) == (ERROR, "rechazado")
    assert "9000" in c.detalle
    assert "::" in c.detalle


def test_timeout_no_se_confunde_con_rechazo():
    c = _causa_de(_MaxRetryError(Exception("ConnectTimeoutError: timed out")))
    assert c.causa == "timeout"


# Las dos que siguen usan la forma REAL que produce urllib3, verificada contra
# un socket de texto plano. Hablarle TLS a un puerto sin TLS **no genera ningún
# error de SSL**: genera un ConnectionReset envuelto en ProtocolError. Una
# versión anterior de esta clasificación buscaba "sslerror" y por eso nunca
# disparaba — los tests pasaban porque usaban una excepción inventada.
def test_corte_con_tls_pedido_manda_a_apagar_minio_secure():
    c = _causa_de(
        _MaxRetryError(_ProtocolError("('Connection aborted.', ConnectionResetError(10054))")),
        secure=True,
    )
    assert (c.estado, c.causa) == (ERROR, "tls")
    assert "MINIO_SECURE=False" in c.detalle


def test_corte_sin_tls_pedido_manda_a_encenderlo():
    """La misma excepción, la acción contraria: depende de si pedimos TLS."""
    c = _causa_de(
        _MaxRetryError(_ProtocolError("('Connection aborted.', ConnectionResetError(10054))")),
        secure=False,
    )
    assert (c.estado, c.causa) == (ERROR, "tls")
    assert "MINIO_SECURE=True" in c.detalle


def test_sin_ruta_al_host_menciona_el_proyecto_y_ipv6():
    c = _causa_de(_MaxRetryError(OSError("[Errno 113] No route to host")))
    assert c.causa == "inalcanzable"
    assert "::" in c.detalle


def test_causa_desconocida_no_revienta():
    c = _causa_de(_MaxRetryError(Exception("algo rarísimo")))
    assert (c.estado, c.causa) == (ERROR, "desconocido")


def test_se_recorre_la_cadena_no_solo_la_excepcion_de_arriba():
    """minio-py envuelve todo en MaxRetryError: mirar solo el nivel de arriba
    clasificaría cualquier fallo de red como desconocido."""
    envuelta = _MaxRetryError(OSError("getaddrinfo failed"))
    assert _causa_de(envuelta).causa == "dns"


def test_una_cadena_ciclica_no_cuelga():
    a = Exception("a")
    b = Exception("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _causa_de(a).causa == "desconocido"


def test_la_causa_de_red_no_filtra_el_endpoint():
    """El mensaje crudo de urllib3 incluye el host: se usa para clasificar,
    nunca se devuelve."""
    c = _causa_de(_MaxRetryError(OSError(f"no resolvió {ENDPOINT_INTERNO}")))
    assert ENDPOINT_INTERNO not in c.detalle


def test_la_causa_viaja_en_el_json():
    cuerpo, _ = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_MaxRetryError(OSError("Name or service not known"))),
    )
    minio = next(c for c in cuerpo["checks"] if c["name"] == "minio")
    assert minio["cause"] == "dns"


def test_sin_causa_no_aparece_la_clave():
    # Evita llenar la respuesta de nulls cuando no hay nada que informar.
    cuerpo, _ = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    minio = next(c for c in cuerpo["checks"] if c["name"] == "minio")
    assert "cause" not in minio


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
    cuerpo, _ = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3(codigo)))
    texto = str(cuerpo)
    assert SECRETO not in texto
    assert ENDPOINT_INTERNO not in texto
    assert ACCESS_KEY not in texto
    assert SECRET_KEY not in texto


def test_el_informe_publica_el_bucket():
    # El bucket no es secreto: viaja en cada `?url=s3://bucket/...` de tile, y
    # es justo el dato que hace falta para diagnosticar un 400 URL_NO_PERMITIDA.
    cuerpo, _ = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    assert cuerpo["bucket"] == "terra-assets"


# --------------------------------------------------------------------------
# Códigos HTTP
# --------------------------------------------------------------------------

def test_todo_bien_da_200_y_ok():
    cuerpo, codigo = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")))
    assert (codigo, cuerpo["status"]) == (200, OK)


def test_degradado_da_200():
    """Ante la duda no se grita fallo: un chequeo ruidoso se aprende a ignorar."""
    cuerpo, codigo = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("AccessDenied")))
    assert (codigo, cuerpo["status"]) == (200, DEGRADADO)


def test_error_da_503():
    cuerpo, codigo = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchBucket")))
    assert (codigo, cuerpo["status"]) == (503, ERROR)


def test_un_error_arrastra_aunque_lo_otro_este_bien():
    """MinIO sano no compensa que falte el secreto: los tiles darían 503 igual."""
    cuerpo, codigo = informe(_settings(map_token_secret=None), entorno=_ENTORNO_COMPLETO, crear_cliente=lambda: _ClienteQueFalla(_ErrorS3("NoSuchKey")),
    )
    assert codigo == 503
    estados = {c["name"]: c["status"] for c in cuerpo["checks"]}
    assert estados == {
        "map_token_secret": ERROR,
        "minio_endpoint": OK,
        "minio_credenciales": OK,
        "minio": OK,
    }


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
        cuerpo, codigo = informe(_settings(), entorno=_ENTORNO_COMPLETO, crear_cliente=cliente_factory)
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
    assert [c["name"] for c in cuerpo["checks"]] == [
        "map_token_secret",
        "minio_endpoint",
        "minio_credenciales",
        "minio",
    ]


def test_comprobacion_ok_es_falso_solo_en_error():
    assert Comprobacion("x", OK, "").ok is True
    assert Comprobacion("x", DEGRADADO, "").ok is True
    assert Comprobacion("x", ERROR, "").ok is False
