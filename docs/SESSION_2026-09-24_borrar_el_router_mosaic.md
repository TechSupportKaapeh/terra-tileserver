# Se borró el router `/mosaic` (2026-09-24)

> **Cierra el hallazgo T-3 del mapeo OWASP borrando la superficie**, que es la decisión del
> usuario en [`DECISIONS #64`](../../geework%202.0/docs/DECISIONS.md) del worker. Es la misma
> cura que W-3 (`#23`): un hallazgo se cierra sacando lo que lo carga, no reimplementando un
> control que después hay que mantener.

## Qué era T-3

El `path_dependency` del tileserver valida **la URL del MosaicJSON**, pero no **los assets que
ese documento lista adentro**: `cogeo-mosaic` los abre tal como vengan. Desde M.8.1 eso pesaba
más de lo que pesaba antes — lo que se saltearía ya no era sólo el filtro anti-SSRF, era **el
aislamiento entre tenants**, porque el tenant se compara contra la URL del documento y no
contra lo que lista.

Lo contenía que sólo `worker-rw` escribe en el bucket. Un control que depende de que nadie más
tenga la credencial no es un control: es una apuesta.

## Por qué se borra en vez de arreglarse

Porque **no lo usaba nadie**. `DECISIONS #31` del worker dice explícitamente que no se usa
MosaicJSON: desde el pipeline mensual, el mapa de un rancho es **un COG por índice y por mes**,
que es lo que pide `/cog`. No lo usaban el panel, ni `/piloto`, ni `scripts/check_prod.py`, ni
el worker.

Superficie muerta que carga un hallazgo abierto es el peor negocio posible: se paga el riesgo
y no se cobra nada.

## Qué cambió

| Archivo | Qué |
|---|---|
| `main.py` | fuera el `MosaicTilerFactory`, su `include_router` y el aviso de T-3. En su lugar queda un comentario que dice qué había y por qué no está |
| `requirements.txt` | fuera `titiler.mosaic==0.18.0` y `boto3`, que **existían sólo para eso**. boto3 lo pedía `cogeo_mosaic.backends.s3.S3Backend`; GDAL nunca lo usó, llega a MinIO por sus propias variables |
| `terra_tiles/settings.py` | `AWS_ENDPOINT_URL_S3` **se deja puesta**, con el docstring corregido. Es una variable de más; sacarla toca el único camino por el que GDAL llega a MinIO, y eso se prueba contra el deploy, no de paso acá |
| `tests/test_app_tenant.py` | `RUTAS` pasa de dos a uno, y entra `test_el_router_mosaic_ya_no_existe` |
| `tests/test_settings.py` | los dos tests del endpoint dejan de hablar de boto3 y dicen qué fijan de verdad |
| `scripts/check_mosaic_median.py` | **borrado**: verificaba que MosaicJSON compusiera por mediana, e importa `cogeo_mosaic`. Sin el paquete no corre, y sin el router no verifica nada del servicio |
| `README.md`, `public/piloto.html` | los dos decían que hay dos routers montados |

`RUTAS` sigue siendo una lista de un elemento con su `parametrize`, **a propósito**: el día que
se monte otro router, el que lo monte lo suma ahí y hereda los cuatro controles de M.8.1 de una
línea. Con los tests escritos contra `/cog` a mano, el router nuevo nacería sin ninguno.

## El control negativo, que acá hacía falta más que nunca

Sacar `/mosaic` de `RUTAS` deja los tests verdes **aunque el router siga montado**: lo único
que pasaría es que nadie lo mira. Por eso el borrado tiene su propio test, y por eso se corrió
el control: **con el router remontado a mano, `test_el_router_mosaic_ya_no_existe` sale en
rojo**. Verificado el 2026-09-24.

El test pide **con token**. Un 401 también sería "no pasa" y probaría otra cosa; lo que tiene
que decir es que la ruta no existe.

## Verificación

| Qué | Resultado |
|---|---|
| `pytest` | **181 verdes** (eran 185: se van 5 casos parametrizados de `/mosaic` y entra 1 nuevo) |
| El control negativo | corrido: con el router remontado, el test nuevo falla |
| La app sin los paquetes | se importó `main` con `titiler.mosaic`, `cogeo_mosaic`, `boto3` y `botocore` **bloqueados en el `meta_path`**: arranca igual, y los prefijos montados son `cog`, `health`, `piloto`, `viewer`, `static`, `docs`, `redoc` y `openapi.json`. Ninguna ruta `/mosaic` |

Ese último es el que importa: los tests corren con el venv de esta máquina, que todavía tiene
los paquetes instalados. Lo que prueba que `requirements.txt` quedó bien es el CI, que instala
desde cero — y, antes de eso, bloquear el import a mano.

