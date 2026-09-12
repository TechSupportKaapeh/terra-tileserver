# Sesión 2026-09-09 → 09-11 — El logging que faltaba, el piloto del front, y el PNG blanco

> Primera crónica en este repo; hasta ahora las decisiones del tileserver
> vivían en `geework 2.0/docs/DECISIONS.md` (#19–#22) y siguen ahí (#28 es de
> esta sesión). Explicación visual de todo lo de abajo, con diagramas:
> [`viaje-de-un-tile.html`](viaje-de-un-tile.html).

## Dónde quedó

El tileserver sirve los COG reales del worker, loguea de verdad, se presenta al
arrancar, y tiene una página para enseñarle al front a usarlo. **150 tests.**
Todo en `TechSupportKaapeh/terra-tileserver`:

| Commit | Qué |
|---|---|
| `aef1c4c` | logging configurado + reporte de arranque |
| `1003b75` | `/piloto`: cómo pintar un COG en el mapa |
| `1609488` | el piloto muestra el origen exacto que Geocore tiene que aceptar |
| `4a95afa` | el PNG "transparente" era blanco opaco; `/cog/point` fuera del raster da 404 |

---

## 1. Este servicio no tenía logging

`health.py` y `security.py` creaban loggers desde hace tiempo, y nadie les había
puesto un handler. Uvicorn no lo hace: su `LOGGING_CONFIG` configura
`uvicorn`, `uvicorn.error` y `uvicorn.access`, y **no toca el root**. Verificado:

```python
>>> logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
>>> logging.getLogger().handlers
[]
```

| Llamada | Qué pasaba |
|---|---|
| `logger.info(...)` | se descartaba |
| `logger.warning/error(...)` | salía por `logging.lastResort`: sin timestamp, sin nivel, sin logger |

Los avisos de tokens rechazados de `security.py` estaban en ese saco.

`terra_tiles/logging_config.py` lo arregla llamándose al importar `main.py`.
Alcanza porque uvicorn arma su logging en `Config.__init__` y recién importa la
app en `Config.load()` — leído del fuente, no supuesto. Nivel por `LOG_LEVEL`
(default `INFO`; un valor inválido cae a `INFO`, no deja mudo el servicio).

`sys.stdout` se reconfigura con `errors="replace"`: los mensajes de `health.py`
tienen acentos y, ahora que se ven, en una consola cp1252 convertirían la línea
en `--- Logging error ---`.

## 2. El reporte de arranque

`terra_tiles/arranque.py`: arte ASCII, configuración y estado de conexiones.
**Reusa las comprobaciones de `health.py`**, no las reimplementa. Lo que cambia
es el público: `/health/ready` es público y oculta el endpoint de MinIO; el log
del contenedor no lo es, así que ahí sí se imprime. Tres de los cuatro errores
recurrentes del despliegue son sobre la forma del endpoint, y ninguno se ve
mirando una variable sola:

```
bucket.railway.internal        falta el :9000
bucket.railway.internal:9000   con MINIO_SECURE=True  -> WRONG_VERSION_NUMBER
bucket.up.railway.app:9000     sobra el puerto        -> ConnectionReset
bucket.up.railway.app          con MINIO_SECURE=False
```

Va a nivel de módulo, no en un evento de `startup`: en el worker, un import que
falló impidió que el evento se disparara y el reporte nunca salió.

## 3. `/piloto`, la página para el front

El front no sabía usar TiTiler, y con razón: **la URL que devuelve Geocore no
alcanza.** `GET /api/layers/{id}` da `tiles[0]` con el servidor y la ruta del
COG, y le faltan tres parámetros que pone el front:

| Parámetro | Sin él |
|---|---|
| `token` | 401 en cada tile |
| `rescale=-1,1` | el COG es NDVI crudo en `float32`, números y no colores |
| `colormap_name` | no hay paleta, y la leyenda no tiene con qué coincidir |

El piloto recorre el flujo real contra los servicios: `/health/ready`, el token
de mapa (pegado, o pedido a Geocore), el COG con `/cog/info` y
`/cog/statistics`, y el pintado. Muestra la URL de Geocore al lado de la final,
genera el componente de react-leaflet (terra-admin usa React 19 +
react-leaflet 5 + Leaflet 1.9.4, y el piloto usa esa versión), y cuando un tile
falla lo pide con `fetch()` para mostrar el código HTTP, porque un `<img>` que
falla no dice por qué.

Se sirve en `/piloto` para quedar en el mismo origen que TiTiler (sin CORS) y
heredar el HTTPS de Railway. **No hace falta ngrok**: ngrok expone algo que
corre en una máquina local, y nada de esto corre en local.

**Pedirle el token a Geocore desde el piloto** necesita que Geocore acepte su
origen (`Cors__Origins__N = https://<tileserver>`). La página muestra el valor
exacto. Abierta como archivo manda `Origin: null`, y eso **no** se agrega a
Geocore: `null` es también el origen de cualquier iframe con sandbox.

Reglas que protege `tests/test_piloto.py`: no guarda tokens en `localStorage`,
no usa `innerHTML`, no trae un JWT real, no pasa `nodata`.

> **Destino:** el diagnóstico se muda al panel de terra-admin, sólo para
> TerraAdmin. Ver la sesión de Geocore del 2026-09-11.

## 4. El cuadrado blanco

Al pintar un heatmap real del worker apareció un cuadrado blanco macizo junto a
un raster que casi no se veía.

**Causa:** el PNG que se devolvía para `TileOutsideBounds` era un literal base64
comentado como transparente. Decodificado: 1×1, gris+alfa, píxel
`[filtro 1, gris 255, alfa 255]` — **blanco opaco** —, y además con el CRC del
bloque IDAT roto (los navegadores lo dibujaban igual). Leaflet lo estira a
256×256.

**Por qué se vio recién ahora:** el heatmap del worker está alineado exacto al
tile de zoom 14 `3626/6981`. Sus bounds difieren del borde del tile vecino en
10⁻¹⁴ grados, así que algún vecino "se superpone", se pide, cae fuera del
raster y vuelve blanco. Esto último es la explicación más probable, no una
medición.

**Y quedaba pegado:** `/cog/tiles` lleva `Cache-Control` de un año y la URL
incluye el token. Después del deploy, si todavía se ve blanco, se pide un token
nuevo.

**Arreglo:** `terra_tiles/png.py` arma el PNG desde los bytes del píxel, con alfa
0, y `tests/test_png.py` lo decodifica. El test documenta también los dos
defectos del literal viejo.

El raster, en cambio, estaba bien: tiene dato en **35 de 65.536 píxeles**, con
NDVI entre −0,03 y 0,18. Con `rescale=-1,1` eso cae en el centro de `rdylgn`,
que es crema casi blanco `(254, 254, 189)`. Es una pregunta sobre los datos del
worker, no sobre TiTiler.

## 5. `/cog/point` fuera del raster, y por qué no se activaron los errores de TiTiler

Un click fuera del raster daba **500**: TiTiler 0.18 no convierte
`PointOutsideBounds` en 404. Ahora hay un manejador propio con mensaje fijo.

Lo obvio era registrar `add_exception_handlers` de TiTiler. **No se hizo**: uno
de sus manejadores devuelve `str(exc)` de cualquier excepción, y los errores de
GDAL traen el endpoint de S3 (verificado: `CURL error: Failed to connect to
<host> port <puerto>`). Publicaría `bucket.railway.internal:9000`.
`DECISIONS #28`.

## 6. Datos de TiTiler 0.18 que el front necesita saber

Verificados contra el paquete instalado, no de memoria:

- Rutas del `TilerFactory`: `/tiles/{tms}/{z}/{x}/{y}[@{scale}x]`, `/info`,
  `/statistics`, `/point/{lon},{lat}`, `/bounds`, `/preview`, y **las dos**
  formas de tilejson (`/tilejson.json` y `/{tms}/tilejson.json`).
- **`/colorMaps` no está**: es de `ColorMapFactory`, que este servicio no monta.
  El piloto la pedía (error mío del commit `1003b75`); ya no.
- **Un archivo inexistente es 500, no 404.**
- **No pasar `nodata=0`**: el COG trae máscara interna (`add_mask=True` en el
  worker) y NDVI = 0 es un valor real. El `/viewer` viejo lo pasa.

## 7. CORS, para no volver a preguntarlo

- **¿Hace falta?** Depende de cómo se pidan los tiles: Leaflet con `<img>` no lo
  necesita; MapLibre sí (usa `fetch` y WebGL rechaza texturas sin CORS); y
  cualquier `fetch` a `/cog/info` o `/tilejson.json` también. Queda puesto.
- **`*` es aceptable acá**: no hay cookies ni credenciales ambientales, así que
  CORS no autoriza nada que el token no controle ya. Restringirlo cuando existan
  los dominios reales del front.
- **Vacío no es ausente:** `CORS_ALLOW_ORIGINS=` son cero orígenes; todo
  bloqueado.

## 8. Correcciones a datos que di mal

- **`MINIO_SECURE` con el endpoint privado va en `false`** (o sin poner). En una
  lista de variables había dicho `true`, asumiendo el dominio público.
- El tileserver deduce **nada**: a diferencia del worker, su default de
  `MINIO_SECURE` es `false` fijo. `/health/ready` avisa si no coincide con el
  endpoint.

---

## Lo que esta sesión NO arregló

- **El token de mapa va en la URL de cada tile.** El caché de un año rinde una
  hora, porque cada token nuevo cambia todas las URLs.
- **El token de mapa no está atado al tenant**, y TiTiler sólo mira que la ruta
  esté en el bucket. Ver la sesión del worker del 09-11.
- **El `/viewer` viejo** pasa `nodata=0` y usa `innerHTML`. Anotado, no tocado.
- **El `.venv` de este repo está muerto:** se creó en otra máquina
  (`C:\Users\USUARIO\…`) con Python 3.11 del Store. Los tests de esta sesión
  corrieron en un venv aparte con Python 3.13.
- **Endurecer `/piloto`** (activarlo con una variable, SRI y CSP, URL de Geocore
  fija): propuesto, no hecho. Queda en suspenso porque el diagnóstico se muda al
  panel.
