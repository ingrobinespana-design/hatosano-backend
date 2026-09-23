"""
Hato Sano — API (FastAPI)
Backend MVP: login por finca + endpoints del ganado (animales, pesajes, tratamientos).
Se conecta al esquema "hato" del Postgres-xgPP (el mismo de Vende Putumayo).
"""
import os
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from passlib.context import CryptContext
import jwt

# ---------------- configuración ----------------
DATABASE_URL = os.environ["DATABASE_URL"]
if DATABASE_URL.startswith("postgres://"):            # Railway/Render dan postgres://; SQLAlchemy quiere postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
JWT_SECRET = os.environ.get("JWT_SECRET", "cambia-esta-clave")
JWT_ALG = "HS256"
TOKEN_HORAS = 24 * 30                                   # sesión de 30 días

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
# migración idempotente: columna para la segunda foto del animal
try:
    with engine.begin() as _con:
        _con.execute(text("ALTER TABLE hato.animales ADD COLUMN IF NOT EXISTS foto_url2 text"))
        _con.execute(text("ALTER TABLE hato.fincas ADD COLUMN IF NOT EXISTS caracterizacion text"))
except Exception as _e:
    print("migracion:", _e)
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()   # el token se manda como  Authorization: Bearer <token>

app = FastAPI(title="Hato Sano API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # en producción, poner el dominio real de la app
    allow_methods=["*"], allow_headers=["*"],
)

# ---------------- auth helpers ----------------
def crear_token(usuario_id, finca_id) -> str:
    exp = datetime.utcnow() + timedelta(hours=TOKEN_HORAS)
    return jwt.encode({"sub": str(usuario_id), "finca": str(finca_id), "exp": exp}, JWT_SECRET, algorithm=JWT_ALG)

def usuario_actual(cred: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        data = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        raise HTTPException(401, "Sesión inválida o expirada")
    return {"usuario_id": data["sub"], "finca_id": data["finca"]}

def _animal_de_finca(con, animal_id, finca_id):
    row = con.execute(text("SELECT id FROM hato.animales WHERE id=:a AND finca_id=:f"),
                      {"a": animal_id, "f": finca_id}).first()
    if not row:
        raise HTTPException(404, "Animal no encontrado en tu finca")

# ---------------- modelos ----------------
class Registro(BaseModel):
    finca: str
    nombre: str
    email: str
    password: str = Field(min_length=6)

class Login(BaseModel):
    email: str
    password: str

class AnimalIn(BaseModel):
    arete: Optional[str] = None
    nombre: Optional[str] = None
    tipo: Optional[str] = None
    sexo: Optional[str] = None
    categoria: Optional[str] = None
    foto_url: Optional[str] = None
    foto_url2: Optional[str] = None
    descripcion: Optional[str] = None
    peso_kg: Optional[float] = None

class PesajeIn(BaseModel):
    fecha: date
    peso_kg: float
    nota: Optional[str] = None

class TratamientoIn(BaseModel):
    animal_id: Optional[str] = None      # None = a todo el lote
    fecha: date
    plaga: Optional[str] = None
    familia: Optional[str] = None
    producto: Optional[str] = None
    activo: Optional[str] = None
    dosis: Optional[str] = None
    retiro_dias: int = 0

class FincaIn(BaseModel):
    municipio: Optional[str] = None
    area_ha: Optional[float] = None
    caracterizacion: Optional[str] = None   # JSON en texto

# ---------------- salud ----------------
@app.get("/")
def salud():
    return {"ok": True, "app": "Hato Sano API"}

# ---------------- hoja de vida pública (sin login) ----------------
@app.get("/publico/animal/{animal_id}")
def publico_animal(animal_id: str):
    with engine.begin() as con:
        a = con.execute(text("SELECT id,finca_id,arete,nombre,tipo,sexo,categoria,foto_url,foto_url2,descripcion,peso_kg FROM hato.animales WHERE id=:a AND activo"),
                        {"a": animal_id}).mappings().first()
        if not a:
            raise HTTPException(404, "Hoja de vida no encontrada")
        fn = con.execute(text("SELECT nombre FROM hato.fincas WHERE id=:f"), {"f": a["finca_id"]}).scalar()
        pes = con.execute(text("SELECT fecha,peso_kg FROM hato.pesajes WHERE animal_id=:a ORDER BY fecha"),
                          {"a": animal_id}).mappings().all()
        trs = con.execute(text("SELECT fecha,plaga,familia,producto,animal_id,retiro_dias,apta_venta FROM hato.tratamientos WHERE (animal_id=:a OR (animal_id IS NULL AND finca_id=:f)) ORDER BY fecha DESC"),
                          {"a": animal_id, "f": a["finca_id"]}).mappings().all()
    d = dict(a); d.pop("finca_id", None)
    return {"animal": d, "finca": fn, "pesajes": [dict(x) for x in pes], "tratamientos": [dict(x) for x in trs]}

# ---------------- finca (caracterización) ----------------
@app.get("/finca")
def get_finca(u=Depends(usuario_actual)):
    with engine.begin() as con:
        row = con.execute(text("SELECT id,nombre,municipio,vereda,area_ha,plan,caracterizacion FROM hato.fincas WHERE id=:f"),
                          {"f": u["finca_id"]}).mappings().first()
    return dict(row) if row else {}

@app.put("/finca")
def put_finca(d: FincaIn, u=Depends(usuario_actual)):
    with engine.begin() as con:
        con.execute(text("""UPDATE hato.fincas SET
              municipio=COALESCE(:m,municipio), area_ha=COALESCE(:a,area_ha), caracterizacion=COALESCE(:c,caracterizacion)
              WHERE id=:f"""),
              {"m": d.municipio, "a": d.area_ha, "c": d.caracterizacion, "f": u["finca_id"]})
    return {"ok": True}

# ---------------- autenticación ----------------
@app.post("/auth/registro")
def registro(d: Registro):
    with engine.begin() as con:
        if con.execute(text("SELECT 1 FROM hato.usuarios WHERE email=:e"), {"e": d.email}).first():
            raise HTTPException(400, "Ese correo ya está registrado")
        finca_id = con.execute(text("INSERT INTO hato.fincas(nombre) VALUES(:n) RETURNING id"),
                               {"n": d.finca}).scalar()
        uid = con.execute(text("""INSERT INTO hato.usuarios(finca_id,nombre,email,password_hash,rol)
                                  VALUES(:f,:n,:e,:p,'dueno') RETURNING id"""),
                          {"f": finca_id, "n": d.nombre, "e": d.email, "p": pwd.hash(d.password)}).scalar()
    return {"token": crear_token(uid, finca_id), "finca": d.finca, "nombre": d.nombre}

@app.post("/auth/login")
def login(d: Login):
    with engine.begin() as con:
        row = con.execute(text("SELECT id,finca_id,nombre,password_hash FROM hato.usuarios WHERE email=:e AND activo"),
                          {"e": d.email}).first()
    if not row or not pwd.verify(d.password, row.password_hash):
        raise HTTPException(401, "Correo o contraseña incorrectos")
    return {"token": crear_token(row.id, row.finca_id), "nombre": row.nombre}

# ---------------- animales ----------------
@app.get("/animales")
def listar_animales(u=Depends(usuario_actual)):
    with engine.begin() as con:
        rows = con.execute(text("""SELECT * FROM hato.animales
                                   WHERE finca_id=:f AND activo ORDER BY creado_en DESC"""),
                           {"f": u["finca_id"]}).mappings().all()
    return [dict(r) for r in rows]

@app.post("/animales")
def crear_animal(a: AnimalIn, u=Depends(usuario_actual)):
    with engine.begin() as con:
        aid = con.execute(text("""INSERT INTO hato.animales
              (finca_id,arete,nombre,tipo,sexo,categoria,foto_url,foto_url2,descripcion,peso_kg)
              VALUES(:f,:arete,:nombre,:tipo,:sexo,:cat,:foto,:foto2,:desc,:peso) RETURNING id"""),
              {"f": u["finca_id"], "arete": a.arete, "nombre": a.nombre, "tipo": a.tipo, "sexo": a.sexo,
               "cat": a.categoria, "foto": a.foto_url, "foto2": a.foto_url2, "desc": a.descripcion, "peso": a.peso_kg}).scalar()
    return {"id": str(aid)}

@app.put("/animales/{animal_id}")
def editar_animal(animal_id: str, a: AnimalIn, u=Depends(usuario_actual)):
    with engine.begin() as con:
        _animal_de_finca(con, animal_id, u["finca_id"])
        con.execute(text("""UPDATE hato.animales SET
              arete=:arete,nombre=:nombre,tipo=:tipo,sexo=:sexo,categoria=:cat,
              foto_url=:foto,foto_url2=:foto2,descripcion=:desc,peso_kg=COALESCE(:peso,peso_kg),actualizado_en=now()
              WHERE id=:id AND finca_id=:f"""),
              {"id": animal_id, "f": u["finca_id"], "arete": a.arete, "nombre": a.nombre, "tipo": a.tipo,
               "sexo": a.sexo, "cat": a.categoria, "foto": a.foto_url, "foto2": a.foto_url2, "desc": a.descripcion, "peso": a.peso_kg})
    return {"ok": True}

@app.delete("/animales/{animal_id}")
def baja_animal(animal_id: str, u=Depends(usuario_actual)):
    with engine.begin() as con:
        _animal_de_finca(con, animal_id, u["finca_id"])
        con.execute(text("UPDATE hato.animales SET activo=false,actualizado_en=now() WHERE id=:id"),
                    {"id": animal_id})
    return {"ok": True}

# ---------------- pesajes / GDP ----------------
@app.get("/animales/{animal_id}/pesajes")
def listar_pesajes(animal_id: str, u=Depends(usuario_actual)):
    with engine.begin() as con:
        _animal_de_finca(con, animal_id, u["finca_id"])
        rows = con.execute(text("SELECT * FROM hato.pesajes WHERE animal_id=:a ORDER BY fecha"),
                           {"a": animal_id}).mappings().all()
    return [dict(r) for r in rows]

@app.post("/animales/{animal_id}/pesajes")
def agregar_pesaje(animal_id: str, p: PesajeIn, u=Depends(usuario_actual)):
    with engine.begin() as con:
        _animal_de_finca(con, animal_id, u["finca_id"])
        con.execute(text("INSERT INTO hato.pesajes(animal_id,fecha,peso_kg,nota) VALUES(:a,:fe,:kg,:n)"),
                    {"a": animal_id, "fe": p.fecha, "kg": p.peso_kg, "n": p.nota})
        # el peso actual del animal = el pesaje más reciente
        ultimo = con.execute(text("SELECT peso_kg FROM hato.pesajes WHERE animal_id=:a ORDER BY fecha DESC LIMIT 1"),
                             {"a": animal_id}).scalar()
        con.execute(text("UPDATE hato.animales SET peso_kg=:kg,actualizado_en=now() WHERE id=:a"),
                    {"kg": ultimo, "a": animal_id})
    return {"ok": True}

# ---------------- tratamientos ----------------
@app.get("/tratamientos")
def listar_tratamientos(u=Depends(usuario_actual)):
    with engine.begin() as con:
        rows = con.execute(text("SELECT * FROM hato.tratamientos WHERE finca_id=:f ORDER BY fecha DESC"),
                           {"f": u["finca_id"]}).mappings().all()
    return [dict(r) for r in rows]

@app.post("/tratamientos")
def crear_tratamiento(t: TratamientoIn, u=Depends(usuario_actual)):
    apta_venta = t.fecha + timedelta(days=t.retiro_dias) if t.retiro_dias > 0 else None
    proxima = t.fecha + timedelta(days=365) if (t.plaga == "rabia") else None
    with engine.begin() as con:
        if t.animal_id:
            _animal_de_finca(con, t.animal_id, u["finca_id"])
        tid = con.execute(text("""INSERT INTO hato.tratamientos
              (finca_id,animal_id,fecha,plaga,familia,producto,activo,dosis,retiro_dias,apta_venta,proxima_dosis)
              VALUES(:f,:a,:fe,:plaga,:fam,:prod,:act,:dosis,:ret,:apta,:prox) RETURNING id"""),
              {"f": u["finca_id"], "a": t.animal_id, "fe": t.fecha, "plaga": t.plaga, "fam": t.familia,
               "prod": t.producto, "act": t.activo, "dosis": t.dosis, "ret": t.retiro_dias,
               "apta": apta_venta, "prox": proxima}).scalar()
    return {"id": str(tid), "apta_venta": apta_venta, "proxima_dosis": proxima}

@app.delete("/tratamientos/{trat_id}")
def borrar_tratamiento(trat_id: str, u=Depends(usuario_actual)):
    with engine.begin() as con:
        con.execute(text("DELETE FROM hato.tratamientos WHERE id=:id AND finca_id=:f"),
                    {"id": trat_id, "f": u["finca_id"]})
    return {"ok": True}
