# tileserver-titiler

Servidor de tiles del ecosistema Terra. Traduce peticiones XYZ/WMTS a lecturas
parciales (HTTP Range) de los COG guardados en MinIO, vía GDAL `/vsis3/`.

**No escribe nada.** No sube archivos, no toca el catálogo, no habla con la base
de datos. Quien escribe los COG es el worker (`geeworker`); quien registra las
capas en `geodata.layers` es el worker; acá solo se lee.

---

## Su lugar en el ecosistema

```
front ──GET /api/layers/{id}──▶ Geocore ──arma la URL──┐
front ──GET /api/maps/token──▶ Geocore ──firma JWT─────┤
                                                        ▼
front ──GET /cog/tiles/{z}/{x}/{y}?url=…&token=…──▶ TiTiler ──/vsis3/──▶ MinIO
```

Geocore compone la URL como `s3://{GeoData:MinioBucket}/{layers.storage_key}`.
Por eso `storage_key` guarda la **key pelada**, sin el prefijo `s3://`.

---

## Variables de entorno

| Variable | Obligatoria | Para qué |
|---|---|---|
| `MAP_TOKEN_SECRET` | **sí** | Valida el JWT que firma Geocore. Sin ella, `/cog/*` devuelve **503**. |
| `MINIO_BUCKET` | **sí** | Único bucket del que se sirven COG. Además **acota qué rutas acepta `?url=`**: es la defensa contra SSRF. |
| `MINIO_ENDPOINT` | sí | Host:puerto del storage. En Railway, `minio.railway.internal:9000`. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | sí | Credenciales de **solo lectura**. |
| `MINIO_SECURE` | no | `True` si el endpoint es HTTPS. Con red privada, `False`. |
| `AWS_REGION` | no | Default `us-east-1`. MinIO no la usa, GDAL la exige. |
| `CORS_ALLOW_ORIGINS` | no | Default `*`. En producción, el dominio del front. |
| `TILE_CACHE_SECONDS` | no | Default un año. Los COG son inmutables. Bajalo para invalidar rápido en pruebas. |
| `PORT` | no | Lo inyecta Railway. Default `8001`. |

### Los dos valores que tienen que coincidir con Geocore

Esto es lo que más se rompe en deploy, porque nada lo valida y falla en silencio:

| Acá | En Geocore | Si no coinciden |
|---|---|---|
| `MAP_TOKEN_SECRET` | `GeoData__MapTokenSecret` | Todos los tiles dan 401 |
| `MINIO_BUCKET` | `GeoData__MinioBucket` | Geocore arma URLs de un bucket que este servicio rechaza: **400 `URL_NO_PERMITIDA`** por tile |

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

47 tests sobre los controles de acceso y el caché. No necesitan MinIO ni GDAL:
`terra_tiles/` no importa TiTiler a propósito, para que la lógica sea testeable
en aislamiento. `tests/` no entra en la imagen.

### Estructura

```
main.py              composición: lee config, arma dependencias, conecta
terra_tiles/
  settings.py        Settings (inmutable) + configure_gdal()
  security.py        validación del token (A01) y de la ruta (A10)
  caching.py         Cache-Control sobre /cog/tiles
```

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

**Primero `/health/ready`.** Un solo pedido, sin token, y distingue las formas
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
| `degradado` | 200 | MinIO respondió y aceptó las credenciales, pero negó la lectura. Puede ser una policy de `tiler-ro` sin `s3:GetObject`, o MinIO ocultando que la key no existe: no se puede concluir desde afuera. |
| `error` | 503 | El `detail` de cada check nombra la variable a corregir. |

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
| **500** | La URL pasó el filtro pero GDAL no pudo abrir el COG: la key no existe, o `tiler-ro` no tiene permiso |
| Timeout | `MINIO_ENDPOINT` mal escrito, o los servicios están en proyectos de Railway distintos |
