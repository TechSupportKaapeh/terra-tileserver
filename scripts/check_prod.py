"""Verifica un tileserver YA DESPLEGADO, de afuera hacia adentro.

Cada paso agrega exactamente un eslabón a la cadena, así que el primero que
falle señala la causa sin ambigüedad. Ese es todo el diseño: `/health` en 200
no prueba nada sobre el token, y un tile en 200 prueba las seis cosas de una
vez pero no dice cuál se rompió cuando da error.

    escalón 1  /health              el proceso vive
    escalón 2  /health/ready        config + MinIO alcanzable
    escalón 3  /cog/info sin token  el control de acceso está activo
    escalón 4  /cog/info con token  el token vale Y GDAL abre el COG por vsis3
    escalón 5  un tile              rio-tiler renderiza y devuelve PNG
    escalón 6  Cache-Control        el header de caché está puesto
    escalón 7  tile fuera de borde  PNG transparente, no un error

Uso:

    python scripts/check_prod.py \\
        --base  https://<titiler>.up.railway.app \\
        --token "$TOKEN" \\
        --key   ranchos/<id>/pasadas/<fecha>/ndvi.tif

El token sale de `GET /api/maps/token` de Geocore y dura 1 h. No se imprime
nunca: los mensajes de error de este script se pegan en chats y tickets.

Solo usa la librería estándar, así que corre con cualquier Python 3.9+ sin
instalar nada — incluido el equipo que no tenga el proyecto clonado.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_fallos: list[str] = []
_avisos: list[str] = []


# --------------------------------------------------------------------------
# Salida
# --------------------------------------------------------------------------

def ok(paso: str, detalle: str = "") -> None:
    print(f"  [OK]    {paso}" + (f" — {detalle}" if detalle else ""))


def fallo(paso: str, detalle: str) -> None:
    print(f"  [FALLO] {paso} — {detalle}")
    _fallos.append(f"{paso}: {detalle}")


def aviso(paso: str, detalle: str) -> None:
    print(f"  [AVISO] {paso} — {detalle}")
    _avisos.append(f"{paso}: {detalle}")


def titulo(t: str) -> None:
    print(f"\n{t}")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def pedir(url: str) -> tuple[int, bytes, dict]:
    """GET que devuelve (status, cuerpo, headers) sin lanzar en 4xx/5xx.

    Los códigos de error son justamente el dato que buscamos, así que tratarlos
    como excepción obligaría a envolver cada llamada.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "terra-check-prod"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except urllib.error.URLError as e:
        raise SystemExit(f"\nNo se pudo alcanzar {_sin_query(url)}: {e.reason}\n"
                         f"Revisá --base (con https://) y que el servicio esté desplegado.")


def _sin_query(url: str) -> str:
    """La URL sin query string: el token viaja ahí y no debe imprimirse."""
    return url.split("?", 1)[0]


def _query(base: str, ruta: str, **params) -> str:
    return f"{base}{ruta}?{urllib.parse.urlencode(params)}"


def _json(cuerpo: bytes):
    try:
        return json.loads(cuerpo)
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# Escalones
# --------------------------------------------------------------------------

def escalon_health(base: str) -> None:
    titulo("1. Liveness")
    status, cuerpo, _ = pedir(f"{base}/health")
    if status == 200:
        ok("/health", "200")
    else:
        fallo("/health", f"esperaba 200, dio {status}. El servicio no está sirviendo.")


def escalon_ready(base: str) -> None:
    titulo("2. Readiness (config + MinIO)")
    status, cuerpo, _ = pedir(f"{base}/health/ready")
    datos = _json(cuerpo)

    if datos is None:
        # Un 404 acá es la pista de que la imagen es vieja, no de que algo falle.
        if status == 404:
            aviso("/health/ready", "404: el servicio corre una imagen anterior a este endpoint. "
                                   "Redesplegá para tener el diagnóstico detallado.")
        else:
            fallo("/health/ready", f"respuesta no-JSON con status {status}")
        return

    print(f"          bucket: {datos.get('bucket')}")
    for chk in datos.get("checks", []):
        linea = f"{chk['name']} = {chk['status']}"
        if chk.get("cause"):
            linea += f" ({chk['cause']})"
        if chk["status"] == "ok":
            ok(linea)
        elif chk["status"] == "degradado":
            aviso(linea, chk["detail"])
        else:
            fallo(linea, chk["detail"])


def escalon_sin_token(base: str, url_cog: str) -> None:
    titulo("3. Control de acceso")
    status, cuerpo, _ = pedir(_query(base, "/cog/info", url=url_cog))
    if status == 401:
        ok("/cog/info sin token", "401 — el control está activo")
    elif status == 503:
        fallo("/cog/info sin token", "503: falta MAP_TOKEN_SECRET en el tileserver.")
    elif status == 200:
        fallo("/cog/info sin token",
              "200 SIN TOKEN. Los tiles están abiertos a cualquiera. "
              "Revisá que el router de /cog tenga la dependencia del token.")
    else:
        aviso("/cog/info sin token", f"esperaba 401, dio {status}")


def escalon_info(base: str, url_cog: str, token: str) -> dict | None:
    titulo("4. Token válido + lectura del COG desde MinIO")
    status, cuerpo, _ = pedir(_query(base, "/cog/info", url=url_cog, token=token))
    datos = _json(cuerpo)

    if status == 200 and datos:
        ok("/cog/info con token", f"dtype={datos.get('dtype')} bandas={datos.get('count')}")
        print(f"          bounds: {datos.get('bounds')}")
        return datos

    if status == 401:
        fallo("/cog/info con token",
              "401: MAP_TOKEN_SECRET no coincide con GeoData__MapTokenSecret de "
              "Geocore, o el token venció (dura 1 h).")
    elif status == 400:
        fallo("/cog/info con token",
              f"400: {datos}. MINIO_BUCKET no coincide con GeoData__MinioBucket, "
              "o la key se guardó con el prefijo s3:// incluido.")
    elif status == 500:
        fallo("/cog/info con token",
              "500: la URL pasó el filtro pero GDAL no pudo abrir el COG. "
              "La key no existe, o la policy de tiler-ro no otorga s3:GetObject "
              "sobre los OBJETOS (el Resource necesita el sufijo /*).")
    else:
        fallo("/cog/info con token", f"status {status}")
    return None


def escalon_tile(base: str, url_cog: str, token: str, info: dict) -> None:
    titulo("5. Un tile real")
    z, x, y = _tile_del_centro(base, url_cog, token, info)
    print(f"          tile elegido: z={z} x={x} y={y}")

    tile_url = _query(base, f"/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png",
                      url=url_cog, token=token, rescale="-1,1", colormap_name="rdylgn")
    status, cuerpo, headers = pedir(tile_url)

    if status != 200:
        fallo("tile", f"status {status}: {cuerpo[:200]!r}")
        return
    if not cuerpo.startswith(PNG_MAGIC):
        fallo("tile", f"200 pero el cuerpo no es un PNG: {cuerpo[:60]!r}")
        return
    ok("tile", f"200, PNG de {len(cuerpo)} bytes")

    titulo("6. Caché")
    cache = headers.get("Cache-Control") or headers.get("cache-control")
    if cache and "immutable" in cache:
        ok("Cache-Control", cache)
    elif cache:
        aviso("Cache-Control", f"presente pero sin 'immutable': {cache}")
    else:
        aviso("Cache-Control", "ausente. El navegador va a repedir cada tile en cada pan/zoom.")

    titulo("7. Borde del raster")
    # Un tile lejísimos del dato: tiene que salir PNG transparente y no un error,
    # o el visor dibuja el ícono de "tile roto" en todo el borde de la capa.
    status, cuerpo, _ = pedir(_query(
        base, "/cog/tiles/WebMercatorQuad/12/1/1.png", url=url_cog, token=token))
    if status == 200 and cuerpo.startswith(PNG_MAGIC):
        ok("tile fuera de borde", f"200 + PNG de {len(cuerpo)} bytes (transparente)")
    else:
        aviso("tile fuera de borde",
              f"status {status}: el visor va a mostrar tiles rotos en los bordes.")


def _tile_del_centro(base: str, url_cog: str, token: str, info: dict) -> tuple[int, int, int]:
    """Elige un z/x/y con dato garantizado.

    Se prefiere `center` de tilejson, que ya trae el zoom que el raster resuelve;
    si no está, se cae al centro del bbox con un zoom fijo. Probar un tile
    elegido a mano es la forma más fácil de depurar un 404 que en realidad era
    "ese tile está fuera del raster".
    """
    status, cuerpo, _ = pedir(_query(base, "/cog/tilejson.json", url=url_cog, token=token))
    datos = _json(cuerpo) or {}
    centro = datos.get("center")
    if status == 200 and centro and len(centro) == 3:
        lon, lat, z = centro
        z = max(int(z), 1)
    else:
        b = info.get("bounds") or [-180, -85, 180, 85]
        lon, lat, z = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, 12

    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return z, min(x, n - 1), min(max(y, 0), n - 1)


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Verifica un tileserver desplegado.")
    p.add_argument("--base", required=True, help="URL del tileserver, con https://")
    p.add_argument("--token", required=True, help="JWT de GET /api/maps/token de Geocore")
    p.add_argument("--key", required=True,
                   help="storage_key del COG, sin s3:// ni bucket "
                        "(ej: ranchos/<id>/pasadas/<fecha>/ndvi.tif)")
    p.add_argument("--bucket", default="terra-assets")
    args = p.parse_args()

    base = args.base.rstrip("/")
    key = args.key.lstrip("/")
    if key.startswith("s3://"):
        print("El --key va SIN el prefijo s3:// ni el bucket: Geocore lo antepone.")
        return 2
    url_cog = f"s3://{args.bucket}/{key}"

    print(f"Tileserver : {base}")
    print(f"COG        : {url_cog}")

    escalon_health(base)
    escalon_ready(base)
    escalon_sin_token(base, url_cog)
    info = escalon_info(base, url_cog, args.token)
    if info:
        escalon_tile(base, url_cog, args.token, info)
    else:
        titulo("5-7. Tiles")
        print("  omitidos: sin /cog/info no tiene sentido pedir un tile.")

    print("\n" + "=" * 70)
    if _fallos:
        print(f"{len(_fallos)} fallo(s). El primero es la causa; los demás suelen ser consecuencia:")
        for f in _fallos:
            print(f"  - {f}")
    if _avisos:
        print(f"\n{len(_avisos)} aviso(s), no bloquean:")
        for a in _avisos:
            print(f"  - {a}")
    if not _fallos:
        print("Cadena completa verificada: token, red privada, credenciales de "
              "lectura, convención de keys y render.")
        if not _avisos:
            print("Sin avisos.")
    return 1 if _fallos else 0


if __name__ == "__main__":
    sys.exit(main())
