"""
Hato Sano — API (FastAPI)
Backend: login por finca (con verificación de correo, recuperación de clave y
bloqueo por intentos) + suscripción (prueba gratis -> solo lectura al vencer) +
panel admin para activar por pago manual (Nequi) + endpoints del ganado.
Se conecta al esquema "hato" del Postgres-xgPP (el mismo de Vende Putumayo).
"""
import os
import ssl
import json
import smtplib
import secrets
import urllib.request
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header
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

JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET or JWT_SECRET == "cambia-esta-clave":
    # El repo es público: NUNCA arrancar con la clave por defecto. Pon una clave
    # larga y secreta en las variables de entorno de Render (JWT_SECRET).
    raise RuntimeError("Configura una JWT_SECRET propia y secreta en las variables de entorno.")
JWT_ALG = "HS256"
TOKEN_HORAS = 24 * 30                                   # sesión de 30 días

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")             # clave del panel de activación manual
APP_URL = os.environ.get("APP_URL", "https://hatosano-app.vercel.app")
TRIAL_DIAS = int(os.environ.get("TRIAL_DIAS", "30"))    # días de prueba gratis al registrarse

# Envío de correos (verificación / recuperación). Soporta Resend o SMTP (Gmail).
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER or "no-reply@hatosano.app")
FROM_NOMBRE = os.environ.get("FROM_NOMBRE", "Hato Sano")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_CONFIGURADO = bool((SMTP_USER and SMTP_PASS) or RESEND_API_KEY)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# migraciones idempotentes (aditivas: no tocan datos existentes)
_MIGRACIONES = [
    "ALTER TABLE hato.animales ADD COLUMN IF NOT EXISTS foto_url2 text",
    "ALTER TABLE hato.fincas ADD COLUMN IF NOT EXISTS caracterizacion text",
    # suscripción
    "ALTER TABLE hato.fincas ADD COLUMN IF NOT EXISTS estado_suscripcion text NOT NULL DEFAULT 'trial'",
    "ALTER TABLE hato.fincas ADD COLUMN IF NOT EXISTS trial_fin date",
    "ALTER TABLE hato.fincas ADD COLUMN IF NOT EXISTS pago_hasta date",
    # las fincas que ya existían (antes de la suscripción) quedan sin límite práctico
    "UPDATE hato.fincas SET trial_fin = (CURRENT_DATE + 3650) WHERE trial_fin IS NULL",
    # verificación de correo / recuperación / anti fuerza-bruta
    # email_verificado por DEFECTO true -> los usuarios ya existentes quedan verificados;
    # los NUEVOS se crean en false (cuando hay correo configurado) desde /auth/registro.
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS email_verificado boolean NOT NULL DEFAULT true",
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS verif_token text",
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS verif_expira timestamptz",
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS reset_token text",
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS reset_expira timestamptz",
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS intentos integer NOT NULL DEFAULT 0",
    "ALTER TABLE hato.usuarios ADD COLUMN IF NOT EXISTS bloqueado_hasta timestamptz",
]
try:
    with engine.begin() as _con:
        for _sql in _MIGRACIONES:
            _con.execute(text(_sql))
except Exception as _e:
    print("migracion:", _e)

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()   # el token se manda como  Authorization: Bearer <token>

app = FastAPI(title="Hato Sano API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Bearer en header (no cookies): CORS abierto no filtra sesiones
    allow_methods=["*"], allow_headers=["*"],
)

# ---------------- correos ----------------
def _enviar_email(destino: str, asunto: str, html: str) -> bool:
    """Envía un correo por Resend (si hay API key) o por SMTP (Gmail). Devuelve
    True si se envió. Si no hay nada configurado, devuelve False (no rompe nada)."""
    remitente = f"{FROM_NOMBRE} <{FROM_EMAIL}>"
    if RESEND_API_KEY:
        try:
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps({"from": remitente, "to": [destino], "subject": asunto, "html": html}).encode(),
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception as e:
            print("resend error:", e)
    if SMTP_USER and SMTP_PASS:
        try:
            msg = EmailMessage()
            msg["Subject"] = asunto
            msg["From"] = remitente
            msg["To"] = destino
            msg.set_content("Este correo se ve mejor en un lector con HTML.")
            msg.add_alternative(html, subtype="html")
            ctx = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
            return True
        except Exception as e:
            print("smtp error:", e)
    return False


def _correo_html(titulo: str, saludo: str, cuerpo: str, boton_txt: str, boton_url: str) -> str:
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:0 auto;color:#1b241e">
<div style="background:#1E5B45;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<h2 style="margin:0;font-size:20px">🐄 Hato Sano</h2></div>
<div style="border:1px solid #DBE0D3;border-top:0;border-radius:0 0 12px 12px;padding:22px">
<h3 style="margin:0 0 8px">{titulo}</h3>
<p style="font-size:14px;line-height:1.5">{saludo}</p>
<p style="font-size:14px;line-height:1.5">{cuerpo}</p>
<p style="text-align:center;margin:22px 0">
<a href="{boton_url}" style="background:#1E5B45;color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;font-size:15px;display:inline-block">{boton_txt}</a></p>
<p style="font-size:12px;color:#8a8a8a">Si el botón no funciona, copia y pega este enlace:<br>{boton_url}</p>
<p style="font-size:12px;color:#8a8a8a">Si no fuiste tú, ignora este correo.</p>
</div></div>"""


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

def _estado_finca(con, finca_id) -> dict:
    row = con.execute(text("SELECT estado_suscripcion,trial_fin,pago_hasta FROM hato.fincas WHERE id=:f"),
                      {"f": finca_id}).mappings().first()
    hoy = date.today()
    if not row:
        return {"estado": "vencida", "dias": 0, "trial_fin": None, "pago_hasta": None}
    pago, trial = row["pago_hasta"], row["trial_fin"]
    if pago and pago >= hoy:
        return {"estado": "activa", "dias": (pago - hoy).days, "trial_fin": trial, "pago_hasta": pago}
    if trial and trial >= hoy:
        return {"estado": "trial", "dias": (trial - hoy).days, "trial_fin": trial, "pago_hasta": pago}
    return {"estado": "vencida", "dias": 0, "trial_fin": trial, "pago_hasta": pago}

def usuario_escritura(u=Depends(usuario_actual)) -> dict:
    """Como usuario_actual, pero bloquea si la prueba/suscripción venció.
    Se usa SOLO en endpoints que AGREGAN datos: leer siempre está permitido."""
    with engine.begin() as con:
        est = _estado_finca(con, u["finca_id"])
    if est["estado"] == "vencida":
        raise HTTPException(402, "Tu prueba/suscripción venció. Renueva para seguir agregando. Tus datos siguen guardados.")
    return u

def _animal_de_finca(con, animal_id, finca_id):
    row = con.execute(text("SELECT id FROM hato.animales WHERE id=:a AND finca_id=:f"),
                      {"a": animal_id, "f": finca_id}).first()
    if not row:
        raise HTTPException(404, "Animal no encontrado en tu finca")

def _admin(x_admin_key: str = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(401, "No autorizado")

# ---------------- modelos ----------------
class Registro(BaseModel):
    finca: str
    nombre: str
    email: str
    password: str = Field(min_length=6)

class Login(BaseModel):
    email: str
    password: str

class EmailIn(BaseModel):
    email: str

class TokenIn(BaseModel):
    token: str

class ResetIn(BaseModel):
    token: str
    password: str = Field(min_length=6)

class ActivarIn(BaseModel):
    finca_id: Optional[str] = None
    email: Optional[str] = None
    meses: int = 1

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
    return {"ok": True, "app": "Hato Sano API", "email": EMAIL_CONFIGURADO}

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
        est = _estado_finca(con, u["finca_id"])
    d = dict(row) if row else {}
    d["suscripcion"] = est
    return d

@app.put("/finca")
def put_finca(d: FincaIn, u=Depends(usuario_escritura)):
    with engine.begin() as con:
        con.execute(text("""UPDATE hato.fincas SET
              municipio=COALESCE(:m,municipio), area_ha=COALESCE(:a,area_ha), caracterizacion=COALESCE(:c,caracterizacion)
              WHERE id=:f"""),
              {"m": d.municipio, "a": d.area_ha, "c": d.caracterizacion, "f": u["finca_id"]})
    return {"ok": True}

# ---------------- autenticación ----------------
@app.post("/auth/registro")
def registro(d: Registro):
    email = d.email.strip().lower()
    with engine.begin() as con:
        if con.execute(text("SELECT 1 FROM hato.usuarios WHERE lower(email)=:e"), {"e": email}).first():
            raise HTTPException(400, "Ese correo ya está registrado")
        finca_id = con.execute(text("INSERT INTO hato.fincas(nombre,estado_suscripcion,trial_fin) VALUES(:n,'trial',:t) RETURNING id"),
                               {"n": d.finca, "t": date.today() + timedelta(days=TRIAL_DIAS)}).scalar()
        verificado = not EMAIL_CONFIGURADO   # sin correo configurado -> se crea verificado (no bloquea el uso)
        verif = None if verificado else secrets.token_urlsafe(32)
        vexp = None if verificado else datetime.utcnow() + timedelta(days=2)
        uid = con.execute(text("""INSERT INTO hato.usuarios(finca_id,nombre,email,password_hash,rol,email_verificado,verif_token,verif_expira)
                                  VALUES(:f,:n,:e,:p,'dueno',:ver,:vt,:vex) RETURNING id"""),
                          {"f": finca_id, "n": d.nombre, "e": email, "p": pwd.hash(d.password),
                           "ver": verificado, "vt": verif, "vex": vexp}).scalar()
    if not verificado:
        link = f"{APP_URL}/#verif={verif}"
        html = _correo_html("Confirma tu cuenta",
                            f"Hola {d.nombre},",
                            "Gracias por registrar tu finca en Hato Sano. Confirma tu correo para activar tu cuenta y empezar tu prueba gratis de 30 días.",
                            "Confirmar mi cuenta", link)
        _enviar_email(email, "Confirma tu cuenta — Hato Sano", html)
        return {"pendiente_verificacion": True, "email": email}
    return {"token": crear_token(uid, finca_id), "finca": d.finca, "nombre": d.nombre}

@app.post("/auth/login")
def login(d: Login):
    email = d.email.strip().lower()
    with engine.begin() as con:
        row = con.execute(text("SELECT id,finca_id,nombre,password_hash,email_verificado,intentos,bloqueado_hasta FROM hato.usuarios WHERE lower(email)=:e AND activo"),
                          {"e": email}).mappings().first()
    now = datetime.utcnow()
    if row and row["bloqueado_hasta"] and row["bloqueado_hasta"] > now:
        raise HTTPException(429, "Demasiados intentos fallidos. Espera unos minutos e intenta de nuevo.")
    ok = bool(row) and pwd.verify(d.password, row["password_hash"])
    if not ok:
        if row:
            intentos = (row["intentos"] or 0) + 1
            bloq = now + timedelta(minutes=15) if intentos >= 5 else None
            with engine.begin() as con:
                con.execute(text("UPDATE hato.usuarios SET intentos=:i,bloqueado_hasta=:b WHERE id=:id"),
                            {"i": intentos, "b": bloq, "id": row["id"]})
        raise HTTPException(401, "Correo o contraseña incorrectos")
    with engine.begin() as con:
        con.execute(text("UPDATE hato.usuarios SET intentos=0,bloqueado_hasta=NULL WHERE id=:id"), {"id": row["id"]})
    if EMAIL_CONFIGURADO and not row["email_verificado"]:
        raise HTTPException(403, "Verifica tu correo para entrar. Revisa tu bandeja de entrada (y la carpeta de spam).")
    return {"token": crear_token(row["id"], row["finca_id"]), "nombre": row["nombre"]}

@app.post("/auth/verificar")
def verificar(d: TokenIn):
    with engine.begin() as con:
        row = con.execute(text("SELECT id,finca_id,nombre,verif_expira FROM hato.usuarios WHERE verif_token=:t"),
                          {"t": d.token}).mappings().first()
        if not row or (row["verif_expira"] and row["verif_expira"] < datetime.utcnow()):
            raise HTTPException(400, "El enlace de verificación no es válido o expiró. Pide uno nuevo.")
        con.execute(text("UPDATE hato.usuarios SET email_verificado=true,verif_token=NULL,verif_expira=NULL WHERE id=:id"),
                    {"id": row["id"]})
    return {"token": crear_token(row["id"], row["finca_id"]), "nombre": row["nombre"]}

@app.post("/auth/reenviar-verificacion")
def reenviar_verificacion(d: EmailIn):
    email = d.email.strip().lower()
    if EMAIL_CONFIGURADO:
        with engine.begin() as con:
            row = con.execute(text("SELECT id,nombre,email_verificado FROM hato.usuarios WHERE lower(email)=:e AND activo"),
                              {"e": email}).mappings().first()
            if row and not row["email_verificado"]:
                v = secrets.token_urlsafe(32)
                con.execute(text("UPDATE hato.usuarios SET verif_token=:t,verif_expira=:x WHERE id=:id"),
                            {"t": v, "x": datetime.utcnow() + timedelta(days=2), "id": row["id"]})
                html = _correo_html("Confirma tu cuenta", f"Hola {row['nombre']},",
                                    "Aquí tienes de nuevo tu enlace para confirmar tu correo en Hato Sano.",
                                    "Confirmar mi cuenta", f"{APP_URL}/#verif={v}")
                _enviar_email(email, "Confirma tu cuenta — Hato Sano", html)
    return {"ok": True}   # respuesta genérica: no revela si el correo existe

@app.post("/auth/recuperar")
def recuperar(d: EmailIn):
    email = d.email.strip().lower()
    if EMAIL_CONFIGURADO:
        with engine.begin() as con:
            row = con.execute(text("SELECT id,nombre FROM hato.usuarios WHERE lower(email)=:e AND activo"),
                              {"e": email}).mappings().first()
            if row:
                r = secrets.token_urlsafe(32)
                con.execute(text("UPDATE hato.usuarios SET reset_token=:t,reset_expira=:x WHERE id=:id"),
                            {"t": r, "x": datetime.utcnow() + timedelta(hours=2), "id": row["id"]})
                html = _correo_html("Recupera tu contraseña", f"Hola {row['nombre']},",
                                    "Pediste restablecer tu contraseña. El enlace vence en 2 horas.",
                                    "Cambiar mi contraseña", f"{APP_URL}/#reset={r}")
                _enviar_email(email, "Recupera tu contraseña — Hato Sano", html)
    return {"ok": True}

@app.post("/auth/reset")
def reset(d: ResetIn):
    with engine.begin() as con:
        row = con.execute(text("SELECT id FROM hato.usuarios WHERE reset_token=:t AND reset_expira>:n"),
                          {"t": d.token, "n": datetime.utcnow()}).first()
        if not row:
            raise HTTPException(400, "El enlace no es válido o expiró. Pide uno nuevo.")
        con.execute(text("UPDATE hato.usuarios SET password_hash=:p,reset_token=NULL,reset_expira=NULL,intentos=0,bloqueado_hasta=NULL WHERE id=:id"),
                    {"p": pwd.hash(d.password), "id": row[0]})
    return {"ok": True}

# ---------------- animales ----------------
@app.get("/animales")
def listar_animales(u=Depends(usuario_actual)):
    with engine.begin() as con:
        rows = con.execute(text("""SELECT * FROM hato.animales
                                   WHERE finca_id=:f AND activo ORDER BY creado_en DESC"""),
                           {"f": u["finca_id"]}).mappings().all()
    return [dict(r) for r in rows]

@app.post("/animales")
def crear_animal(a: AnimalIn, u=Depends(usuario_escritura)):
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
def agregar_pesaje(animal_id: str, p: PesajeIn, u=Depends(usuario_escritura)):
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
def crear_tratamiento(t: TratamientoIn, u=Depends(usuario_escritura)):
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

# ---------------- panel admin (activación manual por pago Nequi) ----------------
@app.get("/admin/fincas")
def admin_fincas(_=Depends(_admin)):
    with engine.begin() as con:
        rows = con.execute(text("""SELECT f.id,f.nombre,f.trial_fin,f.pago_hasta,f.plan,
              (SELECT email FROM hato.usuarios WHERE finca_id=f.id ORDER BY creado_en LIMIT 1) AS email,
              (SELECT nombre FROM hato.usuarios WHERE finca_id=f.id ORDER BY creado_en LIMIT 1) AS dueno,
              (SELECT count(*) FROM hato.animales WHERE finca_id=f.id AND activo) AS animales,
              f.creada_en
              FROM hato.fincas f ORDER BY f.creada_en DESC""")).mappings().all()
    hoy = date.today()
    out = []
    for r in rows:
        d = dict(r)
        if r["pago_hasta"] and r["pago_hasta"] >= hoy:
            d["estado"] = "activa"
        elif r["trial_fin"] and r["trial_fin"] >= hoy:
            d["estado"] = "trial"
        else:
            d["estado"] = "vencida"
        d["id"] = str(r["id"])
        out.append(d)
    return out

@app.post("/admin/activar")
def admin_activar(d: ActivarIn, _=Depends(_admin)):
    with engine.begin() as con:
        fid = d.finca_id
        if not fid and d.email:
            fid = con.execute(text("SELECT finca_id FROM hato.usuarios WHERE lower(email)=:e"),
                              {"e": d.email.strip().lower()}).scalar()
        if not fid:
            raise HTTPException(404, "Finca no encontrada")
        row = con.execute(text("SELECT pago_hasta FROM hato.fincas WHERE id=:f"), {"f": fid}).mappings().first()
        if not row:
            raise HTTPException(404, "Finca no encontrada")
        base = row["pago_hasta"] if (row["pago_hasta"] and row["pago_hasta"] > date.today()) else date.today()
        nuevo = base + timedelta(days=30 * max(1, d.meses))
        con.execute(text("UPDATE hato.fincas SET pago_hasta=:p,estado_suscripcion='activa',plan='pro' WHERE id=:f"),
                    {"p": nuevo, "f": fid})
    return {"ok": True, "finca_id": str(fid), "pago_hasta": str(nuevo)}

@app.post("/admin/verificar-correo")
def admin_verificar_correo(d: EmailIn, _=Depends(_admin)):
    with engine.begin() as con:
        con.execute(text("UPDATE hato.usuarios SET email_verificado=true,verif_token=NULL,verif_expira=NULL WHERE lower(email)=:e"),
                    {"e": d.email.strip().lower()})
    return {"ok": True}
