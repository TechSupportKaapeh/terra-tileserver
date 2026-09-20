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
    CODIGO_TENANT_AJENO,
    CODIGO_TOKEN_SIN_TENANT,
    CODIGO_URL_NO_PERMITIDA,
    crear_validador_de_ruta,
    crear_validador_de_token,
    prefijo_del_tenant,
)

SECRETO = "un-secreto-de-produccion-de-48-caracteres-abcdefgh"
# El que estaba hardcodeado en el repo antes del fix. Se prueba explícitamente
# que ya no abre nada: es una regresión que no queremos volver a introducir.
SECRETO_VIEJO_DEL_REPO = "default_terra_map_token_secret_which_is_at_least_32_chars_long"

# Dos tenants, en la forma canónica que escribe Geocore (`Guid.ToString()`) y que
# el worker usa en la key (`pipeline/claves.py`).
TENANT = "a1b2c3d4-1111-4111-8111-111111111111"
OTRO_TENANT = "b2c3d4e5-2222-4222-8222-222222222222"

_SIN_TENANT = object()  # para distinguir "sin el claim" de "con el claim vacío"


def _token(secreto: str = SECRETO, *, tipo: str = "map-access", aud: str = "TiTiler",
           iss: str = "Geocore", vence_en: dt.timedelta = dt.timedelta(hours=1),
           tenant=TENANT) -> str:
    ahora = dt.datetime.now(dt.timezone.utc)
    carga = {"aud": aud, "iss": iss, "type": tipo, "iat": ahora, "exp": ahora + vence_en}
    if tenant is not _SIN_TENANT:
        carga["tenant_id"] = tenant
    return jwt.encode(carga, secreto, algorithm="HS256")


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

    def test_token_valido_devuelve_el_tenant(self):
        """Devolverlo, y no sólo aprobar, es lo que usa `dataset_path`."""
        validar = crear_validador_de_token(SECRETO)
        assert validar(_token()) == TENANT

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
        assert validar(_token(vence_en=dt.timedelta(seconds=-10))) == TENANT

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

    def test_un_token_sin_tenant_no_abre_nada(self):
        """Los emitidos antes de M.8.1. Antes servían **todo** el bucket.

        No se toleran "por compatibilidad": tolerarlos sería dejar abierto justo
        lo que esta tarea cierra. La ventana dura lo que dura un token —una
        hora— y por eso Geocore se despliega antes que este servicio.
        """
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(_token(tenant=_SIN_TENANT))
        assert _estado(e) == 403
        assert _codigo(e) == CODIGO_TOKEN_SIN_TENANT

    @pytest.mark.parametrize("tenant", [
        "",                                        # vacío
        None,                                      # nulo
        12345,                                     # no es texto
        "11111111111111111111111111111111",        # sin guiones
        "11111111-1111-1111-1111-11111111111G",    # no es hex
        TENANT.upper(),                            # mayúsculas: otra key
        f"{TENANT}/otro",                          # un id que arma otra ruta
        "../..",
    ])
    def test_un_tenant_con_otra_forma_no_abre_nada(self, tenant):
        """La key lleva el uuid canónico; cualquier otra cosa no puede coincidir.

        El claim viene firmado, así que esto no defiende de un atacante: defiende
        de un cambio del otro lado del contrato, y lo dice en vez de dar 403 en
        todo sin explicación.
        """
        validar = crear_validador_de_token(SECRETO)
        with pytest.raises(HTTPException) as e:
            validar(_token(tenant=tenant))
        assert _estado(e) == 403
        assert _codigo(e) == CODIGO_TOKEN_SIN_TENANT

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
#  Ruta del dataset  (OWASP A10 — SSRF,  y A01 — el tenant, M.8.1)
# --------------------------------------------------------------------------

class TestPrefijoDelTenant:

    def test_es_la_misma_forma_que_arma_el_worker(self):
        assert prefijo_del_tenant("s3://terra-assets/", TENANT) ==             f"s3://terra-assets/tenants/{TENANT}/"

    def test_termina_en_barra(self):
        """Sin la barra, el prefijo de un tenant abre los ids que empiezan igual."""
        assert prefijo_del_tenant("s3://b/", TENANT).endswith("/")


class TestValidadorDeRuta:

    PREFIJO = "s3://terra-assets/"

    @pytest.fixture
    def validar(self):
        """El validador, ya resuelto: se le pasa la url y el tenant del token.

        En la app el tenant llega por `Depends` del validador de token; acá se
        pasa a mano, que es lo mismo que hace FastAPI.
        """
        crudo = crear_validador_de_ruta(self.PREFIJO, lambda: TENANT)
        return lambda url, tenant=TENANT: crudo(url, tenant)

    def test_un_cog_del_propio_tenant_pasa_sin_modificarse(self, validar):
        url = f"{self.PREFIJO}tenants/{TENANT}/ranchos/abc/s2-mensual-v1/ndvi/2026-08.tif"
        assert validar(url) == url

    def test_un_cog_de_otro_tenant_da_403(self, validar):
        """El corazón de M.8.1. La key viaja a la vista en la URL del tile."""
        url = f"{self.PREFIJO}tenants/{OTRO_TENANT}/ranchos/abc/s2-mensual-v1/ndvi/2026-08.tif"
        with pytest.raises(HTTPException) as e:
            validar(url)
        assert _estado(e) == 403
        assert _codigo(e) == CODIGO_TENANT_AJENO

    def test_el_mensaje_del_403_no_nombra_al_otro_tenant(self, validar):
        url = f"{self.PREFIJO}tenants/{OTRO_TENANT}/ranchos/abc/s2-mensual-v1/ndvi/2026-08.tif"
        with pytest.raises(HTTPException) as e:
            validar(url)
        assert OTRO_TENANT not in str(e.value.detail)

    @pytest.mark.parametrize("url", [
        # Las keys anteriores al pipeline mensual: sin tenant en la ruta. Ningún
        # token las alcanza, y por eso se borran (Geocore `DECISIONS #43`).
        "parcelas/abc/2026-08-19_ndvi.tif",
        "ranchos/abc/2026-08-19_ndvi.tif",
        # Y cualquier otra cosa dentro del bucket que no cuelgue de un tenant.
        "tenants/",
        "mosaicos/abc.json",
    ])
    def test_lo_que_no_cuelga_de_un_tenant_da_403(self, validar, url):
        with pytest.raises(HTTPException) as e:
            validar(self.PREFIJO + url)
        assert _estado(e) == 403
        assert _codigo(e) == CODIGO_TENANT_AJENO

    def test_un_id_que_empieza_igual_no_alcanza(self, validar):
        """Por esto la comparación lleva la barra final."""
        casi = f"{TENANT}-bis"
        with pytest.raises(HTTPException) as e:
            validar(f"{self.PREFIJO}tenants/{casi}/ranchos/a/r/ndvi/2026-08.tif")
        assert _estado(e) == 403

    def test_cada_token_abre_lo_suyo_y_nada_mas(self, validar):
        """El mismo validador, dos tenants: cada uno sólo su prefijo."""
        mio = f"{self.PREFIJO}tenants/{TENANT}/r.tif"
        ajeno = f"{self.PREFIJO}tenants/{OTRO_TENANT}/r.tif"
        assert validar(mio, TENANT) == mio
        assert validar(ajeno, OTRO_TENANT) == ajeno
        for url, tenant in ((mio, OTRO_TENANT), (ajeno, TENANT)):
            with pytest.raises(HTTPException) as e:
                validar(url, tenant)
            assert _estado(e) == 403

    @pytest.mark.parametrize("url", [
        "s3://otro-bucket/x.tif",                    # otro bucket
        "https://evil.example/x.tif",                # /vsicurl/ hacia afuera
        "http://minio.railway.internal:9000/x.tif",  # host interno: el SSRF real
        "/vsicurl/https://evil.example/x.tif",       # esquema virtual de GDAL
        "file:///etc/passwd",                        # filesystem local
        "s3://terra-assets-otro/x.tif",              # prefijo parecido, sin la barra
    ])
    def test_rechaza_todo_lo_que_no_sea_el_bucket(self, validar, url):
        """Sigue siendo 400 y no 403: esa URL no la puede pedir nadie."""
        with pytest.raises(HTTPException) as e:
            validar(url)
        assert _estado(e) == 400
        assert _codigo(e) == CODIGO_URL_NO_PERMITIDA

    @pytest.mark.parametrize("url", [
        "s3://terra-assets/../otro/x.tif",
        "s3://terra-assets/ranchos/../../x.tif",
        # El que importa desde M.8.1: sale del propio prefijo y entra al ajeno.
        f"s3://terra-assets/tenants/{TENANT}/../{OTRO_TENANT}/r.tif",
    ])
    def test_rechaza_recorrido_de_directorios(self, validar, url):
        """El `..` se mira ANTES que el tenant, o el último caso pasaría."""
        with pytest.raises(HTTPException) as e:
            validar(url)
        assert _estado(e) == 400

    def test_el_prefijo_sale_de_la_configuracion(self):
        """Cambiar el bucket cambia lo aceptado, sin tocar el código."""
        crudo = crear_validador_de_ruta("s3://otro-bucket/", lambda: TENANT)
        propio = f"s3://otro-bucket/tenants/{TENANT}/x.tif"
        assert crudo(propio, TENANT) == propio
        with pytest.raises(HTTPException):
            crudo(f"s3://terra-assets/tenants/{TENANT}/x.tif", TENANT)
