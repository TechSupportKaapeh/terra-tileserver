# tileserver-titiler

Servidor de tiles del ecosistema Terra. Traduce peticiones XYZ/WMTS a lecturas
parciales (HTTP Range) de los COG guardados en MinIO, vía GDAL `/vsis3/`.

**No escribe nada.** No sube archivos, no toca el catálogo, no habla con la base
de datos. Quien escribe los COG es el worker (`geeworker`); quien registra las
capas en `geodata.layers` es el worker; acá solo se lee.

---

## Su lugar en el ecosistema

```
front ──GET /api/layers/{id}────────▶ Geocore ──arma la URL──┐
front ──GET /api/maps/token────────▶ Geocore ──firma JWT──────┤
        + X-Tenant-ID                (con el tenant adentro)   ▼
front ──GET /cog/tiles/{z}/{x}/{y}?url=…&token=…──▶ TiTiler ──/vsis3/──▶ MinIO
                                                        │
                                          ¿la key cuelga de tenants/{tenant
                                          del token}/?  si no → 403
```

Geocore compone la URL como `s3://{GeoData:MinioBucket}/{layers.storage_key}`.
Por eso `storage_key` guarda la **key pelada**, sin el prefijo `s3://`.

---

## Variables de entorno

| Variable | Obligatoria | Para qué |
|---|---|---|
| `MAP_TOKEN_SECRET` | **sí** | Valida el JWT que firma Geocore. Sin ella, `/cog/*` devuelve **503**. |
| `MINIO_BUCKET` | **sí** | Único bucket del que se sirven COG. Además **acota qué rutas acepta `?url=`**: es la defensa contra SSRF. |
| `MINIO_ENDPOINT` | sí | Host:puerto del storage. En Railway, el dominio privado **con** puerto: `<servicio>.railway.internal:9000`. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | sí | Credenciales de **solo lectura**. |
| `MINIO_SECURE` | no | Default `false` **fijo** (a diferencia del worker, acá no se deduce del host). Con el dominio privado, dejarla sin poner o en `false`: la red privada no hace TLS y `true` da `WRONG_VERSION_NUMBER`. Sólo el dominio público va en `true`. |
| `AWS_REGION` | no | Default `us-east-1`. MinIO no la usa, GDAL la exige. |
| `CORS_ALLOW_ORIGINS` | no | Default `*`, aceptable mientras no haya cookies (ver `docs/SESSION_2026-09-11…` §7). **Vacía no es ausente**: son cero orígenes. |
| `TILE_CACHE_SECONDS` | no | Default un año. Los COG son inmutables. Bajalo para invalidar rápido en pruebas. |
| `LOG_LEVEL` | no | Default `INFO`, que es el que muestra el reporte de arranque. Un valor inválido cae a `INFO`. |
| `PORT` | no | Lo inyecta Railway. Default `8001`. |

### Los dos valores que tienen que coincidir con Geocore

Esto es lo que más se rompe en deploy, porque nada lo valida y falla en silencio:

| Acá | En Geocore | Si no coinciden |
|---|---|---|
| `MAP_TOKEN_SECRET` | `GeoData__MapTokenSecret` | Todos los tiles dan 401 |
| `MINIO_BUCKET` | `GeoData__MinioBucket` | Geocore arma URLs de un bucket que este servicio rechaza: **400 `URL_NO_PERMITIDA`** por tile |

Y una tercera cosa que tiene que coincidir, y que **no es una variable**: el
nombre del claim del tenant. Acá se lee `tenant_id` (`security.CLAIM_TENANT`) y
Geocore lo firma con ese nombre (`MapsController.TenantClaim`). Cambiarlo de un
solo lado no rompe nada visible: el token sigue siendo válido y **todos** los
COG pasan a dar 403.

---

## Seguridad

**El secreto no tiene default, a propósito.** Antes había uno hardcodeado en
`main.py`, lo que hacía que un deploy sin configurar fallara **abierto**:
cualquiera que leyera el repo podía forjar un token válido para cualquier COG
del bucket.

Geocore cerró el mismo agujero de su lado (`DECISIONS #16`, revisión del
2026-08-17) y devuelve 503 cuando le falta el secreto. Acá se replica esa
conducta y el mismo código `MAP_TOKEN_UNAVAILABLE`, para que los dos lados
fallen igual y el problema sea diagnosticable desde los logs.

**Las credenciales de MinIO deben ser de solo lectura.** Este servicio se expone
al público (sirve tiles al navegador). Con credenciales de escritura, una falla
acá pone en riesgo el bucket entero.

### El token es de un tenant (M.8.1, OWASP A01)

Hasta el 2026-09-20 el token decía **quién** pedía tiles y no decía **cuáles**:
validado firma, emisor, audiencia y `type`, se servía cualquier COG del bucket.
La key viaja a la vista en la URL del tile, así que un usuario con su token
legítimo y la key de otro tenant veía los rásters de ese otro tenant.

Ahora el token lleva el claim `tenant_id` y **la ruta tiene que caer bajo
`s3://{MINIO_BUCKET}/tenants/{ese tenant}/`**:

| Caso | Respuesta |
|---|---|
| COG del propio tenant | se sirve |
| COG de otro tenant | **403 `TENANT_AJENO`** |
| Token sin `tenant_id` (emitido antes del cambio) | **403 `TOKEN_SIN_TENANT`** |
| Cualquier URL fuera del bucket | **400 `URL_NO_PERMITIDA`**, como antes |

**Los dos controles ya no son independientes**: el de ruta depende del de token,
y se le pasa la **misma** función —no otra igual— para que FastAPI la resuelva
una sola vez por pedido y no haya dos lecturas del claim que puedan discrepar.

La comparación lleva **la barra final** (`tenants/{id}/`): sin ella, el prefijo
de un tenant sería también el comienzo de cualquier otro id que empezara igual.

Las capas **anteriores al pipeline mensual** tienen keys sin tenant
(`parcelas/{id}/…`, `ranchos/{id}/…`) y por lo tanto **ya no se pueden servir**.
Se borran con sus filas: Geocore `DECISIONS #43`.

**Lo que este control no alcanza:** los assets listados *dentro* de un
MosaicJSON. El tenant se compara contra la URL del documento, no contra lo que
el documento lista, y `cogeo-mosaic` abre esos assets tal como vengan. Lo
contiene lo mismo que contiene el hallazgo T-3 del mapeo OWASP —sólo `worker-rw`
escribe en el bucket, así que un mosaico sólo aparece ahí si lo puso el worker—,
pero desde M.8.1 lo que se saltearía es el aislamiento entre tenants, no sólo el
filtro anti-SSRF.

---

## Desarrollo local

```bash
cp .env.example .env      # y completar los valores
pip install -r requirements.txt
uvicorn main:app --reload --port 8001
```

O desde el compose del worker, que levanta MinIO y este servicio juntos.

### Correr los tests

```bash
pip install -r requirements-dev.txt
pytest
```

185 tests sobre los controles de acceso, el caché, el reporte de arranque, el
piloto y el PNG de fuera del raster. Casi ninguno necesita MinIO ni GDAL:
`terra_tiles/` no importa TiTiler a propósito, para que la lógica sea testeable
en aislamiento. `tests/` no entra en la imagen.

La excepción es `tests/test_app_tenant.py`, que **sí** levanta la app entera con
TiTiler montado (sin red: el endpoint de MinIO apunta a un puerto cerrado).
Prueba lo que ninguna función puede probar sola: que `main.py` conecte los dos
controles y que FastAPI resuelva la dependencia anidada en un pedido real.

**Desde el 2026-09-14 también los corre el CI** (`.github/workflows/ci.yml`), en
cada PR y en cada push a `main`, con Python 3.11, la del Dockerfile. Qué corre
cada uno de los cuatro repos, y cómo proteger `main`, en
`geework 2.0/docs/CI.md` (`DECISIONS #34` del worker).

### Estructura

```
main.py              composición: lee config, arma dependencias, conecta
terra_tiles/
  settings.py        Settings (inmutable) + configure_gdal()
  security.py        validación del token y de la ruta: quién pide (A01), qué
                     puede pedir (A01 por tenant, A10 contra SSRF)
  caching.py         Cache-Control sobre /cog/tiles
  health.py          comprobaciones de /health/ready
  logging_config.py  el logging que uvicorn no configura por su cuenta
  arranque.py        reporte de arranque: config y conexiones, sin secretos
  png.py             el PNG transparente de fuera del raster, armado con código
public/
  piloto.html        /piloto: cómo pintar un COG en el mapa, para el front
  leaflet-cog.html   /viewer: visor viejo de diagnóstico
docs/                crónicas, y viaje-de-un-tile.html con los diagramas
```

### Rutas propias (además de las de TiTiler)

| Ruta | Token | Para qué |
|---|---|---|
| `/health` | no | Liveness de Railway. No consulta MinIO a propósito. |
| `/health/ready` | no | Diagnóstico de config y storage. Nunca publica el endpoint interno. |
| `/piloto` | no (los tiles sí) | Página para el front: el flujo real, paso a paso. |
| `/viewer` | no (los tiles sí) | Visor viejo. Pasa `nodata=0`, que borra NDVI = 0: usar `/piloto`. |

Dos respuestas propias sobre las de TiTiler: un tile **fuera del raster** es
**200 con un PNG transparente** (no un error, para que el mapa no dibuje el
ícono de tile roto), y un `/cog/point` fuera del raster es **404** con mensaje
fijo. Los demás errores de rio-tiler salen como 500 **sin el mensaje crudo**,
porque el de GDAL trae el endpoint de MinIO (`DECISIONS #28` del worker).

`configure_gdal()` **tiene que correr antes de importar TiTiler**: GDAL lee sus
variables al cargarse y fijarlas después se ignora en silencio. Por eso
`terra_tiles/` no importa nada de la pila geoespacial.

### Verificar la conexión con MinIO

```bash
python scripts/check_titiler_minio.py
```

Sube un GeoTIFF mínimo y lo lee por `/vsis3/`, que es exactamente el mecanismo
que usa TiTiler internamente. Si ese script pasa, los tiles van a funcionar.

> `scripts/` no entra en la imagen (`.dockerignore`): es diagnóstico, no servicio.
> `numpy` está en `requirements.txt` solo para él. `minio` ya no: lo usa
> `/health/ready` y es dependencia del servicio.

Contra un deploy remoto no hace falta el script — está `/health/ready`, que
sondea MinIO desde adentro y no necesita credenciales de escritura.

---

## Despliegue en Railway

1. Servicio propio, desde este repo.
2. Variables de la tabla de arriba. **`MAP_TOKEN_SECRET` primero**: sin ella el
   servicio levanta y el healthcheck pasa, pero los tiles dan 503.
3. Healthcheck en `/health` (no requiere token).
4. MinIO va en **otro** servicio, con volumen propio.

> **El healthcheck de Railway va a `/health`, no a `/health/ready`.** `/health`
> solo dice que el proceso vive y nunca consulta MinIO: si dependiera del
> storage, un parpadeo de MinIO haría que Railway reinicie un tileserver sano,
> y el reinicio no arregla nada de lo que falló. `/health/ready` es para
> diagnosticar, no para que un orquestador tome decisiones.

### Verificación post-deploy

**Antes que nada, el log de arranque.** El servicio imprime al arrancar su
configuración —el endpoint de MinIO y `MINIO_SECURE` en líneas contiguas, los
secretos sólo por largo— y el resultado de las mismas comprobaciones de
`/health/ready`. Lo que falla sale además como `ERROR`, así que sobrevive a
cualquier `LOG_LEVEL`. Si ahí dice *"Todo verificado responde"*, lo que sigue es
el tile real.

**Después, `/health/ready`.** Un solo pedido, sin token, y distingue las formas
conocidas de romper el deploy antes de que haga falta subir ningún COG:

```bash
curl https://<titiler>/health/ready
```

```json
{
  "status": "ok",
  "bucket": "terra-assets",
  "checks": [
    {"name": "map_token_secret", "status": "ok", "detail": "Configurado."},
    {"name": "minio",            "status": "ok", "detail": "MinIO respondió y la lectura funciona."}
  ]
}
```

| `status` | HTTP | Qué hacer |
|---|---|---|
| `ok` | 200 | Config y storage listos. Falta el tile real. |
| `degradado` | 200 | Hay algo sospechoso pero no concluyente. No tumba el chequeo a 503 a propósito. |
| `error` | 503 | El `detail` de cada check nombra la variable a corregir. |

Cuando hay algo que corregir, el check trae un `cause` estable para no tener que
parsear el texto:

| `cause` | Check | Qué significa |
|---|---|---|
| `falta_puerto` | `minio_endpoint` | Dominio privado sin `:9000`. **La red privada no mapea puertos**; sin puerto se asume el 80. Es el público el que va sin puerto. |
| `puerto_en_publico` | `minio_endpoint` | Al revés: el dominio público va sin puerto, el edge escucha en 443. |
| `tls_en_privado` / `sin_tls_en_publico` | `minio_endpoint` | `MINIO_SECURE` al revés. Privado → `False`, público → `True`. |
| `localhost` | `minio_endpoint` | Dentro del contenedor, `localhost` es el propio contenedor. Suele ser un `.env` de docker-compose quedado de antes. |
| `credencial_por_defecto` | `minio_credenciales` | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` no están en el entorno y se está usando el default `minioadmin`. Si nombraste la variable distinto (p.ej. `MINIO_ROOT_USER`), el mensaje lo dice. |
| `policy_no_asignada` | `minio` | Las credenciales son válidas pero MinIO negó objeto **y** bucket. La policy no está asignada al usuario, o no nombra este bucket. |
| `bucket_inexistente` | `minio` | Las credenciales llegan y no hay ningún bucket con ese nombre. |
| `dns` | `minio` | El hostname no resolvió: nombre de servicio equivocado, o servicios en proyectos distintos. |
| `rechazado` | `minio` | Resolvió pero nada escucha ahí: puerto equivocado (9001 es la consola), o MinIO escuchando solo en IPv4 — la red privada de Railway es **solo IPv6**. |
| `tls` | `minio` | Corte de conexión sin handshake. Revisá `MINIO_SECURE`. |
| `timeout` | `minio` | MinIO arrancando, caído, o el puerto atiende otro protocolo. |

`minio_endpoint` revisa la **forma** del endpoint sin tocar la red, así que
señala la causa incluso cuando el sondeo solo puede decir "no hubo respuesta".

Sirve sobre todo para el fallo más caro de diagnosticar: **sin
`MAP_TOKEN_SECRET`, `/health` da 200 y Railway muestra el servicio verde
mientras todos los tiles dan 503.** `/health/ready` lo dice de entrada.

Lo que **no** prueba: que la configuración de GDAL sea correcta. El sondeo usa
el cliente de MinIO, no `/vsis3/`. Salen de la misma `Settings`, pero detalles
como `AWS_S3_ADDRESSING_STYLE=path` solo los ejercita GDAL — eso lo cierra
recién el primer tile real.

#### Después, los tiles

`/health` en 200 no prueba nada sobre el token: la validación corre en `/cog/*`.
Para cerrar el flujo hace falta un tile real con un token de
`GET /api/maps/token` de Geocore.

| Respuesta | Qué significa |
|---|---|
| **401** sin token | Correcto: el control está activo |
| **200** + PNG con token válido | Todo el flujo funciona |
| **503** | Falta `MAP_TOKEN_SECRET` |
| **401** *con* token válido | El secreto no coincide con el de Geocore, o el token expiró (dura 1 h) |
| **400 `URL_NO_PERMITIDA`** | `MINIO_BUCKET` no coincide con `GeoData__MinioBucket`, o `storage_key` quedó guardado con el prefijo `s3://` |
| **403 `TENANT_AJENO`** | El token es de otro tenant que la key. No se arregla reintentando: pedí el token con el `X-Tenant-ID` del dueño de esa capa. Si la key no empieza con `tenants/`, es una capa vieja y ya no se sirve |
| **403 `TOKEN_SIN_TENANT`** | Token emitido antes de M.8.1, o una Geocore anterior al cambio. Dura lo que dura un token: 1 h |
| **500** | La URL pasó el filtro pero GDAL no pudo abrir el COG: la key no existe, o `tiler-ro` no tiene permiso |
| Timeout | `MINIO_ENDPOINT` mal escrito, o los servicios están en proyectos de Railway distintos |
