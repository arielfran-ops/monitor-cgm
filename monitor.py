"""
Monitor de glucosa (Nightscout -> Telegram)

Uso:
  python monitor.py revisar               -> revision periodica (cada 2 min desde cron-job.org)
  python monitor.py reporte [AAAA-MM-DD]  -> reporte de un dia completo (por defecto, ayer)

Datos sensibles: todos llegan como "secrets" de GitHub, nunca estan en el codigo.
  NS_URL, NS_TOKEN, TG_TOKEN, TG_CHAT_ID, NOMBRE
"""

import hashlib
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

ARG = timezone(timedelta(hours=-3))  # Argentina, sin horario de verano

# ----- Reglas (cambialas aca) -----
ALTO = 180                  # mg/dL
MINUTOS_ALTO = 60           # tiempo continuo por encima de ALTO para avisar
UMBRAL_BAJADA = 150         # mg/dL: con flecha de bajada rapida -> aviso
HIPO = 70                   # mg/dL: por debajo, aviso siempre
MINUTOS_SIN_DATOS = 30      # sensor sin datos
RECORDATORIO_MIN = 20       # recordatorios de altas y bajas
RECORDATORIO_FALLAS_MIN = 60  # recordatorios de sensor sin datos / Nightscout caido
HORA_REPORTE = 9
BAJO, MUY_ALTO, MUY_BAJO = 70, 250, 54

RAPIDO = {"SingleDown", "DoubleDown"}
BAJANDO = RAPIDO | {"FortyFiveDown"}
FLECHAS = {"DoubleUp": "⇈", "SingleUp": "↑", "FortyFiveUp": "↗", "Flat": "→",
           "FortyFiveDown": "↘", "SingleDown": "↓", "DoubleDown": "⇊"}

NS_URL = os.environ["NS_URL"].strip().rstrip("/")
NS_TOKEN = os.environ["NS_TOKEN"].strip()
TG_TOKEN = os.environ["TG_TOKEN"].strip()
TG_CHAT_ID = os.environ["TG_CHAT_ID"].strip()
NOMBRE = os.environ.get("NOMBRE", "").strip() or "Paciente"
ESTADO = os.environ.get("ESTADO", "estado/estado.json")


class NightscoutCaido(Exception):
    pass


# ---------- utilidades ----------

def telegram(texto):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": texto}, timeout=30)
    if not r.ok:
        raise SystemExit(f"Telegram rechazó el mensaje: {r.json().get('description')}")


def lecturas(desde, hasta=None, cantidad=2000):
    """Lecturas entre `desde` y `hasta` (ms epoch), de la mas vieja a la mas nueva."""
    url = f"{NS_URL}/api/v1/entries/sgv.json"
    params = {"count": cantidad, "find[date][$gte]": desde}
    if hasta is not None:
        params["find[date][$lt]"] = hasta
    try:
        r = requests.get(url, params={**params, "token": NS_TOKEN}, timeout=30)
        if r.status_code == 401:  # no es token de acceso: probar como API_SECRET
            secreto = hashlib.sha1(NS_TOKEN.encode()).hexdigest()
            r = requests.get(url, params=params, headers={"api-secret": secreto}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        codigo = getattr(getattr(e, "response", None), "status_code", None)
        motivo = {402: "servicio pausado por falta de pago (ns.10be.de)",
                  401: "la clave fue rechazada"}.get(codigo, "no responde")
        raise NightscoutCaido(motivo)
    datos = [e for e in r.json() if e.get("sgv")]
    return sorted(datos, key=lambda e: e["date"])


def flecha(e):
    return FLECHAS.get(e.get("direction", ""), "")


def hora(ms):
    return datetime.fromtimestamp(ms / 1000, ARG).strftime("%H:%M")


def duracion(ms):
    m = int(ms // 60000)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def cargar_estado():
    try:
        with open(ESTADO) as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(estado):
    os.makedirs(os.path.dirname(ESTADO) or ".", exist_ok=True)
    with open(ESTADO, "w") as f:
        json.dump(estado, f)


def episodio(estado, clave, activo, inicio, recordatorio, fin, cada_min, ahora):
    """Avisa al empezar, recuerda cada `cada_min` mientras siga, y avisa al terminar."""
    eps = estado.setdefault("episodios", {})
    ep = eps.get(clave)
    if activo:
        if not ep:
            telegram(inicio)
            eps[clave] = {"desde": ahora, "aviso": ahora}
        elif ahora - ep["aviso"] >= cada_min * 60000 - 45000:  # margen por demoras
            telegram(recordatorio(ahora - ep["desde"]))
            ep["aviso"] = ahora
    elif ep:
        if fin:
            telegram(fin)
        del eps[clave]


# ---------- revision periodica ----------

def revisar():
    estado = cargar_estado()
    ahora = time.time() * 1000
    try:
        _revisar(estado, ahora)
    finally:
        guardar_estado(estado)


def _revisar(estado, ahora):
    # Reporte diario: una sola vez por dia, en la primera revision desde las 9:00
    hoy = datetime.now(ARG)
    if hoy.hour >= HORA_REPORTE and estado.get("reporte") != hoy.date().isoformat():
        try:
            reporte()
            estado["reporte"] = hoy.date().isoformat()
        except NightscoutCaido:
            pass  # lo avisa el bloque de abajo; se reintenta en la proxima revision

    # Nightscout
    try:
        datos = lecturas(int(ahora - 2 * 3600 * 1000))
        caido = None
    except NightscoutCaido as e:
        datos, caido = [], str(e)
    episodio(estado, "nightscout", caido is not None,
             f"⚠️ No puedo leer el Nightscout de {NOMBRE}: {caido}.\nMientras tanto no hay alertas.",
             lambda d: f"⚠️ Nightscout sigue sin responder ({duracion(d)}): {caido}.",
             f"✅ Nightscout de {NOMBRE} volvió a responder.",
             RECORDATORIO_FALLAS_MIN, ahora)
    if caido:
        return

    # Sensor sin datos
    sin_datos = not datos or ahora - datos[-1]["date"] > MINUTOS_SIN_DATOS * 60000
    ultima_hora = hora(datos[-1]["date"]) if datos else "hace más de 2 horas"
    episodio(estado, "sin_datos", sin_datos,
             f"⚠️ {NOMBRE}: no llegan datos del sensor desde las {ultima_hora}.",
             lambda d: f"⚠️ {NOMBRE}: sigue sin datos del sensor (última lectura {ultima_hora}).",
             f"✅ {NOMBRE}: volvieron a llegar datos del sensor.",
             RECORDATORIO_FALLAS_MIN, ahora)
    if sin_datos:
        return

    u = datos[-1]
    valor, dir_ = u["sgv"], u.get("direction", "")
    ahora_txt = f"{valor} mg/dL {flecha(u)}".strip()

    # Baja: empieza con hipo o bajada rapida <= 150; sigue mientras baje o este en hipo
    ya_baja = "baja" in estado.get("episodios", {})
    baja = valor < HIPO or (valor <= UMBRAL_BAJADA and dir_ in (BAJANDO if ya_baja else RAPIDO))
    inicio_baja = (f"🚨 {NOMBRE}: {ahora_txt}, por debajo de {HIPO}." if valor < HIPO
                   else f"🔻 {NOMBRE}: {ahora_txt} y bajando rápido. Revisalo.")
    episodio(estado, "baja", baja, inicio_baja,
             lambda d: f"🔻 Recordatorio baja ({duracion(d)}): {NOMBRE} está en {ahora_txt}.",
             f"✅ {NOMBRE}: la baja se estabilizó. Ahora {ahora_txt}.",
             RECORDATORIO_MIN, ahora)

    # Alta: empieza con 1 hora completa por encima de 180; sigue mientras este arriba de 180
    ya_alta = "alta" in estado.get("episodios", {})
    if ya_alta:
        alta = valor > ALTO
    else:
        ventana = [e for e in datos if ahora - e["date"] <= MINUTOS_ALTO * 60000]
        cubre = datos[0]["date"] <= ahora - MINUTOS_ALTO * 60000
        alta = cubre and len(ventana) >= 6 and all(e["sgv"] > ALTO for e in ventana)
    episodio(estado, "alta", alta,
             f"🔺 {NOMBRE}: más de {MINUTOS_ALTO} min por encima de {ALTO}. Ahora {ahora_txt}.",
             lambda d: f"🔺 Recordatorio alta ({duracion(d + MINUTOS_ALTO * 60000)} arriba de {ALTO}): {NOMBRE} está en {ahora_txt}.",
             f"✅ {NOMBRE}: volvió a rango. Ahora {ahora_txt}.",
             RECORDATORIO_MIN, ahora)


# ---------- reporte diario ----------

def reporte(fecha=None):
    """Dia calendario completo (00:00 a 24:00 hora Argentina). Por defecto, ayer."""
    if fecha:
        dia = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=ARG)
    else:
        dia = datetime.now(ARG).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    fin = dia + timedelta(days=1)
    datos = lecturas(int(dia.timestamp() * 1000), int(fin.timestamp() * 1000))
    txt_fecha = dia.strftime("%d/%m/%Y")
    valores = [e["sgv"] for e in datos]
    if not valores:
        telegram(f"⚠️ Reporte {NOMBRE} {txt_fecha}: no hay datos de ese día.")
        return
    cobertura = 100 * len({e["date"] // 300000 for e in datos}) / 288
    n = len(valores)
    pct = lambda cond: 100 * sum(1 for v in valores if cond(v)) / n
    prom = statistics.mean(valores)
    de = statistics.pstdev(valores)
    telegram(
        f"📊 Reporte de {NOMBRE} – {txt_fecha} (00:00 a 24:00)\n\n"
        f"Promedio: {prom:.0f} mg/dL\n"
        f"Tiempo en rango (70-180): {pct(lambda v: BAJO <= v <= ALTO):.0f}%\n"
        f"Por encima de 180: {pct(lambda v: v > ALTO):.0f}% (más de 250: {pct(lambda v: v > MUY_ALTO):.0f}%)\n"
        f"Por debajo de 70: {pct(lambda v: v < BAJO):.0f}% (menos de 54: {pct(lambda v: v < MUY_BAJO):.0f}%)\n"
        f"Desvío estándar: {de:.1f} mg/dL\n"
        f"Variabilidad (CV): {100 * de / prom:.0f}%\n"
        f"Mínimo / máximo: {min(valores)} / {max(valores)}\n"
        f"Datos del sensor: {min(cobertura, 100):.0f}% del día\n\n"
        + hba1c_estimada(fin)
    )


def hba1c_estimada(hasta_dia):
    """HbA1c estimada (GMI) con hasta 90 dias: GMI = 3.31 + 0.02392 x promedio."""
    desde = hasta_dia - timedelta(days=90)
    datos = lecturas(int(desde.timestamp() * 1000), int(hasta_dia.timestamp() * 1000), 100000)
    if not datos:
        return "HbA1c estimada: sin datos suficientes"
    prom = statistics.mean(e["sgv"] for e in datos)
    dias = len({datetime.fromtimestamp(e["date"] / 1000, ARG).date() for e in datos})
    nota = "" if dias >= 90 else f" (con {dias} de 90 días: todavía orientativa)"
    return f"HbA1c estimada (90 días): {3.31 + 0.02392 * prom:.1f}%{nota}"


if __name__ == "__main__":
    args = " ".join(sys.argv[1:]).split() or ["revisar"]
    modo = args[0]
    if modo in ("revisar", "alertas"):
        revisar()
    elif modo == "reporte":
        try:
            reporte(args[1] if len(args) > 1 else None)
        except NightscoutCaido as e:
            telegram(f"⚠️ No pude armar el reporte: Nightscout {e}.")
    else:
        raise SystemExit(f"Modo desconocido: {modo}")
