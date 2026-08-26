"""Componentes del tileserver de Terra, separados de la composición de la app.

Ninguno de estos módulos importa TiTiler ni rasterio: eso permite testearlos
sin la pila geoespacial, y garantiza que `configure_gdal()` pueda correr antes
de que GDAL se cargue.
"""
