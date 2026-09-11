"""El PNG que se devuelve para un tile que cae fuera del raster.

EL BUG QUE ARREGLA
------------------
Hasta el 2026-09-11 este PNG era un literal base64 en `main.py`, con un
comentario que decía "PNG transparente". **No lo era.** Decodificado:

    1x1 px, profundidad 8, tipo de color 4 (gris + alfa)
    bytes del píxel (filtro, gris, alfa): [1, 255, 255]

Gris 255 y alfa 255: **blanco opaco**. Leaflet estira ese píxel a 256×256, así
que cada tile que TiTiler consideraba fuera del raster se dibujaba como un
cuadrado blanco macizo encima del mapa base.

Se notó con los rasters del heatmap on-demand del worker, que están alineados
exactamente a un tile de zoom 14: Leaflet trata como intersecantes a los tiles
vecinos que sólo *tocan* el borde, los pide, TiTiler contesta que están fuera
del raster, y aparecían cuadrados blancos al lado de un raster que casi no se
veía.

Y quedaban pegados: la respuesta sale por `/cog/tiles`, que lleva
`Cache-Control` de un año. El navegador reusaba el tile blanco hasta que
cambiaba la URL, es decir hasta que rotaba el token de mapa.

POR QUÉ SE ARMA CON CÓDIGO Y NO CON UN LITERAL
----------------------------------------------
Un literal base64 es opaco a la lectura: nadie lo decodifica en una revisión,
y el comentario de al lado puede decir cualquier cosa. Armado desde los bytes
del píxel, lo que dice el código es lo que hay en el archivo, y
`tests/test_png.py` lo verifica decodificándolo.

Vive en `terra_tiles/` y no en `main.py` para que ese test corra sin la pila
geoespacial, igual que el resto de la lógica del servicio.
"""

import struct
import zlib

_FIRMA = b"\x89PNG\r\n\x1a\n"


def _chunk(tipo: bytes, datos: bytes) -> bytes:
    crc = zlib.crc32(tipo + datos) & 0xFFFFFFFF
    return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", crc)


def png_transparente() -> bytes:
    """PNG de 1×1 en gris + alfa, con **alfa 0**."""
    # ancho, alto, profundidad 8, tipo de color 4 (gris + alfa),
    # compresión 0, filtro 0, sin entrelazado.
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 4, 0, 0, 0)
    # Una sola fila: byte de filtro 0 (ninguno), gris 0, alfa 0.
    fila = b"\x00" + b"\x00" + b"\x00"
    return (_FIRMA
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(fila))
            + _chunk(b"IEND", b""))


PNG_TRANSPARENTE = png_transparente()
