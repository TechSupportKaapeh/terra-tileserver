"""El PNG de "fuera del raster" tiene que ser transparente de verdad.

Parece obvio, y justamente por eso estuvo mal: el literal base64 anterior se
comentó como transparente y era blanco opaco. Estos tests lo decodifican a
mano —sin Pillow, que no es dependencia del servicio— y miran el byte de alfa.
"""

import base64
import struct
import zlib

from terra_tiles.png import PNG_TRANSPARENTE

# El literal que había en main.py hasta el 2026-09-11. Se deja acá para que el
# test documente por qué existe.
_LITERAL_VIEJO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7l3l8AAAAASUVORK5CYII="
)


def _chunks(png: bytes, verificar_crc: bool = True):
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "no es un PNG"
    i = 8
    while i < len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        tipo, datos = png[i + 4:i + 8], png[i + 8:i + 8 + n]
        crc = struct.unpack(">I", png[i + 8 + n:i + 12 + n])[0]
        if verificar_crc:
            assert crc == zlib.crc32(tipo + datos) & 0xFFFFFFFF, f"CRC mal en {tipo}"
        yield tipo, datos, crc
        i += 12 + n


def _pixel_gris_alfa(png: bytes, verificar_crc: bool = True):
    """Devuelve (gris, alfa) del único píxel de un PNG 1×1 gris+alfa."""
    chunks = {t: d for t, d, _ in _chunks(png, verificar_crc)}
    ancho, alto, prof, tipo_color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (ancho, alto, prof, tipo_color) == (1, 1, 8, 4)
    filtro, gris, alfa = zlib.decompress(chunks[b"IDAT"])
    # Con un solo píxel, los filtros 0 (None) y 1 (Sub) dejan los bytes como
    # están: no hay píxel a la izquierda que sumar.
    assert filtro in (0, 1)
    return gris, alfa


def test_el_png_es_un_png_valido_con_crc_correctos():
    tipos = [t for t, _, _ in _chunks(PNG_TRANSPARENTE)]
    assert tipos == [b"IHDR", b"IDAT", b"IEND"]


def test_el_png_es_transparente():
    _, alfa = _pixel_gris_alfa(PNG_TRANSPARENTE)
    assert alfa == 0


def test_el_literal_viejo_era_blanco_opaco():
    # La razón de este archivo. Si alguien vuelve a pegar un base64 "porque es
    # más corto", este test muestra qué había detrás del último. El CRC no se
    # verifica acá porque también estaba roto: ver el test de abajo.
    assert _pixel_gris_alfa(_LITERAL_VIEJO, verificar_crc=False) == (255, 255)


def test_el_literal_viejo_ademas_tenia_el_crc_roto():
    # Lo encontró el test de arriba la primera vez que corrió, cuando todavía
    # verificaba el CRC: el bloque IDAT del literal guarda un CRC que no es el
    # de sus datos. Los navegadores lo dibujaban igual, y por eso nunca se
    # notó. Otra prueba de que nadie lo había decodificado.
    rotos = [tipo for tipo, datos, crc in _chunks(_LITERAL_VIEJO, verificar_crc=False)
             if crc != zlib.crc32(tipo + datos) & 0xFFFFFFFF]
    assert rotos == [b"IDAT"]
