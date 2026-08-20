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
| `MINIO_ENDPOINT` | sí | Host:puerto del storage. En Railway, `minio.railway.internal:9000`. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | sí | Credenciales de **solo lectura**. |
| `MINIO_SECURE` | no | `True` si el endpoint es HTTPS. Con red privada, `False`. |
| `AWS_REGION` | no | Default `us-east-1`. MinIO no la usa, GDAL la exige. |
| `CORS_ALLOW_ORIGINS` | no | Default `*`. En producción, el dominio del front. |
| `PORT` | no | Lo inyecta Railway. Default `8001`. |

### Los dos valores que tienen que coincidir con Geocore

Esto es lo que más se rompe en deploy, porque nada lo valida y falla en silencio:

| Acá | En Geocore | Si no coinciden |
|---|---|---|
| `MAP_TOKEN_SECRET` | `GeoData__MapTokenSecret` | Todos los tiles dan 401 |
| El bucket que usa el worker (`MINIO_BUCKET`) | `GeoData__MinioBucket` | TiTiler apunta a un bucket inexistente: 404 por tile |

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

### Verificar la conexión con MinIO

```bash
python scripts/check_titiler_minio.py
```

Sube un GeoTIFF mínimo y lo lee por `/vsis3/`, que es exactamente el mecanismo
que usa TiTiler internamente. Si ese script pasa, los tiles van a funcionar.

> `scripts/` no entra en la imagen (`.dockerignore`): es diagnóstico, no servicio.
> `minio` y `numpy` están en `requirements.txt` solo para él.

---

## Despliegue en Railway

1. Servicio propio, desde este repo.
2. Variables de la tabla de arriba. **`MAP_TOKEN_SECRET` primero**: sin ella el
   servicio levanta y el healthcheck pasa, pero los tiles dan 503.
3. Healthcheck en `/health` (no requiere token).
4. MinIO va en **otro** servicio, con volumen propio.

### Verificación post-deploy

`/health` en 200 no prueba nada sobre el token: la validación corre en `/cog/*`.
Para cerrar el flujo hace falta un tile real con un token de
`GET /api/maps/token` de Geocore.

- `/cog/tiles/...` sin token → **401**
- `/cog/tiles/...` con token válido → **200** y un PNG
- Si da **503**, falta `MAP_TOKEN_SECRET`
- Si da **404** por tile, el bucket o la key no coinciden
