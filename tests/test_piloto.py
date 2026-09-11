"""Reglas del piloto del front (`public/piloto.html`).

No se prueba el comportamiento en un navegador —el repo no tiene esa
infraestructura—, sino las reglas que hacen seguro al piloto y que un cambio
chico podría romper sin que nadie lo note:

- no guarda credenciales en `localStorage`;
- no inserta datos como HTML (el token, la ruta del COG y los cuerpos de error
  son datos que vienen de afuera);
- no trae un token de verdad pegado en el código;
- no le pasa `nodata` a TiTiler: el COG del worker trae máscara interna y
  NDVI = 0 es un valor real.
"""

import re
from pathlib import Path

PILOTO = Path(__file__).resolve().parent.parent / "public" / "piloto.html"


def _html() -> str:
    return PILOTO.read_text(encoding="utf-8")


def test_el_piloto_existe():
    assert PILOTO.is_file()


def test_no_persiste_credenciales():
    # La lista de lo que se guarda en localStorage tiene que quedar sin
    # secretos. Un token en localStorage es lo primero que se lleva un XSS, y
    # el de Supabase no vence en una hora.
    m = re.search(r"var GUARDAR = \[([^\]]*)\]", _html())
    assert m, "no se encontró la lista GUARDAR"
    guardado = m.group(1)
    for prohibido in ("token", "supabase"):
        assert f'"{prohibido}"' not in guardado


def test_no_usa_innerhtml():
    # Todo lo dinámico entra con textContent. Si alguien agrega un innerHTML
    # para "formatear un mensaje", el cuerpo de un error de TiTiler pasa a ser
    # HTML ejecutable en esta página.
    assert "innerHTML" not in _html()


def test_no_trae_un_jwt_real():
    # Un JWT completo tiene tres segmentos base64url separados por puntos. Los
    # placeholders del piloto son un prefijo cortado, sin puntos.
    assert not re.search(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}", _html())


def test_no_pasa_nodata_a_titiler():
    # En el armado de la URL no puede aparecer `nodata` como parámetro. El
    # texto explicativo sí lo menciona, por eso se busca la forma de clave.
    assert "nodata:" not in _html()


def test_usa_la_misma_version_de_leaflet_que_terra_admin():
    # terra-admin usa leaflet ^1.9.4 con react-leaflet 5. El piloto tiene que
    # comportarse como el front, no como otra versión.
    assert "leaflet/1.9.4/leaflet.js" in _html()
