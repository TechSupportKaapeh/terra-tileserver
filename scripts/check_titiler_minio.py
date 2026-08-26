"""
scripts/check_titiler_minio.py

Validación STANDALONE del lado de TiTiler:
  1. Confirma que las variables AWS_*/GDAL quedaron bien fijadas a partir
     del .env (mismo bloque que main.py corre antes de importar TiTiler).
  2. Sube un GeoTIFF mínimo de prueba usando minio-py (para tener algo
     real que leer) -- usa temporalmente el cliente, solo para esta prueba.
  3. Intenta leer ese archivo con rasterio a través de la ruta /vsis3/,
     que es exactamente el mecanismo que usa TiTiler internamente.

Si el paso 3 funciona, TiTiler podrá servir tiles de ese archivo sin
problema, porque usa la misma vía de lectura (GDAL VSIS3).

Ejecutar desde tileserver-titiler/ con el venv activado:
    python scripts/check_titiler_minio.py
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

# Se usa EXACTAMENTE la misma configuración que el servidor, no una copia.
# Este bloque estaba duplicado acá y arrastraba un bug ya corregido en
# `settings.py`: no quitaba la barra final del endpoint. Un script de
# diagnóstico configurado distinto que el servicio no diagnostica nada —
# puede pasar cuando el servidor falla, o al revés.
from terra_tiles.settings import Settings, configure_gdal

_settings = Settings.from_env()
configure_gdal(_settings)


def main() -> int:
    print("== Verificación de lectura TiTiler -> MinIO (tiler-ro) ==\n")

    bucket = os.getenv("MINIO_BUCKET", "terra-assets")
    access_key = os.getenv("MINIO_ACCESS_KEY")
    secret_key = os.getenv("MINIO_SECRET_KEY")

    if not access_key or not secret_key:
        print("[FALLO] Faltan MINIO_ACCESS_KEY / MINIO_SECRET_KEY en tileserver-titiler/.env")
        return 1

    print(f"[OK] Variables GDAL/VSIS3 fijadas:")
    print(f"     AWS_S3_ENDPOINT = {os.environ['AWS_S3_ENDPOINT']}")
    print(f"     AWS_HTTPS       = {os.environ['AWS_HTTPS']}")
    print(f"     bucket          = {bucket}")

    # --- Paso 1: subir un GeoTIFF mínimo de prueba usando minio-py ---
    # (usamos minio-py SOLO para preparar el dato de prueba; el usuario
    #  tiler-ro normalmente no podría hacer esto, así que probamos con
    #  cualquier credencial de escritura disponible en el entorno si existe,
    #  o saltamos este paso si no la hay y asumimos que el archivo ya existe)
    test_key = "_healthcheck/titiler_test.tif"

    try:
        import rasterio
        from rasterio.transform import from_origin
        import numpy as np
    except ImportError as e:
        print(f"[FALLO] No se pudo importar rasterio/numpy: {e}")
        return 1

    # Generar un GeoTIFF mínimo de 10x10 píxeles en memoria
    print("\n[INFO] Generando GeoTIFF mínimo de prueba en memoria...")
    data = np.ones((1, 10, 10), dtype="uint8")
    transform = from_origin(-75.0, 10.0, 0.001, 0.001)  # cerca de Cartagena, solo de ejemplo

    with rasterio.io.MemoryFile() as memfile:
        with memfile.open(
            driver="GTiff",
            height=10, width=10, count=1,
            dtype="uint8", crs="EPSG:4326", transform=transform,
        ) as dataset:
            dataset.write(data)
        tif_bytes = memfile.read()

    # Subir usando minio-py. Por defecto usamos las credenciales del .env
    # actual (tiler-ro), lo cual fallará con AccessDenied -- eso es CORRECTO,
    # confirma que tiler-ro no puede escribir.
    #
    # Para tener un archivo real que leer, puedes exportar credenciales de
    # escritura SOLO para esta subida de prueba, sin tocar tu .env:
    #   export UPLOAD_ACCESS_KEY=worker-rw
    #   export UPLOAD_SECRET_KEY=<la clave real de worker-rw>
    #   python scripts/check_titiler_minio.py
    
    upload_access_key = os.getenv("UPLOAD_ACCESS_KEY", access_key)
    upload_secret_key = os.getenv("UPLOAD_SECRET_KEY", secret_key)

    try:
        from minio import Minio
        upload_client = Minio(
            os.environ["AWS_S3_ENDPOINT"],
            access_key=upload_access_key,
            secret_key=upload_secret_key,
            secure=(os.environ["AWS_HTTPS"] == "YES"),
        )
        upload_client.put_object(bucket, test_key, io.BytesIO(tif_bytes), length=len(tif_bytes))
        print(f"[OK] GeoTIFF de prueba subido a s3://{bucket}/{test_key} "
              f"(con credenciales '{upload_access_key}')")
        uploaded_by_this_script = True
    except Exception as e:
        print(f"[INFO] No se pudo subir: {e}")
        if upload_access_key == access_key:
            print("[INFO] Exporta UPLOAD_ACCESS_KEY/UPLOAD_SECRET_KEY con las de worker-rw.")
        uploaded_by_this_script = False

    # --- Paso 2: leer ese archivo vía /vsis3/, el mismo mecanismo que usa TiTiler ---
    vsis3_path = f"/vsis3/{bucket}/{test_key}"
    print(f"\n[INFO] Intentando leer vía GDAL VSIS3: {vsis3_path}")
    try:
        with rasterio.open(vsis3_path) as src:
            print(f"[OK] Lectura exitosa vía VSIS3.")
            print(f"     bounds = {src.bounds}")
            print(f"     crs    = {src.crs}")
            print(f"     shape  = {src.shape}")
    except Exception as e:
        print(f"[FALLO] No se pudo leer vía VSIS3: {e}")
        print("        Revisa: AWS_S3_ENDPOINT, AWS_S3_ADDRESSING_STYLE=path, "
              "credenciales tiler-ro con permiso de lectura sobre el bucket.")
        return 1

    # --- Cleanup si lo subimos nosotros ---
    if uploaded_by_this_script:
        try:
            upload_client.remove_object(bucket, test_key)
            print("\n[OK] Limpieza exitosa (archivo de prueba eliminado).")
        except Exception as e:
            print(f"[ADVERTENCIA] No se pudo limpiar el archivo de prueba: {e}")

    print("\n✅ TiTiler puede leer correctamente desde MinIO vía VSIS3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
