"""Tests de los dos controles de acceso.

Se prueban las fábricas directamente, sin levantar la app: son funciones puras
respecto de su configuración, así que no hace falta TiTiler ni GDAL instalados.
"""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
from fastapi import HTTPException

from terra_tiles.security import (
    CODIGO_SIN_SECRETO,
    CODIGO_URL_NO_PERMITIDA,
    crear_validador_de_ruta,
    crear_validador_de_token,
)

SECRETO = "un-secreto-de-produccion-de-48-caracteres-abcdefgh"
# El que estaba hardcodeado en el repo antes del fix. Se prueba explícitamente
# que ya no abre nada: es una regresión que no queremos volver a introducir.
SECRETO_VIEJO_DEL_REPO = "default_terra_map_token_secret_which_is_at_least_32_chars_long"


def _token(secreto: str = SECRETO, *, tipo: str = "map-access", aud: str = "TiTiler",
           iss: str = "Geocore", vence_en: dt.timedelta = dt.timedelta(hours=1)) -> str:
    ahora = dt.datetime.now(dt.timezone.utc)
    return jwt.encode(
        {"aud": aud, "iss": iss, "type": tipo, "iat": ahora, "exp": ahora + vence_en},
        secreto,
        algorithm="HS256",
    )


def _estado(excinfo) -> int:
    return excinfo.value.status_code


def _codigo(excinfo):
    detalle = excinfo.value.detail
    return detalle.get("code") if isinstance(detalle, dict) else None


# --------------------------------------------------------------------------
#  Token de mapa  (OWASP A01)
# --------------------------------------------------------------------------

class TestValidadorDeToken:

    def test_sin_secreto_configurado_devuelve_503_y_no_valida_nada(self):
        """Falla cerrado: sin secreto, ni siquiera se mira el token."""
        validar = crear_validador_de_token(None)
        with pytest.raises(HTTPException) as e:
            validar(_token())          # token perfectamente válido
        assert _estado(e) == 503
        assert _codigo(e) == CODIGO_SIN_SECRETO

    def test_secreto_vacio_se_trata_como_ausente(self):
        validar = crear_validador_de_token("")
        with pytest.raises(HTTPException) as e:
            validar("lo-que-sea")
        assert _estado(e) == 503

    def test_token_valido_pasa(self):
        validar = crear_validador_de_token(SECRETO)
        assert validar(_token()) is None

    def test_sin_token_devuelve_401(self):
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(None)
        assert _estado(e) == 401

    def test_token_firmado_con_el_secreto_viejo_del_repo_no_abre(self):
        """Regresión del fallo que arreglamos: el default hardcodeado."""
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(_token(SECRETO_VIEJO_DEL_REPO))
        assert _estado(e) == 401

    def test_token_expirado_devuelve_401(self):
        validar = crear_validador_de_token(SECRETO, leeway=0)
        with pytest.raises(HTTPException) as e:
            validar(_token(vence_en=dt.timedelta(hours=-1)))
        assert _estado(e) == 401

    def test_leeway_tolera_desfase_de_reloj(self):
        """Un token vencido hace 10 s pasa con 30 s de margen.

        Cubre el caso real: relojes desincronizados entre Geocore y este
        servicio produciendo 401 intermitentes.
        """
        validar = crear_validador_de_token(SECRETO, leeway=30)
        assert validar(_token(vence_en=dt.timedelta(seconds=-10))) is None

    def test_audiencia_ajena_devuelve_401(self):
        """Un token legítimo emitido para otro servicio no sirve acá."""
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(_token(aud="OtroServicio"))
        assert _estado(e) == 401

    def test_emisor_ajeno_devuelve_401(self):
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(_token(iss="NoEsGeocore"))
        assert _estado(e) == 401

    def test_tipo_incorrecto_devuelve_403(self):
        """Firma válida pero no es un token de acceso a mapas: 403, no 401."""
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(_token(tipo="refresh"))
        assert _estado(e) == 403

    def test_token_sin_firma_no_se_acepta(self):
        """`alg: none` es el ataque clásico contra JWT mal configurado."""
        validar = crear_validador_de_token(SECRETO)
        sin_firma = jwt.encode({"aud": "TiTiler", "iss": "Geocore", "type": "map-access"},
                               key="", algorithm="none")
        with pytest.raises(HTTPException) as e:
            validar(sin_firma)
        assert _estado(e) == 401

    def test_token_basura_devuelve_401_y_no_revienta(self):
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar("esto-no-es-un-jwt")
        assert _estado(e) == 401


# --------------------------------------------------------------------------
#  Ruta del dataset  (OWASP A10 — SSRF)
# --------------------------------------------------------------------------

class TestValidadorDeRuta:

    PREFIJO = "s3://terra-assets/"

    @pytest.fixture
    def validar(self):
        return crear_validador_de_ruta(self.PREFIJO)

    def test_ruta_del_bucket_pasa_sin_modificarse(self, validar):
        url = "s3://terra-assets/ranchos/abc/2026-08-19_ndvi.tif"
        assert validar(url) == url

    @pytest.mark.parametrize("url", [
        "s3://otro-bucket/x.tif",                    # otro bucket
        "https://evil.example/x.tif",                # /vsicurl/ hacia afuera
        "http://minio.railway.internal:9000/x.tif",  # host interno: el SSRF real
        "/vsicurl/https://evil.example/x.tif",       # esquema virtual de GDAL
        "file:///etc/passwd",                        # filesystem local
        "s3://terra-assets-otro/x.tif",              # prefijo parecido, sin la barra
    ])
    def test_rechaza_todo_lo_que_no_sea_el_bucket(self, validar, url):
        with pytest.raises(HTTPException) as e:
            validar(url)
        assert _estado(e) == 400
        assert _codigo(e) == CODIGO_URL_NO_PERMITIDA

    @pytest.mark.parametrize("url", [
        "s3://terra-assets/../otro/x.tif",
        "s3://terra-assets/ranchos/../../x.tif",
    ])
    def test_rechaza_recorrido_de_directorios(self, validar, url):
        with pytest.raises(HTTPException) as e:
            validar(url)
        assert _estado(e) == 400

    def test_el_prefijo_sale_de_la_configuracion(self):
        """Cambiar el bucket cambia lo aceptado, sin tocar el código."""
        validar = crear_validador_de_ruta("s3://otro-bucket/")
        assert validar("s3://otro-bucket/x.tif") == "s3://otro-bucket/x.tif"
        with pytest.raises(HTTPException):
            validar("s3://terra-assets/x.tif")
