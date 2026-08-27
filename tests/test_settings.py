"""Tests de la lectura de configuración."""
from __future__ import annotations

import pytest

from terra_tiles.settings import Settings, _sin_esquema, configure_gdal


class TestSinEsquema:
    """GDAL espera `host:puerto` en AWS_S3_ENDPOINT, sin protocolo."""

    @pytest.mark.parametrize("entrada,esperado", [
        ("localhost:9000", "localhost:9000"),
        ("http://minio:9000", "minio:9000"),
        ("https://bucket-x.up.railway.app", "bucket-x.up.railway.app"),
        ("https://bucket-x.up.railway.app/", "bucket-x.up.railway.app"),
    ])
    def test_normaliza_el_endpoint(self, entrada, esperado):
        assert _sin_esquema(entrada) == esperado


class TestSettings:

    def test_secreto_ausente_queda_en_none(self, monkeypatch):
        """`None` es lo que dispara el 503; un string vacío tiene que dar None."""
        monkeypatch.delenv("MAP_TOKEN_SECRET", raising=False)
        assert Settings.from_env().map_token_secret is None

    def test_secreto_vacio_queda_en_none(self, monkeypatch):
        monkeypatch.setenv("MAP_TOKEN_SECRET", "")
        assert Settings.from_env().map_token_secret is None

    def test_el_prefijo_se_deriva_del_bucket(self, monkeypatch):
        monkeypatch.setenv("MINIO_BUCKET", "otro-bucket")
        assert Settings.from_env().prefijo_valido == "s3://otro-bucket/"

    def test_cors_se_parte_y_se_limpia(self, monkeypatch):
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.com, https://b.com ,")
        assert Settings.from_env().cors_origins == ["https://a.com", "https://b.com"]

    @pytest.mark.parametrize("valor,esperado", [
        ("True", True), ("true", True), ("1", True), ("yes", True),
        ("False", False), ("false", False), ("", False), ("no", False),
    ])
    def test_minio_secure_acepta_las_formas_habituales(self, monkeypatch, valor, esperado):
        monkeypatch.setenv("MINIO_SECURE", valor)
        assert Settings.from_env().minio_secure is esperado

    def test_es_inmutable(self, monkeypatch):
        """Cambiar config en caliente haría el servicio imposible de razonar."""
        s = Settings.from_env()
        with pytest.raises(Exception):
            s.minio_bucket = "otro"


class TestConfigureGdal:

    def test_traduce_minio_secure_a_aws_https(self, monkeypatch):
        monkeypatch.setenv("MINIO_SECURE", "true")
        configure_gdal(Settings.from_env())
        import os
        assert os.environ["AWS_HTTPS"] == "YES"

    def test_usa_direccionamiento_por_ruta(self, monkeypatch):
        """MinIO usa `host/bucket/key`, no subdominios como S3 de AWS."""
        configure_gdal(Settings.from_env())
        import os
        assert os.environ["AWS_S3_ADDRESSING_STYLE"] == "path"
        assert os.environ["AWS_VIRTUAL_HOSTING"] == "FALSE"

    def test_configura_tambien_boto3_con_el_endpoint_de_minio(self, monkeypatch):
        """boto3 no comparte NINGUNA variable con GDAL.

        Lo usa cogeo-mosaic para leer un MosaicJSON del bucket (`S3Backend` crea
        su cliente sin `endpoint_url`). Sin esto apuntaría a AWS de verdad, y el
        fallo sería un timeout contra un bucket ajeno, no un error de config.
        """
        monkeypatch.setenv("MINIO_ENDPOINT", "minio.railway.internal:9000")
        monkeypatch.setenv("MINIO_SECURE", "false")
        configure_gdal(Settings.from_env())
        import os
        assert os.environ["AWS_ENDPOINT_URL_S3"] == "http://minio.railway.internal:9000"
        # GDAL lo quiere pelado, boto3 con esquema: son formatos distintos.
        assert os.environ["AWS_S3_ENDPOINT"] == "minio.railway.internal:9000"

    def test_el_endpoint_de_boto3_respeta_minio_secure(self, monkeypatch):
        monkeypatch.setenv("MINIO_ENDPOINT", "bucket-x.up.railway.app")
        monkeypatch.setenv("MINIO_SECURE", "true")
        configure_gdal(Settings.from_env())
        import os
        assert os.environ["AWS_ENDPOINT_URL_S3"] == "https://bucket-x.up.railway.app"
