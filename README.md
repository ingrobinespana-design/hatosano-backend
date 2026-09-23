# Hato Sano — Backend (API)

API en **FastAPI** para la app de gestión ganadera. Login por finca + endpoints de animales, pesajes y tratamientos. Se conecta al esquema `hato` del Postgres-xgPP (el mismo de Vende Putumayo).

## Archivos
- `main.py` — la API.
- `requirements.txt` — dependencias.
- `Procfile` — comando de arranque.
- `.env.example` — variables de entorno (no subas tus valores reales).

## Variables de entorno (obligatorias)
| Variable | De dónde sale |
|---|---|
| `DATABASE_URL` | Railway → **Postgres-xgPP** → **Connect** → *Public Network* → copia la URL `postgresql://…` |
| `JWT_SECRET` | Invéntate una clave larga y aleatoria (ej. 40+ caracteres) |

## Desplegar en Render (gratis, como Vende Putumayo)
1. Crea un repo en GitHub (ej. `hatosano-backend`) y **sube estos 4 archivos**.
2. En **render.com** → **New +** → **Web Service** → conecta ese repo.
3. Configura:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free
4. En **Environment** agrega `DATABASE_URL` y `JWT_SECRET`.
5. **Create Web Service** y espera el deploy.

> Alternativa: desplegar como servicio en tu mismo proyecto de **Railway** (siempre encendido, sin arranque en frío). Ahí usa la conexión **privada** del Postgres.

## Probar que funciona
Abre la URL del servicio + `/docs` (ej. `https://hatosano-backend.onrender.com/docs`).
Verás la documentación interactiva (Swagger). Prueba en orden:
1. **POST /auth/registro** — crea tu finca y tu usuario → te devuelve un `token`.
2. Arriba a la derecha, botón **Authorize** → pega el token → autorízate.
3. **POST /animales** — crea un animal.
4. **GET /animales** — debe listarlo.
5. **POST /animales/{id}/pesajes** y **POST /tratamientos** — prueba lo demás.

## Correr en tu PC (opcional)
```bash
pip install -r requirements.txt
# define DATABASE_URL y JWT_SECRET en tu entorno
uvicorn main:app --reload
```

## Notas
- La API filtra **siempre por finca** (cada finca ve solo sus datos).
- `DELETE /animales/{id}` es baja lógica (`activo=false`), no borra el historial.
- Endpoints listos: registro, login, animales (CRUD), pesajes, tratamientos (con cálculo de periodo de retiro y próxima dosis de rabia).
- Pendiente (siguiente): subida de foto a Cloudinary y la app PWA que consume esta API.
