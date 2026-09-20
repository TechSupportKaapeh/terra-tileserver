"""La parte con lógica de `scripts/check_prod.py`.

El script es de diagnóstico y se corre a mano contra un deploy, así que casi
todo él es red y no se puede probar acá. `_key_de_otro_tenant` sí: es texto, y
es de lo que depende el escalón que verifica el aislamiento de M.8.1. Si
devolviera la misma key, el escalón daría 200 y se leería como que el control
falla; si devolviera una ruta inválida, daría 400 y se leería como que funciona.

Se carga por ruta porque `scripts/` no es un paquete y el script no se instala.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

RUTA = Path(__file__).resolve().parent.parent / "scripts" / "check_prod.py"
_spec = importlib.util.spec_from_file_location("check_prod", RUTA)
check_prod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_prod)

TENANT = "a1b2c3d4-1111-4111-8111-111111111111"
KEY = (f"s3://terra-assets/tenants/{TENANT}/ranchos/"
       "3f1c9a7e-0000-4000-8000-000000000001/s2-mensual-v1/ndvi/2026-08.tif")


def test_cambia_el_tenant_y_deja_el_resto_igual():
    ajena = check_prod._key_de_otro_tenant(KEY)

    assert TENANT not in ajena
    assert ajena.startswith("s3://terra-assets/tenants/")
    # Todo lo que va después del tenant se conserva: lo único que cambia entre
    # las dos keys es de quién es.
    assert ajena.endswith(KEY.split(f"{TENANT}/", 1)[1])
    assert ajena != KEY


def test_una_key_sin_tenant_no_se_puede_transformar():
    """Las viejas. El escalón avisa en vez de inventar una prueba."""
    assert check_prod._key_de_otro_tenant("s3://terra-assets/parcelas/abc/x.tif") is None


def test_una_key_cortada_en_el_tenant_tampoco():
    assert check_prod._key_de_otro_tenant(f"s3://terra-assets/tenants/{TENANT}") is None
