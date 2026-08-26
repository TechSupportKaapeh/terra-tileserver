FROM python:3.11-slim

# rasterio trae GDAL en su wheel; libexpat1 es la dependencia de sistema que falta.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El Dockerfile anterior NO copiaba el código: funcionaba solo porque el compose
# hacía bind mount de la carpeta. Sin mount (Railway) la imagen quedaba sin
# main.py y el contenedor moría al arrancar.
COPY main.py .
COPY terra_tiles/ ./terra_tiles/
COPY public/ ./public/

# Servicio público: no correr como root.
RUN useradd --create-home --uid 10001 tiler && chown -R tiler:tiler /app
USER tiler

# Railway inyecta $PORT y espera que el proceso lo escuche. El 8001 es solo el
# fallback para `docker run` a mano.
ENV PORT=8001
EXPOSE 8001

# Sin --reload: es un flag de desarrollo, vigila el filesystem y duplica procesos.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
