"""Verifica que MosaicJSON componga por mediana y respete el nodata.

Nació como el spike de la FASE B (`DECISIONS #19` y #20) y se conserva porque la
respuesta depende de las versiones de `rio-tiler` y `cogeo-mosaic`, que entran
sin pinnear. Volver a correrlo después de tocar `requirements.txt`.

No necesita red ni MinIO: genera los COG en un directorio temporal y compone
desde disco. Correr con el venv del proyecto:

    python scripts/check_mosaic_median.py

Responde una sola pregunta: ¿`rio-tiler` compone varias pasadas por mediana y
respeta el nodata? Si la respuesta es no, `DECISIONS #19` y #20 hay que
reconsiderarlas.

Los datos son sintéticos y deliberados. Cuatro cuadrantes con valores constantes
por fecha, para que la mediana esperada se calcule a mano:

                    fecha1   fecha2   fecha3      mediana esperada
  NO  las tres        0.2      0.5      0.8            0.50
  NE  solo la 3ª      --       --       0.8            0.80   <- el caso de B.5
  SO  1ª y 3ª         0.2      --       0.8            0.50
  SE  ninguna         --       --       --             NODATA <- control negativo

El SE es lo que hace que la prueba sirva: si todo saliera con dato, no probaría
nada. Y el NE es exactamente el escenario que importa — una zona nublada en una
pasada y despejada en otra tiene que salir con el dato, no con un agujero.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_origin

AQUI = Path(__file__).resolve().parent
DATOS = AQUI.parent / "outputs" / "mosaico_prueba"
DATOS.mkdir(parents=True, exist_ok=True)

# Misma grilla para las tres: mismo origen, mismo tamaño de píxel, mismo CRS.
# `DECISIONS #19` avisa que si esto se escapa, las pasadas no se apilan y el bug
# aparece recién al componer.
TAM = 512
RES = 0.0002
ORIGEN = (-107.45, 24.85)

FECHAS = ["2026-06-10", "2026-06-20", "2026-06-30"]
VALORES = [0.2, 0.5, 0.8]

# Qué cuadrantes tienen dato en cada fecha. (fila, columna) sobre la mitad.
#   NO = (0,0)   NE = (0,1)
#   SO = (1,0)   SE = (1,1)
CON_DATO = {
    0: [(0, 0), (1, 0)],          # fecha1: NO y SO
    1: [(0, 0)],                  # fecha2: solo NO
    2: [(0, 0), (0, 1), (1, 0)],  # fecha3: NO, NE y SO
}

ESPERADO = {
    "NO (las tres pasadas)":  0.50,
    "NE (solo la 3a)":        0.80,
    "SO (1a y 3a)":           0.50,
    "SE (ninguna)":           None,   # nodata
}


def escribir_cog(indice: int) -> Path:
    """Un COG por pasada, con nodata=nan en los cuadrantes sin dato."""
    datos = np.full((TAM, TAM), np.nan, dtype="float32")
    mitad = TAM // 2
    for fila, col in CON_DATO[indice]:
        datos[fila * mitad:(fila + 1) * mitad, col * mitad:(col + 1) * mitad] = VALORES[indice]

    perfil = dict(
        driver="GTiff", height=TAM, width=TAM, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(*ORIGEN, RES, RES),
        nodata=float("nan"), tiled=True, blockxsize=256, blockysize=256,
        compress="deflate",
    )
    destino = DATOS / f"{FECHAS[indice]}_ndvi.tif"

    with rasterio.io.MemoryFile() as mem:
        with mem.open(**perfil) as ds:
            ds.write(datos, 1)
            ds.build_overviews([2, 4], Resampling.average)
        with mem.open() as ds:
            rio_copy(ds, destino, copy_src_overviews=True, **perfil)
    return destino


def main() -> int:
    print("== 1. Tres pasadas sobre la misma grilla ==")
    rutas = [escribir_cog(i) for i in range(3)]
    for r, v in zip(rutas, VALORES):
        print(f"   {r.name}  valor={v}")

    print("\n== 2. MosaicJSON ==")
    from cogeo_mosaic.mosaic import MosaicJSON

    # `from_urls` abre cada COG para sacar bounds y armar el índice de quadkeys.
    mosaico = MosaicJSON.from_urls([str(r) for r in rutas], minzoom=10, maxzoom=16)
    ruta_mosaico = DATOS / "mosaico.json"
    ruta_mosaico.write_text(mosaico.model_dump_json(indent=2), encoding="utf-8")
    print(f"   bounds : {[round(b, 4) for b in mosaico.bounds]}")
    print(f"   zooms  : {mosaico.minzoom}-{mosaico.maxzoom}")
    print(f"   tiles  : {len(mosaico.tiles)} quadkey(s), "
          f"{sum(len(v) for v in mosaico.tiles.values())} referencias")

    print("\n== 3. Un tile compuesto por MEDIANA ==")
    from cogeo_mosaic.backends import MosaicBackend
    from rio_tiler.mosaic.methods import PixelSelectionMethod

    # El zoom NO es arbitrario: hay que elegir uno donde el área entre entera en
    # un solo tile, o los cuadrantes quedan repartidos entre tiles vecinos.
    # A z=13 un tile mide 0.044° de longitud y el área 0.102°: no entra.
    import math
    oeste, sur, este, norte = mosaico.bounds
    lon = (oeste + este) / 2
    lat = (sur + norte) / 2

    ancho_grados = este - oeste
    z = max(z for z in range(1, 20) if 360.0 / 2 ** z > ancho_grados * 2)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    print(f"   área de {ancho_grados:.4f}° -> z={z} (tile de {360.0 / n:.4f}°)")
    print(f"   tile z={z} x={x} y={y}")

    # Ruta RELATIVA a propósito: `MosaicBackend` despacha por `urlparse`, y en
    # Windows una ruta absoluta como C:\... se lee con esquema "c", que no
    # existe. Sin esquema cae al FileBackend, que es lo que queremos.
    # (`file:///C:/...` tampoco sirve: deja el path como /C:/... y no abre.)
    import os
    os.chdir(DATOS)

    with MosaicBackend(ruta_mosaico.name) as backend:
        img, assets = backend.tile(x, y, z, pixel_selection=PixelSelectionMethod.median.value())

    print(f"   assets usados: {len(assets)}")
    for a in assets:
        print(f"      {Path(a).name}")

    print("\n== 4. Verificación por cuadrante ==")
    import numpy as np
    from rasterio.warp import transform as reproyectar

    datos = np.asarray(img.data[0])
    mascara = np.asarray(img.mask)

    # Los cuadrantes se ubican por COORDENADAS y no por fracción de la imagen.
    # Una versión anterior muestreaba a un cuarto y tres cuartos del tile, pero
    # el área ocupa solo una parte de él: los puntos caían fuera del dato y todo
    # salía NODATA. La prueba decía "falla la arquitectura" cuando fallaba ella.
    cuartos = {
        "NO (las tres pasadas)": (oeste + ancho_grados * 0.25, norte - (norte - sur) * 0.25),
        "NE (solo la 3a)":       (oeste + ancho_grados * 0.75, norte - (norte - sur) * 0.25),
        "SO (1a y 3a)":          (oeste + ancho_grados * 0.25, norte - (norte - sur) * 0.75),
        "SE (ninguna)":          (oeste + ancho_grados * 0.75, norte - (norte - sur) * 0.75),
    }

    izq, abajo, der, arriba = img.bounds
    alto_px, ancho_px = datos.shape

    def a_pixel(lon_p: float, lat_p: float) -> tuple[int, int]:
        xs, ys = reproyectar("EPSG:4326", img.crs, [lon_p], [lat_p])
        col = int((xs[0] - izq) / (der - izq) * ancho_px)
        fila = int((arriba - ys[0]) / (arriba - abajo) * alto_px)
        return fila, col

    fallos = []
    for nombre, (lon_p, lat_p) in cuartos.items():
        f, c = a_pixel(lon_p, lat_p)

        # Si el punto cae fuera del tile, se dice — no se muestrea otra cosa en
        # silencio, que es exactamente el error que tuvo la versión anterior.
        if not (0 <= f < alto_px and 0 <= c < ancho_px):
            print(f"   FALLO {nombre:<24} el punto cae FUERA del tile (fila={f}, col={c})")
            fallos.append(nombre)
            continue

        valido = bool(mascara[f, c])
        valor = float(datos[f, c]) if valido else None
        esperado = ESPERADO[nombre]

        if esperado is None:
            ok = not valido
            got = "NODATA" if not valido else f"{valor:.3f}"
            exp = "NODATA"
        else:
            ok = valido and abs(valor - esperado) < 1e-4
            got = f"{valor:.3f}" if valido else "NODATA"
            exp = f"{esperado:.3f}"

        print(f"   {'OK  ' if ok else 'FALLO'} {nombre:<24} esperado {exp:<8} obtuvo {got}")
        if not ok:
            fallos.append(nombre)

    print("\n" + "=" * 62)
    if fallos:
        print(f"El spike FALLA en: {', '.join(fallos)}")
        print("Reconsiderar DECISIONS #19 y #20.")
        return 1
    print("MosaicJSON compone por mediana y respeta el nodata.")
    print("DECISIONS #19 y #20 se sostienen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
