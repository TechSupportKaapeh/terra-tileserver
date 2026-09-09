"""Tests del reporte de arranque del tileserver.

Dos cosas se cuidan acá por encima del resto:

1. **Que no se escape ningún secreto.** El reporte imprime a nivel INFO, en un
   log que otra persona va a leer. Es el peor lugar posible para un descuido, y
   acá hay dos secretos en juego: la secret key de MinIO y `MAP_TOKEN_SECRET`.

2. **Que el endpoint y el modo salgan juntos.** Tres de los cuatro errores
   recurrentes de este despliegue son incoherencias entre `MINIO_ENDPOINT` y
   `MINIO_SECURE`, y ninguno se ve mirando una variable sola.
"""

import logging

import pytest

from terra_tiles.arranque import (
    ARTE,
    describir_secreto,
    registrar_arranque,
    reporte_de_conexiones,
    reporte_de_configuracion,
)
from terra_tiles.health import DEGRADADO, ERROR, OK, Comprobacion
from terra_tiles.logging_config import configurar_logging, nivel_desde_entorno
from terra_tiles.settings import Settings


SECRET_KEY = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"
MAP_SECRET = "un-secreto-de-mapa-de-mas-de-32-caracteres"


def hacer_settings(**cambios) -> Settings:
    base = dict(
        map_token_secret=MAP_SECRET,
        minio_bucket="terra-assets",
        minio_endpoint="bucket.railway.internal:9000",
        minio_access_key="tiler-ro",
        minio_secret_key=SECRET_KEY,
        minio_secure=False,
        aws_region="us-east-1",
    )
    base.update(cambios)
    return Settings(**base)


def texto(settings=None, entorno=None) -> str:
    return "\n".join(
        reporte_de_configuracion(settings or hacer_settings(), entorno or {})
    )


# --------------------------------------------------------------------------
# lo primero: ningún secreto en la salida
# --------------------------------------------------------------------------

@pytest.mark.parametrize("secreto", [SECRET_KEY, MAP_SECRET])
def test_ningun_secreto_aparece_en_el_reporte(secreto):
    salida = texto()
    assert secreto not in salida
    # Ni un prefijo: ocho caracteres ya reducen muchísimo el espacio de
    # búsqueda de quien tenga el log.
    assert secreto[:8] not in salida
    assert secreto[-8:] not in salida
    assert f"({len(secreto)} chars)" in salida


def test_la_access_key_si_se_muestra():
    # No es un secreto —es el nombre de usuario— y es lo que uno necesita ver
    # para saber si el servicio está usando `tiler-ro` o cayó al default.
    assert "tiler-ro" in texto()


def test_describir_secreto_marca_el_minimo():
    assert "DEMASIADO CORTO (minimo 32)" in describir_secreto("corto", 32)
    assert "DEMASIADO CORTO" not in describir_secreto("x" * 40, 32)


def test_describir_secreto_marca_los_bordes_sucios():
    # Railway no recorta nada; python-dotenv sí. El mismo valor anda en local y
    # falla desplegado.
    assert "ESPACIOS EN LOS BORDES" in describir_secreto(" " + "x" * 40)


def test_describir_secreto_ausente():
    assert describir_secreto(None) == "AUSENTE"
    assert describir_secreto("") == "AUSENTE"


# --------------------------------------------------------------------------
# el endpoint y el modo, juntos
# --------------------------------------------------------------------------

def test_el_endpoint_se_imprime_completo():
    # A diferencia de `/health/ready`, que lo oculta porque es público: el log
    # del contenedor no lo es, y sin el valor no se puede diagnosticar nada.
    assert "bucket.railway.internal:9000" in texto()


def test_el_modo_se_traduce_a_esquema():
    assert "-> http" in texto(hacer_settings(minio_secure=False))
    assert "-> https" in texto(hacer_settings(minio_secure=True))


# --------------------------------------------------------------------------
# CORS: vacío no es lo mismo que ausente
# --------------------------------------------------------------------------

def test_cors_abierto_se_explica_en_vez_de_alarmar():
    salida = texto(hacer_settings(cors_origins=["*"]))
    # Un aviso que grita por algo aceptable se vuelve ruido que se ignora.
    assert "no hay cookies" in salida


def test_cors_vacio_se_marca_como_el_error_que_es():
    salida = texto(hacer_settings(cors_origins=[]))
    assert "CERO origenes" in salida


def test_cors_con_origenes_los_lista():
    salida = texto(hacer_settings(cors_origins=["https://a.mx", "https://b.mx"]))
    assert "https://a.mx" in salida and "https://b.mx" in salida


# --------------------------------------------------------------------------
# el resumen de conexiones
# --------------------------------------------------------------------------

def test_un_error_se_lista_y_explica_el_sintoma():
    salida = "\n".join(reporte_de_conexiones([
        Comprobacion("minio", ERROR, "no responde"),
        Comprobacion("map_token_secret", OK, "Configurado."),
    ]))
    assert "NO FUNCIONAN: minio" in salida
    # Lo importante: que el fallo NO se ve desde acá, se ve en el navegador de
    # otra persona.
    assert "401/403/503" in salida


def test_degradado_no_se_confunde_con_roto():
    # `degradado` significa "no se pudo concluir". Mezclarlo con un fallo real
    # convierte el reporte en ruido; es el mismo criterio con el que
    # `informe()` devuelve 200 y no 503.
    salida = "\n".join(reporte_de_conexiones([
        Comprobacion("minio_endpoint", DEGRADADO, "no se pudo concluir"),
    ]))
    assert "SIN CONCLUIR" in salida
    assert "NO FUNCIONAN" not in salida


def test_todo_ok_lo_dice():
    salida = "\n".join(reporte_de_conexiones([Comprobacion("minio", OK, "bien")]))
    assert "Todo verificado responde" in salida
    assert "NO FUNCIONAN" not in salida


# --------------------------------------------------------------------------
# el arte y el formato
# --------------------------------------------------------------------------

def test_el_arte_es_ascii_y_no_trae_saltos_internos():
    for linea in ARTE:
        linea.encode("ascii")
        assert "\n" not in linea


def test_ninguna_linea_del_reporte_trae_saltos_internos():
    # Una línea por registro: si trajera un salto adentro, cualquier
    # formateador estructurado lo escaparía y el bloque quedaría ilegible.
    for linea in reporte_de_configuracion(hacer_settings(), {}):
        assert "\n" not in linea


# --------------------------------------------------------------------------
# registrar_arranque no puede tumbar el proceso
# --------------------------------------------------------------------------

def test_registrar_arranque_no_levanta_si_el_sondeo_explota(caplog):
    def cliente_roto():
        raise RuntimeError("MinIO no existe")

    with caplog.at_level(logging.INFO):
        comprobaciones = registrar_arranque(
            hacer_settings(), {}, crear_cliente=cliente_roto)

    # Devolvió algo y no levantó: es la regla de los tres servicios.
    assert isinstance(comprobaciones, list)


def _cliente_roto():
    raise RuntimeError("MinIO no existe")


def test_registrar_arranque_escribe_las_tres_secciones(caplog):
    with caplog.at_level(logging.INFO):
        registrar_arranque(hacer_settings(), {}, crear_cliente=_cliente_roto)

    # `getMessage()` aplica los args del registro; leer `.message` crudo deja
    # los `%s` sin sustituir.
    salida = "\n".join(r.getMessage() for r in caplog.records)
    assert ARTE[1] in salida            # el dibujo
    assert "CONFIGURACION" in salida    # la config
    assert "CONEXIONES" in salida       # los chequeos


def test_un_fallo_sale_tambien_como_error(caplog):
    # Aparte del bloque INFO, para que sobreviva a cualquier LOG_LEVEL y para
    # que Railway lo destaque.
    with caplog.at_level(logging.INFO):
        registrar_arranque(hacer_settings(), {}, crear_cliente=_cliente_roto)

    errores = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Configuracion minio:" in m for m in errores)


# --------------------------------------------------------------------------
# la configuración de logging que faltaba
# --------------------------------------------------------------------------

def test_nivel_por_defecto_es_info():
    assert nivel_desde_entorno(None) == logging.INFO
    assert nivel_desde_entorno("") == logging.INFO


def test_los_niveles_validos_se_respetan():
    assert nivel_desde_entorno("debug") == logging.DEBUG
    assert nivel_desde_entorno("WARNING") == logging.WARNING
    assert nivel_desde_entorno("  error  ") == logging.ERROR


def test_un_nivel_invalido_cae_a_info_y_no_deja_mudo_el_servicio():
    # Un LOG_LEVEL mal escrito no puede reproducir el problema que este módulo
    # vino a arreglar.
    assert nivel_desde_entorno("VERBOSO") == logging.INFO
    assert nivel_desde_entorno("42x") == logging.INFO


def test_configurar_logging_hace_que_un_info_del_servicio_se_vea(capsys):
    # Sin esto, un `logger.info` de `terra_tiles.*` se descarta: uvicorn no
    # configura el root logger.
    configurar_logging({"LOG_LEVEL": "INFO"})
    logging.getLogger("terra_tiles.prueba").info("hola")
    assert "hola" in capsys.readouterr().out


def test_configurar_logging_es_idempotente(capsys):
    configurar_logging({})
    configurar_logging({})
    logging.getLogger("terra_tiles.prueba").info("una sola vez")
    # Sin `force=True` cada llamada agregaría un handler y duplicaría la línea.
    assert capsys.readouterr().out.count("una sola vez") == 1


def test_configurar_logging_no_muere_con_un_stream_no_reconfigurable(monkeypatch):
    # pytest reemplaza sys.stdout por un objeto que no siempre tiene
    # `reconfigure`. Eso no puede ser lo que rompa el arranque.
    import sys as _sys

    class SinReconfigure:
        def write(self, _s): return 0
        def flush(self): pass

    monkeypatch.setattr(_sys, "stdout", SinReconfigure())
    configurar_logging({})  # no debe levantar
