"""Configuración de logging del tileserver.

POR QUÉ EXISTE
--------------
**Hasta ahora este servicio no tenía logging configurado, y sus logs se
perdían.** `terra_tiles/health.py` y `terra_tiles/security.py` crean loggers
desde hace tiempo, pero nadie les puso nunca un handler, y uvicorn no lo hace
por vos: su `LOGGING_CONFIG` configura exactamente tres loggers —`uvicorn`,
`uvicorn.error`, `uvicorn.access`— y **no toca el root**. Verificado:

    >>> logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    >>> logging.getLogger().handlers
    []

Con el root sin handlers, lo que pasa es esto:

    logger.info(...)     se descarta, no llega a ningún lado
    logger.warning(...)  sale por `logging.lastResort`, que es un
    logger.error(...)    StreamHandler a stderr de nivel WARNING **sin
                         formato**: sin timestamp, sin nivel, sin el nombre
                         del logger

O sea que la mitad de los mensajes no existía y la otra mitad salía como texto
suelto, indistinguible de un `print`. Los avisos de `security.py` sobre tokens
rechazados entraban en esa categoría.

CUÁNDO CORRE
------------
Se llama al importar `main.py`, y eso alcanza porque uvicorn arma su logging
**antes** de importar la app: `Config.__init__` llama a `configure_logging()`,
y `load_app()` recién ocurre después, dentro de `Config.load()`. Así que este
`basicConfig` se aplica encima del de uvicorn en vez de ser pisado por él.

No se reemplaza la configuración de uvicorn: se le agrega la que falta. Sus
tres loggers siguen con su formato propio (`INFO:     ...`), y los del servicio
pasan a tener el suyo.
"""

import logging
import os
import sys

# Un nivel más alto que INFO oculta el reporte de arranque, que es justo lo que
# uno quiere ver cuando un deploy sale mal. Se puede subir con `LOG_LEVEL`, pero
# el default es el que sirve para diagnosticar.
NIVEL_POR_DEFECTO = "INFO"

FORMATO = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
FORMATO_FECHA = "%Y-%m-%dT%H:%M:%SZ"


def nivel_desde_entorno(valor: str | None) -> int:
    """Traduce `LOG_LEVEL` a un nivel de logging, tolerando basura.

    Un `LOG_LEVEL` mal escrito no puede dejar el servicio mudo: sería el mismo
    modo de fallo que este módulo vino a arreglar. Ante cualquier valor que no
    se reconozca, se cae a INFO y se avisa.
    """
    nombre = (valor or NIVEL_POR_DEFECTO).strip().upper()
    nivel = logging.getLevelNamesMapping().get(nombre)
    if nivel is None:
        return logging.INFO
    return nivel


def configurar_logging(entorno: dict | None = None) -> None:
    entorno = os.environ if entorno is None else entorno
    crudo = entorno.get("LOG_LEVEL")
    nivel = nivel_desde_entorno(crudo)

    # Los mensajes de `health.py` tienen acentos ("poné MINIO_SECURE=False"), y
    # hasta ahora eso no importaba porque no se veían. Ahora sí.
    #
    # En el contenedor no hay problema: Linux y Python 3.13 son UTF-8. En una
    # consola de Windows, que es cp1252, un acento hace que el handler tire
    # `UnicodeEncodeError` y en vez del mensaje salga un "--- Logging error
    # ---". Ya nos pasó dos veces con un tick verde y con un em-dash, y las dos
    # veces el síntoma apuntó al lugar equivocado.
    #
    # `errors="replace"` degrada el acento a `?` en vez de perder la línea
    # entera. Va en try/except porque no todo stream es reconfigurable —un pipe
    # capturado por pytest, por ejemplo— y esto no puede ser lo que rompa.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    # `force=True` para que sea idempotente: si algo dejó un handler a medias
    # —un test, un import doble— se reemplaza en vez de duplicar cada línea.
    #
    # A stdout y no a stderr: en un contenedor los dos van al mismo lado, pero
    # stdout es lo convencional para logs de aplicación y deja stderr para lo
    # que de verdad es un fallo del proceso.
    logging.basicConfig(
        level=nivel,
        format=FORMATO,
        datefmt=FORMATO_FECHA,
        stream=sys.stdout,
        force=True,
    )

    if crudo and nivel_desde_entorno(crudo) == logging.INFO and crudo.strip().upper() != "INFO":
        logging.getLogger(__name__).warning(
            "LOG_LEVEL='%s' no es un nivel valido; se usa INFO.", crudo,
        )
