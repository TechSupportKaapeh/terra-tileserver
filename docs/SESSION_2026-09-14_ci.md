# Sesión 2026-09-14 — El CI del tileserver (sprint M.0)

> Sesión anterior: [`SESSION_2026-09-11_logging_piloto_y_el_png_blanco.md`](SESSION_2026-09-11_logging_piloto_y_el_png_blanco.md).
> La crónica del sprint, con lo común a los cuatro repos:
> `geework 2.0/docs/SESSION_2026-09-14_el_ci_en_los_cuatro_repos.md`. Referencia del
> CI: `geework 2.0/docs/CI.md`. Las decisiones de este repo siguen viviendo en
> `geework 2.0/docs/DECISIONS.md`; la de hoy es la **#34**.

## Qué se hizo

Tarea **M.0.5**: [terra-tileserver#1](https://github.com/TechSupportKaapeh/terra-tileserver/pull/1),
mergeado en `e5e9711`. El CI de `main` salió verde
([run](https://github.com/TechSupportKaapeh/terra-tileserver/actions/runs/34906259690)).

- `.github/workflows/ci.yml`: Python **3.11**, la del Dockerfile;
  `pip install -r requirements-dev.txt`, igual que la imagen; y `pytest`.
- El README dice que el CI corre los tests.

**150 tests**, iguales:
- en local, con un venv nuevo de 3.13 (el `.venv` del repo sigue muerto);
- en un clon limpio sin `.env` ni MinIO;
- en el runner, con 3.11 (0,8 s).

Después del merge, el deploy de Railway respondió `200` en `/health`.

## Lo que queda

- **El repo es público.** Proteger `main` está disponible con el plan gratis: la API
  responde `404 Branch not protected`, no `403`. Lo hace el equipo en M.0.6, con los
  pasos de `CI.md`.
- **El tileserver no audita dependencias**, y `aiofiles`, `boto3` y `numpy` no tienen
  pin. Queda registrado en `CI.md`.
