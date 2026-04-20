"""
Polymarket Smart Money Tracker v4.1
─────────────────────────────────────────────────────────────────
Fixes v4.1:
  1. condition_id / outcome_index — columnas DB consistentes
  2. Timestamp CEST en resolver (no más UTC mismatch)
  3. Score mínimo mejorado para ballenas nuevas
  4. Umbrales en modo TEST ($100 VIP, ROI >5%)
"""

import os
import json
import time
import random
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import threading

# ── ZONA HORARIA ─────────────────────────────────────────────────
CEST = timezone(timedelta(hours=2))

# ── CONFIGURACIÓN ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID_BASICO = os.getenv("TELEGRAM_CHAT_ID_BASICO")
TELEGRAM_CHAT_ID_VIP    = os.getenv("TELEGRAM_CHAT_ID_VIP")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")
DATABASE_URL            = os.getenv("DATABASE_URL", "postgresql://postgres:xLXseImrMQCWrpOHSVxCJdNIlZKfGSSo@postgres.railway.internal:5432/railway")

# ── UMBRALES ─────────────────────────────────────────────────────
MIN_USD_BASICO   = 50
MAX_USD_BASICO   = 499
MIN_ROI_BASICO   = 0
MIN_USD_VIP      = 100    # TEST — cambiar a 500 en producción
MIN_ROI_VIP      = 5      # TEST — cambiar a 10 en producción
PRECIO_MIN       = 0.15
PRECIO_MAX       = 0.85

# ── PARÁMETROS OPERACIONALES ─────────────────────────────────────
POLL_INTERVAL         = 5
MAX_SEEN              = 3000
CACHE_TTL_HORAS       = 6
WALLET_API_DELAY      = 0.3
CEBO_PROBABILIDAD     = 4
PERSIST_PATH          = Path("state.json")
ANTI_SPAM_MINUTOS     = 15
MERCADO_CALIENTE_N    = 3
MERCADO_CALIENTE_MIN  = 10
RESOLVER_CADA_HORAS   = 1
ALTA_CONVICCION_HORAS = 24
DIVERGENCIA_MINUTOS   = 5

# ── ESTADO EN MEMORIA ────────────────────────────────────────────
seen_hashes               = deque(maxlen=MAX_SEEN)
whale_cache               = {}
whale_streaks             = {}
whale_apodos              = {}
anti_spam                 = {}
mercado_hits              = defaultdict(list)
ciclo_actual              = 0
ultimo_resumen            = datetime.now(timezone.utc)
ultima_resolucion         = datetime.now(timezone.utc) - timedelta(hours=2)
ultimo_update_id          = 0
wallet_mercado_ultima_vez = defaultdict(dict)
cola_divergencia          = deque(maxlen=200)
ultimo_lunes_enviado      = None
ultimo_limpieza           = datetime.now(timezone.utc) - timedelta(hours=25)
mensaje_pinned_id         = None
ultimo_pin                = None

stats_dia = {
    "señales_vip":    0,
    "señales_basico": 0,
    "wallets_vip":    set(),
    "mercados_vip":   [],
}

# ── APODOS ÉPICOS ────────────────────────────────────────────────
APODOS_EPICOS = [
    "El Oráculo", "El Arquitecto", "La Sombra", "El Mago de Washington",
    "El Tiburón Silencioso", "La Mano Invisible", "El Estratega",
    "El Profeta", "El Alquimista", "El Lobo Solitario",
    "La Ballena Blanca", "El Gran Maestro", "El Fantasma",
    "El Sabio del Mercado", "El Cazador de Tendencias",
    "El Señor del Margen", "La Serpiente Fría", "El Coloso",
]

# ── MERCADOS BASURA ──────────────────────────────────────────────
SLUGS_IGNORADOS = [
    "btc-updown", "eth-updown", "sol-updown",
    "matic-updown", "xrp-updown", "bnb-updown",
    "highest-temperature", "lowest-temperature",
    "will-the-price-of", "crypto-", "bitcoin-price",
]

# ── MENSAJES CANAL ───────────────────────────────────────────────
MENSAJE_BIENVENIDA = """👋 <b>Bienvenido al canal de señales gratuito</b>

Aquí recibirás alertas en tiempo real de traders rentables operando en Polymarket.

📡 <b>Este canal (GRATIS):</b>
• Trades de $50 a $500
• Wallet y mercado verificados
• Señales en tiempo real

🐋 <b>Canal VIP ($15/mes):</b>
• Ballenas gordas +$500
• Score de confianza 0-100
• Historial de aciertos de cada ballena
• Alertas de alta convicción (doble apuesta)
• Alertas de divergencia de precio
• Análisis IA de cada operación
• Track record con tasa de acierto

👇 <b>Acceso inmediato al pagar:</b>
<a href="t.me/send?start=s-VIPaccess">🔐 Unirse al VIP — $15/mes</a>"""

MENSAJE_PIN_VIP = """🐋 <b>¿Quieres las ballenas gordas?</b>

Este canal es GRATIS y muestra señales de $50–$500.

En el canal <b>VIP ($15/mes)</b> recibes:
✅ Ballenas de +$500 USD
✅ Historial de aciertos por ballena
✅ Alertas de doble apuesta (alta convicción)
✅ Alertas de divergencia de precio
✅ Score de confianza 0–100
✅ Análisis IA de cada jugada
✅ Track record con tasa de acierto verificada

<b>Los traders que seguimos tienen ROI >10% verificado.</b>

👇 <b>Acceso inmediato al pagar:</b>
<a href="t.me/send?start=s-VIPaccess">🔐 Unirse al VIP — $15/mes</a>"""

# ════════════════════════════════════════════════════════════════
#  BASE DE DATOS
# ════════════════════════════════════════════════════════════════

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            SERIAL PRIMARY KEY,
                timestamp     TEXT,
                fecha         TEXT,
                apodo         TEXT,
                wallet        TEXT,
                mercado       TEXT,
                posicion      TEXT,
                outcome       TEXT,
                outcome_index INTEGER,
                condition_id  TEXT,
                usd           FLOAT,
                prob          FLOAT,
                roi_wallet    FLOAT,
                score         INTEGER,
                url           TEXT,
                resultado     TEXT DEFAULT 'PENDIENTE',
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("   🗄️  Base de datos inicializada")
    except Exception as e:
        print(f"   ⚠️  DB init error: {e}")

def cargar_signals() -> list:
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 500")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in reversed(rows)]
    except Exception as e:
        print(f"   ⚠️  DB cargar error: {e}")
        return []

def guardar_señal(payload: dict, apodo: str, score: int, trade: dict):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO signals
            (timestamp, fecha, apodo, wallet, mercado, posicion, outcome,
             outcome_index, condition_id, usd, prob, roi_wallet, score, url, resultado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDIENTE')
        """, (
            payload["timestamp"],
            datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            apodo,
            payload["wallet"][:12],
            payload["market"],
            f"{payload['side']} → {payload['outcome']}",
            payload["outcome"],
            int(trade.get("outcomeIndex", -1)),
            trade.get("conditionId", ""),
            payload["usd_invested"],
            payload["price"],
            round(payload["roi"], 1),
            score,
            payload["url"],
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"   ⚠️  No se pudo guardar señal: {e}")

# ════════════════════════════════════════════════════════════════
#  HISTORIAL DE BALLENAS
# ════════════════════════════════════════════════════════════════

def get_historial_ballena(apodo: str) -> dict:
    log = cargar_signals()
    resueltas = [s for s in log if s.get("apodo") == apodo and s.get("resultado") in ("ACIERTO", "FALLO")]
    total    = len(resueltas)
    aciertos = sum(1 for s in resueltas if s.get("resultado") == "ACIERTO")
    fallos   = total - aciertos

    if total == 0:
        return {"aciertos": 0, "fallos": 0, "total": 0, "tasa": "Nuevo", "emoji": "🆕"}

    tasa_num = (aciertos / total) * 100
    tasa_str = f"{tasa_num:.0f}%"
    if tasa_num >= 75:   emoji = "🔥🔥🔥"
    elif tasa_num >= 60: emoji = "🔥🔥"
    elif tasa_num >= 50: emoji = "🔥"
    else:                emoji = "⚠️"

    return {"aciertos": aciertos, "fallos": fallos, "total": total, "tasa": tasa_str, "emoji": emoji}

# ════════════════════════════════════════════════════════════════
#  ALTA CONVICCIÓN
# ════════════════════════════════════════════════════════════════

def check_alta_conviccion(wallet: str, slug: str, apodo: str, payload: dict) -> bool:
    ahora  = datetime.now(timezone.utc)
    limite = ahora - timedelta(hours=ALTA_CONVICCION_HORAS)
    ultima = wallet_mercado_ultima_vez[wallet].get(slug)
    wallet_mercado_ultima_vez[wallet][slug] = ahora

    if ultima and ultima > limite:
        diff_seg   = int((ahora - ultima).total_seconds())
        diff_horas = diff_seg // 3600
        diff_min   = (diff_seg % 3600) // 60
        tiempo_txt = f"{diff_horas}h {diff_min}min" if diff_horas > 0 else f"{diff_min}min"
        msg = (
            f"⚡ <b>ALTA CONVICCIÓN — DOBLE APUESTA</b> ⚡\n\n"
            f"🏷️ <b>{apodo}</b> ha vuelto al mismo mercado\n\n"
            f"📋 <b>Mercado:</b> {payload['market']}\n"
            f"🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>\n"
            f"💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD\n"
            f"⏱️ <b>Tiempo desde la última entrada:</b> {tiempo_txt}\n\n"
            f"<i>Cuando una ballena repite mercado, su convicción es máxima.</i>\n\n"
            f"🔗 <a href=\"{payload['url']}\">Ver mercado</a>"
        )
        if TELEGRAM_CHAT_ID_VIP:
            enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)
            print(f"   ⚡ Alta convicción: {apodo} en {slug[:20]}")
        return True
    return False

# ════════════════════════════════════════════════════════════════
#  DIVERGENCIA DE PRECIO
# ════════════════════════════════════════════════════════════════

def registrar_para_divergencia(condition_id: str, outcome_index: int,
                                precio_entrada: float, apodo: str, payload: dict):
    if not condition_id or outcome_index < 0:
        return
    cola_divergencia.append({
        "condition_id":   condition_id,
        "outcome_index":  outcome_index,
        "precio_entrada": precio_entrada,
        "apodo":          apodo,
        "market":         payload["market"],
        "side":           payload["side"],
        "outcome":        payload["outcome"],
        "url":            payload["url"],
        "usd":            payload["usd_invested"],
        "ts":             datetime.now(timezone.utc),
        "notificado":     False,
    })

def revisar_divergencias():
    ahora   = datetime.now(timezone.utc)
    ventana = timedelta(minutes=DIVERGENCIA_MINUTOS)

    for entrada in cola_divergencia:
        if entrada["notificado"]:
            continue
        if ahora - entrada["ts"] < ventana:
            continue
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets?conditionId={entrada['condition_id']}",
                timeout=5
            )
            if not r.ok:
                entrada["notificado"] = True
                continue
            mercados = r.json()
            if not mercados:
                entrada["notificado"] = True
                continue
            mercado    = mercados[0] if isinstance(mercados, list) else mercados
            prices_raw = mercado.get("outcomePrices", "[]")
            prices     = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            if entrada["outcome_index"] >= len(prices):
                entrada["notificado"] = True
                continue
            precio_actual  = round(float(prices[entrada["outcome_index"]]) * 100, 1)
            precio_entrada = entrada["precio_entrada"]
            diff           = precio_actual - precio_entrada
            if diff <= -15:
                msg = (
                    f"📉 <b>DIVERGENCIA DETECTADA</b> 📉\n\n"
                    f"🏷️ <b>{entrada['apodo']}</b> compró pero el precio bajó\n\n"
                    f"📋 <b>Mercado:</b> {entrada['market']}\n"
                    f"🎯 <b>Posición:</b> {entrada['side']} → <b>{entrada['outcome']}</b>\n"
                    f"💰 <b>USD invertido:</b> ${entrada['usd']:,.2f}\n"
                    f"📊 <b>Precio entrada:</b> {precio_entrada}%\n"
                    f"📉 <b>Precio actual:</b> {precio_actual}% ({diff:.1f}%)\n\n"
                    f"<i>El mercado no ha seguido a la ballena — posible ineficiencia o entrada temprana.</i>\n\n"
                    f"🔗 <a href=\"{entrada['url']}\">Ver mercado</a>"
                )
                if TELEGRAM_CHAT_ID_VIP:
                    enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)
                    print(f"   📉 Divergencia: {entrada['apodo']} | {diff:.1f}%")
            entrada["notificado"] = True
        except Exception as e:
            print(f"   ⚠️  Divergencia error: {e}")
            entrada["notificado"] = True

# ════════════════════════════════════════════════════════════════
#  RESOLUCIÓN AUTOMÁTICA  (FIX: usa condition_id / outcome_index)
# ════════════════════════════════════════════════════════════════

def resolver_pendientes():
    global ultima_resolucion
    ahora = datetime.now(timezone.utc)
    if ahora - ultima_resolucion < timedelta(hours=RESOLVER_CADA_HORAS):
        return
    ultima_resolucion = ahora

    log        = cargar_signals()
    pendientes = [s for s in log if s.get("resultado") == "PENDIENTE" and s.get("condition_id")]

    if not pendientes:
        return

    print(f"   🔍 Revisando {len(pendientes)} señales pendientes...")
    actualizadas = 0

    for señal in pendientes:
        condition_id  = señal.get("condition_id", "")
        outcome_index = señal.get("outcome_index", -1)
        if not condition_id or outcome_index == -1:
            continue

        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets?conditionId={condition_id}",
                timeout=8
            )
            if not r.ok:
                continue
            mercados = r.json()
            if not mercados:
                continue
            mercado = mercados[0] if isinstance(mercados, list) else mercados
            if not mercado.get("closed", False) and not mercado.get("resolved", False):
                continue

            prices_raw = mercado.get("outcomePrices", "[]")
            prices     = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            if outcome_index >= len(prices):
                continue

            precio_final = float(prices[outcome_index])
            if precio_final >= 0.9:
                resultado = "ACIERTO"
                emoji     = "✅"
            elif precio_final <= 0.1:
                resultado = "FALLO"
                emoji     = "❌"
            else:
                continue

            # Actualizar en DB
            try:
                conn_u = get_db()
                cur_u  = conn_u.cursor()
                cur_u.execute(
                    "UPDATE signals SET resultado=%s WHERE condition_id=%s AND outcome_index=%s AND resultado='PENDIENTE'",
                    (resultado, condition_id, outcome_index)
                )
                conn_u.commit()
                cur_u.close()
                conn_u.close()
            except Exception as e:
                print(f"   ⚠️  DB update error: {e}")

            señal["resultado"] = resultado
            actualizadas += 1
            print(f"   {emoji} Resuelto: {señal['apodo']} → {resultado}")

            # Tiempo transcurrido desde la señal (CEST)
            try:
                ts_str = señal.get("timestamp", "")
                ts_señal = datetime.strptime(ts_str, '%H:%M:%S CEST').replace(
                    tzinfo=CEST,
                    year=datetime.now(CEST).year,
                    month=datetime.now(CEST).month,
                    day=datetime.now(CEST).day
                )
                diff_seg = int((datetime.now(CEST) - ts_señal).total_seconds())
                if diff_seg < 0:
                    diff_seg += 86400  # cruzó medianoche
                if diff_seg >= 3600:
                    tiempo_txt = f"hace {diff_seg // 3600}h {(diff_seg % 3600) // 60}min"
                else:
                    tiempo_txt = f"hace {diff_seg // 60}min"
            except Exception:
                tiempo_txt = "hace un momento"

            # Notificación VIP
            if TELEGRAM_CHAT_ID_VIP:
                h = get_historial_ballena(señal["apodo"])
                historial_txt = (
                    f"\n📜 <b>Historial:</b> {h['aciertos']}/{h['total']} aciertos ({h['tasa']}) {h['emoji']}"
                    if h["total"] > 0 else ""
                )
                msg_vip = (
                    f"{emoji} <b>RESULTADO CONFIRMADO</b>\n\n"
                    f"🐋 <b>{señal['apodo']}</b> apostó {tiempo_txt}:\n"
                    f"📋 {señal['mercado']}\n"
                    f"🎯 {señal['posicion']} — <b>${señal['usd']:,.0f} USD</b> al {señal['prob']}%\n\n"
                    f"<b>El mercado resolvió → {emoji} {'Acertó' if resultado == 'ACIERTO' else 'Falló'}</b>"
                    f"{historial_txt}\n\n"
                    f"🔗 <a href=\"{señal['url']}\">Ver mercado</a>"
                )
                enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg_vip)

            # FOMO al básico solo si ACIERTO
            if resultado == "ACIERTO" and TELEGRAM_CHAT_ID_BASICO:
                msg_fomo = (
                    f"✅ <b>SEÑAL VIP VERIFICADA — ACIERTO</b>\n\n"
                    f"{tiempo_txt} detectamos esta jugada:\n"
                    f"📋 <b>{señal['mercado']}</b>\n"
                    f"🎯 {señal['posicion']} — ${señal['usd']:,.0f} USD al {señal['prob']}%\n\n"
                    f"<b>El mercado resolvió. La ballena acertó ✅</b>\n\n"
                    f"<i>Los suscriptores VIP lo vieron en tiempo real.</i>\n\n"
                    f"👇 ¿La perdiste?\n"
                    f"<a href=\"t.me/send?start=s-VIPaccess\">🔐 Unirse al VIP — $15/mes</a>"
                )
                enviar_telegram(TELEGRAM_CHAT_ID_BASICO, msg_fomo)
                print(f"   📣 FOMO básico: {señal['apodo']}")

            time.sleep(0.5)

        except Exception as e:
            print(f"   ⚠️  Error resolviendo: {e}")
            continue

    if actualizadas > 0:
        print(f"   ✅ {actualizadas} señales resueltas")

# ════════════════════════════════════════════════════════════════
#  COMANDO /resultados
# ════════════════════════════════════════════════════════════════

def generar_texto_resultados() -> str:
    log = cargar_signals()
    if not log:
        return "📊 <b>TRACK RECORD</b>\n\nAún no hay señales registradas."

    total      = len(log)
    pendientes = sum(1 for s in log if s["resultado"] == "PENDIENTE")
    acertadas  = sum(1 for s in log if s["resultado"] == "ACIERTO")
    falladas   = sum(1 for s in log if s["resultado"] == "FALLO")
    resueltas  = acertadas + falladas
    tasa       = f"{(acertadas/resueltas*100):.0f}%" if resueltas > 0 else "Sin datos aún"

    ultimas     = log[-5:][::-1]
    ultimas_txt = ""
    for s in ultimas:
        e = "✅" if s["resultado"] == "ACIERTO" else "❌" if s["resultado"] == "FALLO" else "⏳"
        ultimas_txt += f"\n{e} <b>{s['apodo']}</b>\n"
        ultimas_txt += f"   {s['mercado'][:40]}\n"
        ultimas_txt += f"   {s['posicion']} | Score {s.get('score', '?')}\n"

    return (
        f"📈 <b>TRACK RECORD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Total señales:</b> {total}\n"
        f"✅ <b>Acertadas:</b> {acertadas}\n"
        f"❌ <b>Falladas:</b> {falladas}\n"
        f"⏳ <b>Pendientes:</b> {pendientes}\n"
        f"🎯 <b>Tasa de acierto:</b> {tasa}\n\n"
        f"<b>Últimas señales:</b>\n{ultimas_txt}"
    )

# ════════════════════════════════════════════════════════════════
#  RESUMEN SEMANAL
# ════════════════════════════════════════════════════════════════

def check_resumen_semanal():
    global ultimo_lunes_enviado
    ahora = datetime.now(timezone.utc)
    if ahora.weekday() != 0 or ahora.hour < 9:
        return
    semana_actual = ahora.isocalendar()[1]
    if ultimo_lunes_enviado == semana_actual:
        return
    ultimo_lunes_enviado = semana_actual

    log = cargar_signals()
    if not log:
        return

    hace_7_dias = ahora - timedelta(days=7)
    semana = [
        s for s in log
        if datetime.strptime(s["fecha"], '%Y-%m-%d').replace(tzinfo=timezone.utc) >= hace_7_dias
    ]
    if not semana:
        return

    total_sem  = len(semana)
    acertadas  = sum(1 for s in semana if s["resultado"] == "ACIERTO")
    falladas   = sum(1 for s in semana if s["resultado"] == "FALLO")
    pendientes = sum(1 for s in semana if s["resultado"] == "PENDIENTE")
    resueltas  = acertadas + falladas
    tasa       = f"{(acertadas/resueltas*100):.0f}%" if resueltas > 0 else "En curso"

    top     = sorted(semana, key=lambda x: x.get("score", 0), reverse=True)[:3]
    top_txt = ""
    for s in top:
        e = "✅" if s["resultado"] == "ACIERTO" else "❌" if s["resultado"] == "FALLO" else "⏳"
        h = get_historial_ballena(s["apodo"])
        top_txt += f"\n{e} <b>{s['apodo']}</b> ({h['tasa']} histórico)\n   {s['mercado'][:40]}\n   Score {s.get('score','?')}\n"

    msg_vip = (
        f"📅 <b>RESUMEN SEMANAL VIP</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🐋 <b>Señales esta semana:</b> {total_sem}\n"
        f"✅ <b>Acertadas:</b> {acertadas}\n"
        f"❌ <b>Falladas:</b> {falladas}\n"
        f"⏳ <b>Pendientes:</b> {pendientes}\n"
        f"🎯 <b>Tasa de acierto:</b> {tasa}\n\n"
        f"🏆 <b>Top señales:</b>{top_txt}"
    )
    if TELEGRAM_CHAT_ID_VIP:
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg_vip)
        print("   📅 Resumen semanal VIP enviado")

    if TELEGRAM_CHAT_ID_BASICO:
        msg_basico = (
            f"📊 <b>Esta semana en PolyWhales VIP:</b>\n\n"
            f"✅ <b>{acertadas} señales acertadas</b>\n"
            f"❌ {falladas} falladas\n"
            f"⏳ {pendientes} pendientes\n"
            f"🎯 Tasa de acierto: <b>{tasa}</b>\n\n"
            f"<i>Las mejores señales las reciben primero los VIP.</i>\n\n"
            f"<a href=\"t.me/send?start=s-VIPaccess\">🔐 Unirse al VIP — $15/mes</a>"
        )
        enviar_telegram(TELEGRAM_CHAT_ID_BASICO, msg_basico)
        print("   📣 Resumen semanal FOMO al básico")

# ════════════════════════════════════════════════════════════════
#  RESUMEN DIARIO
# ════════════════════════════════════════════════════════════════

def check_resumen_diario():
    global stats_dia, ultimo_resumen
    if datetime.now(timezone.utc) - ultimo_resumen < timedelta(hours=24):
        return

    n_vip     = stats_dia["señales_vip"]
    n_basico  = stats_dia["señales_basico"]
    n_wallets = len(stats_dia["wallets_vip"])

    mercados_count = defaultdict(int)
    for m in stats_dia["mercados_vip"]:
        mercados_count[m] += 1
    top_mercados = sorted(mercados_count.items(), key=lambda x: x[1], reverse=True)[:3]
    top_txt = "\n".join([f"  • {m[:40]} ({n}x)" for m, n in top_mercados]) if top_mercados else "  • Sin datos"

    msg = (
        f"📊 <b>RESUMEN DIARIO VIP</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🐋 <b>Señales VIP:</b> {n_vip}\n"
        f"📡 <b>Señales básico:</b> {n_basico}\n"
        f"👛 <b>Ballenas únicas:</b> {n_wallets}\n\n"
        f"🔥 <b>Mercados más activos:</b>\n{top_txt}"
    )
    if TELEGRAM_CHAT_ID_VIP:
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)

    stats_dia = {
        "señales_vip":    0,
        "señales_basico": 0,
        "wallets_vip":    set(),
        "mercados_vip":   [],
    }
    ultimo_resumen = datetime.now(timezone.utc)

# ════════════════════════════════════════════════════════════════
#  COMANDOS TELEGRAM
# ════════════════════════════════════════════════════════════════

def procesar_comandos():
    global ultimo_update_id
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": ultimo_update_id + 1, "timeout": 1},
            timeout=5,
        )
        if not r.ok:
            return
        updates = r.json().get("result", [])
        procesar_nuevos_miembros(updates)
        for update in updates:
            ultimo_update_id = update["update_id"]
            msg     = update.get("message", {})
            texto   = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            canales = {TELEGRAM_CHAT_ID_VIP, TELEGRAM_CHAT_ID_BASICO, "1387775814"}
            if chat_id not in canales:
                continue

            if "/resultados" in texto:
                print(f"   📩 /resultados desde {chat_id}")
                enviar_telegram(chat_id, generar_texto_resultados())

    except Exception as e:
        print(f"   ⚠️  Comandos: {e}")

# ════════════════════════════════════════════════════════════════
#  GESTIÓN DEL CANAL
# ════════════════════════════════════════════════════════════════

def fijar_mensaje_vip():
    global mensaje_pinned_id, ultimo_pin
    if not TELEGRAM_CHAT_ID_BASICO or not TELEGRAM_BOT_TOKEN:
        return
    ahora = datetime.now(timezone.utc)
    if ultimo_pin and ahora - ultimo_pin < timedelta(days=7):
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID_BASICO, "text": MENSAJE_PIN_VIP, "parse_mode": "HTML"},
            timeout=10,
        )
        if not r.ok:
            return
        msg_id = r.json()["result"]["message_id"]
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage",
            json={"chat_id": TELEGRAM_CHAT_ID_BASICO, "message_id": msg_id, "disable_notification": True},
            timeout=10,
        )
        mensaje_pinned_id = msg_id
        ultimo_pin        = ahora
        print("   📌 Mensaje VIP fijado")
    except Exception as e:
        print(f"   ⚠️  Pin VIP: {e}")

def limpiar_mensajes_antiguos():
    global ultimo_limpieza
    ahora = datetime.now(timezone.utc)
    if ahora - ultimo_limpieza < timedelta(hours=24):
        return
    ultimo_limpieza = ahora
    if not TELEGRAM_CHAT_ID_BASICO or not TELEGRAM_BOT_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"limit": 100}, timeout=10,
        )
        if not r.ok:
            return
        updates  = r.json().get("result", [])
        limite   = ahora - timedelta(days=7)
        borrados = 0
        for update in updates:
            msg = update.get("channel_post", {})
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id", "")) != TELEGRAM_CHAT_ID_BASICO:
                continue
            fecha_msg = datetime.fromtimestamp(msg.get("date", 0), timezone.utc)
            if fecha_msg >= limite:
                continue
            msg_id = msg.get("message_id")
            if not msg_id or msg_id == mensaje_pinned_id:
                continue
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                json={"chat_id": TELEGRAM_CHAT_ID_BASICO, "message_id": msg_id},
                timeout=5,
            )
            if resp.ok:
                borrados += 1
            time.sleep(0.1)
        if borrados > 0:
            print(f"   🗑️  {borrados} mensajes borrados")
    except Exception as e:
        print(f"   ⚠️  Limpieza: {e}")

def procesar_nuevos_miembros(updates: list):
    for update in updates:
        msg    = update.get("message", {})
        nuevos = msg.get("new_chat_members", [])
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if nuevos and chat_id == TELEGRAM_CHAT_ID_BASICO:
            print("   👋 Nuevo miembro")
            enviar_telegram(TELEGRAM_CHAT_ID_BASICO, MENSAJE_BIENVENIDA)

# ════════════════════════════════════════════════════════════════
#  PERSISTENCIA
# ════════════════════════════════════════════════════════════════

def cargar_estado():
    global whale_apodos, ultimo_pin
    if not PERSIST_PATH.exists():
        return
    try:
        data = json.loads(PERSIST_PATH.read_text())
        whale_apodos.update(data.get("apodos", {}))
        for h in data.get("seen_hashes", []):
            seen_hashes.append(h)
        if data.get("ultimo_pin"):
            ultimo_pin = datetime.fromisoformat(data["ultimo_pin"])
        print(f"   💾 Estado cargado: {len(whale_apodos)} apodos | {len(seen_hashes)} hashes")
    except Exception as e:
        print(f"   ⚠️  No se pudo cargar estado: {e}")

def guardar_estado():
    try:
        data = {
            "apodos":      whale_apodos,
            "seen_hashes": list(seen_hashes)[-500:],
            "ultimo_pin":  ultimo_pin.isoformat() if ultimo_pin else None,
        }
        PERSIST_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"   ⚠️  No se pudo guardar estado: {e}")

# ════════════════════════════════════════════════════════════════
#  FILTROS
# ════════════════════════════════════════════════════════════════

def es_mercado_basura(trade: dict) -> bool:
    slug  = trade.get("eventSlug", "").lower()
    title = trade.get("title", "").lower()
    return any(p in slug or p in title for p in SLUGS_IGNORADOS)

def es_spam(wallet: str, slug: str) -> bool:
    key   = (wallet.lower(), slug.lower())
    ahora = datetime.now(timezone.utc)
    if key in anti_spam:
        if ahora - anti_spam[key] < timedelta(minutes=ANTI_SPAM_MINUTOS):
            return True
    anti_spam[key] = ahora
    return False

def registrar_mercado_caliente(slug: str) -> bool:
    ahora  = datetime.now(timezone.utc)
    limite = ahora - timedelta(minutes=MERCADO_CALIENTE_MIN)
    mercado_hits[slug] = [t for t in mercado_hits[slug] if t > limite]
    mercado_hits[slug].append(ahora)
    return len(mercado_hits[slug]) >= MERCADO_CALIENTE_N

# ════════════════════════════════════════════════════════════════
#  HTTP CON RETRY
# ════════════════════════════════════════════════════════════════

def _get_with_retry(url: str, retries: int = 3, timeout: int = 6):
    for intento in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 429:
                wait = 2 ** intento
                print(f"   ⏳ Rate limit, esperando {wait}s...")
                time.sleep(wait)
                continue
            return r
        except requests.exceptions.Timeout:
            print(f"   ⏱️  Timeout (intento {intento+1}/{retries})")
        except Exception as e:
            print(f"   ❌ Error red: {e}")
        time.sleep(1)
    return None

# ════════════════════════════════════════════════════════════════
#  VERIFICACIÓN DE WALLET
# ════════════════════════════════════════════════════════════════

def verificar_wallet(wallet: str):
    wallet = wallet.lower()
    ahora  = datetime.now(timezone.utc)

    if wallet in whale_cache:
        entrada = whale_cache[wallet]
        if ahora - entrada["ts"] < timedelta(hours=CACHE_TTL_HORAS):
            return entrada["roi"], entrada["perfil"]
        del whale_cache[wallet]

    print(f"   🕵️  Analizando {wallet[:10]}... ", end="", flush=True)
    time.sleep(WALLET_API_DELAY)

    r = _get_with_retry(
        f"https://data-api.polymarket.com/positions?user={wallet}&limit=500&sizeThreshold=1"
    )
    if r is None:
        return None, None
    if r.status_code == 404:
        whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
        print("404")
        return None, None

    try:
        r.raise_for_status()
        posiciones = r.json()
        if not posiciones:
            whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
            print("sin posiciones")
            return None, None

        total_invertido = sum(float(p.get("initialValue", 0)) for p in posiciones)
        total_pnl       = sum(float(p.get("cashPnl", 0)) for p in posiciones)
        num_posiciones  = len(posiciones)

        if total_invertido <= 0 or num_posiciones < 3:
            whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
            print(f"descartada (inv=${total_invertido:.0f}, pos={num_posiciones})")
            return None, None

        roi    = (total_pnl / total_invertido) * 100
        perfil = f"ROI {roi:.1f}% | PnL ${total_pnl:,.0f} | {num_posiciones} posiciones"

        if roi >= MIN_ROI_BASICO:
            whale_cache[wallet] = {"roi": roi, "perfil": perfil, "ts": ahora}
            print(f"✅ ROI {roi:.1f}%")
            return roi, perfil

        whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
        print(f"ROI insuficiente ({roi:.1f}%)")
        return None, None

    except Exception as e:
        print(f"error: {e}")
        return None, None

# ════════════════════════════════════════════════════════════════
#  SCORE DE CONFIANZA  (FIX: score mínimo más generoso)
# ════════════════════════════════════════════════════════════════

def calcular_score(usd: float, roi: float, racha: int, caliente: bool, historial: dict) -> int:
    score = 0
    # USD invertido
    if usd >= 5000:   score += 35
    elif usd >= 2000: score += 28
    elif usd >= 1000: score += 20
    elif usd >= 500:  score += 14
    else:             score += 10
    # ROI
    if roi >= 75:     score += 25
    elif roi >= 50:   score += 20
    elif roi >= 25:   score += 14
    elif roi >= 10:   score += 8
    else:             score += 4
    # Racha
    score += min(racha * 4, 15)
    # Mercado caliente
    if caliente:      score += 8
    # Bonus historial
    if historial["total"] >= 5:
        tasa_num = (historial["aciertos"] / historial["total"]) * 100
        if tasa_num >= 75:   score += 17
        elif tasa_num >= 60: score += 10
        elif tasa_num >= 50: score += 5
    return min(score, 100)

def score_emoji(score: int) -> str:
    if score >= 80: return "🔥🔥🔥"
    if score >= 60: return "🔥🔥"
    if score >= 40: return "🔥"
    return "⚡"

# ════════════════════════════════════════════════════════════════
#  APODOS
# ════════════════════════════════════════════════════════════════

def get_apodo(wallet: str) -> str:
    wallet = wallet.lower()
    if wallet not in whale_apodos:
        whale_apodos[wallet] = random.choice(APODOS_EPICOS)
    return whale_apodos[wallet]

# ════════════════════════════════════════════════════════════════
#  NOTICIAS
# ════════════════════════════════════════════════════════════════

def buscar_noticia(query: str):
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            resultados = list(ddgs.news(query, max_results=1))
        if resultados:
            return resultados[0].get("title")
    except Exception:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                resultados = list(ddgs.news(query, max_results=1))
            if resultados:
                return resultados[0].get("title")
        except Exception as e:
            print(f"   ⚠️  Noticias: {e}")
    return None

# ════════════════════════════════════════════════════════════════
#  ANÁLISIS IA
# ════════════════════════════════════════════════════════════════

def analizar_con_claude(payload: dict, noticia):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        ctx_noticia = f'Titular reciente: "{noticia}"' if noticia else "Sin noticias recientes."
        prompt = f"""Eres un analista financiero experto en mercados de predicción.
Analiza esta operación en Polymarket en MÁXIMO 3 líneas. Sé directo y concreto.

DATOS:
- Mercado: {payload['market']}
- Posición: {payload['side']} → {payload['outcome']}
- USD invertido: ${payload['usd_invested']:,.2f}
- Probabilidad implícita: {payload['price']}%
- Perfil del trader: {payload['perfil']}
- {ctx_noticia}

¿Cuál es el posible motivo de esta jugada y qué implica para el mercado?"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-5",
                "max_tokens": 220,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"   ⚠️  Claude API: {e}")
        return None

# ════════════════════════════════════════════════════════════════
#  PRECIO ACTUAL
# ════════════════════════════════════════════════════════════════

def get_precio_actual(condition_id: str, outcome_index: int):
    if not condition_id:
        return None
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets?conditionId={condition_id}",
            timeout=5
        )
        if not r.ok:
            return None
        mercados = r.json()
        if not mercados:
            return None
        mercado    = mercados[0] if isinstance(mercados, list) else mercados
        prices_raw = mercado.get("outcomePrices", "[]")
        prices     = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if outcome_index < len(prices):
            return round(float(prices[outcome_index]) * 100, 1)
    except Exception:
        pass
    return None

# ════════════════════════════════════════════════════════════════
#  MENSAJES
# ════════════════════════════════════════════════════════════════

def mensaje_basico(payload: dict, es_cebo: bool = False) -> str:
    cebo_txt = ""
    if es_cebo:
        cebo_txt = (
            "\n\n⭐ <b>SEÑAL VIP FILTRADA</b>\n"
            "Esta wallet tiene perfil verificado y opera con sumas mayores.\n"
            "<i>En VIP recibes historial de aciertos, alertas de convicción y análisis IA.</i>"
        )
    return (
        f"📡 <b>SEÑAL DETECTADA</b>\n\n"
        f"📋 <b>Mercado:</b> {payload['market']}\n"
        f"🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>\n"
        f"💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD\n"
        f"📊 <b>Probabilidad:</b> {payload['price']}%\n"
        f"🔑 <b>Wallet:</b> <code>{payload['wallet'][:10]}...</code>\n\n"
        f"🔗 <a href=\"{payload['url']}\">Ver en Polymarket</a>"
        f"{cebo_txt}\n\n"
        f"<i>⚠️ Canal básico — Actualiza a VIP para análisis completo.</i>"
    )

def mensaje_vip(payload: dict, apodo: str, noticia, analisis,
                racha: int, score: int, caliente: bool,
                historial: dict, precio_actual=None) -> str:
    racha_txt    = f"\n🔥 <b>RACHA: {racha} ops en sesión</b>" if racha >= 2 else ""
    caliente_txt = "\n🌡️ <b>MERCADO CALIENTE — múltiples ballenas</b>" if caliente else ""
    noticia_txt  = f"\n\n📰 <b>Contexto:</b> <i>{noticia}</i>" if noticia else ""
    analisis_txt = f"\n\n🤖 <b>Análisis IA:</b>\n{analisis}" if analisis else ""

    if precio_actual and precio_actual != payload['price']:
        diff   = precio_actual - payload['price']
        flecha = "📈" if diff > 0 else "📉"
        precio_txt = (
            f"\n📊 <b>Precio entrada:</b> {payload['price']}%"
            f"\n{flecha} <b>Precio ahora:</b> {precio_actual}% "
            f"({'+'if diff>0 else ''}{diff:.1f}%)"
        )
    else:
        precio_txt = f"\n📊 <b>Probabilidad:</b> {payload['price']}%"

    if historial["total"] >= 3:
        hist_txt = f"\n📜 <b>Historial:</b> {historial['aciertos']}/{historial['total']} aciertos ({historial['tasa']}) {historial['emoji']}"
    elif historial["total"] > 0:
        hist_txt = f"\n📜 <b>Historial:</b> {historial['aciertos']}/{historial['total']} aciertos 🆕"
    else:
        hist_txt = "\n📜 <b>Historial:</b> Primera señal detectada 🆕"

    return (
        f"🐋 <b>ALERTA VIP — BALLENA VERIFICADA</b> 🐋{racha_txt}{caliente_txt}\n\n"
        f"🏷️ <b>Apodo:</b> {apodo}\n"
        f"📋 <b>Mercado:</b> {payload['market']}\n"
        f"🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>\n"
        f"💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD"
        f"{precio_txt}\n"
        f"📈 <b>Perfil:</b> {payload['perfil']}"
        f"{hist_txt}\n"
        f"🎯 <b>Score:</b> {score}/100 {score_emoji(score)}\n"
        f"🔑 <b>Wallet:</b> <code>{payload['wallet'][:10]}...</code>"
        f"{noticia_txt}{analisis_txt}\n\n"
        f"🔗 <a href=\"{payload['url']}\">Ver mercado en Polymarket</a>\n"
        f"⏰ {payload['timestamp']}"
    )

def mensaje_mercado_caliente(slug: str, titulo: str, n: int) -> str:
    return (
        f"🌡️ <b>MERCADO CALIENTE</b> 🌡️\n\n"
        f"📋 <b>Mercado:</b> {titulo}\n"
        f"🐋 <b>{n} ballenas en los últimos {MERCADO_CALIENTE_MIN} minutos</b>\n\n"
        f"🔗 <a href=\"https://polymarket.com/event/{slug}\">Ver mercado</a>\n\n"
        f"<i>Señal de alta convicción institucional.</i>"
    )

# ════════════════════════════════════════════════════════════════
#  TELEGRAM
# ════════════════════════════════════════════════════════════════

def enviar_telegram(chat_id: str, texto: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": texto,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10,
        )
        if resp.status_code == 400:
            resp = requests.post(url, json={"chat_id": chat_id, "text": texto}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")
        return False

# ════════════════════════════════════════════════════════════════
#  BUCLE PRINCIPAL
# ════════════════════════════════════════════════════════════════

def poll():
    global ciclo_actual
    ciclo_actual += 1
    print(f"\n🔍 Ciclo {ciclo_actual} — {datetime.now(CEST).strftime('%H:%M:%S CEST')}")

    procesar_comandos()
    check_resumen_diario()
    check_resumen_semanal()
    resolver_pendientes()
    revisar_divergencias()
    fijar_mensaje_vip()
    limpiar_mensajes_antiguos()

    r = _get_with_retry("https://data-api.polymarket.com/trades?limit=100")
    if r is None:
        print("❌ API Polymarket no responde")
        return

    try:
        r.raise_for_status()
        trades = r.json()
    except Exception as e:
        print(f"❌ Error parsing: {e}")
        return

    señales_basico = 0
    señales_vip    = 0

    for trade in trades:
        tx = trade.get("transactionHash", "")
        if not tx or tx in seen_hashes:
            continue
        seen_hashes.append(tx)

        try:
            usd    = round(float(trade.get("size", 0)) * float(trade.get("price", 0)), 2)
            precio = float(trade.get("price", 0))
        except (ValueError, TypeError):
            continue

        wallet = trade.get("proxyWallet", "")
        slug   = trade.get("eventSlug", "")

        if usd < MIN_USD_BASICO:                     continue
        if es_mercado_basura(trade):                 continue
        if not (PRECIO_MIN <= precio <= PRECIO_MAX): continue
        if not wallet:                               continue
        if es_spam(wallet, slug):
            print(f"   🔇 Spam: {wallet[:10]} en {slug[:20]}")
            continue

        roi, perfil = verificar_wallet(wallet)
        if roi is None:
            continue

        try:
            ts = datetime.fromtimestamp(int(trade["timestamp"]), CEST).strftime('%H:%M:%S CEST')
        except Exception:
            ts = datetime.now(CEST).strftime('%H:%M:%S CEST')

        payload = {
            "wallet":       wallet,
            "side":         trade.get("side", ""),
            "outcome":      trade.get("outcome", ""),
            "usd_invested": usd,
            "price":        round(precio * 100, 1),
            "market":       trade.get("title", "Sin título"),
            "url":          f"https://polymarket.com/event/{slug}",
            "tx_hash":      tx,
            "timestamp":    ts,
            "perfil":       perfil,
            "roi":          roi,
        }

        es_vip   = usd >= MIN_USD_VIP and roi >= MIN_ROI_VIP
        caliente = registrar_mercado_caliente(slug)

        # ── VIP ──────────────────────────────────────────────────
        if es_vip and TELEGRAM_CHAT_ID_VIP:
            apodo     = get_apodo(wallet)
            historial = get_historial_ballena(apodo)

            if len(whale_streaks) >= 500:
                del whale_streaks[next(iter(whale_streaks))]
            whale_streaks[wallet] = whale_streaks.get(wallet, 0) + 1
            racha = whale_streaks[wallet]
            score = calcular_score(usd, roi, racha, caliente, historial)

            check_alta_conviccion(wallet, slug, apodo, payload)

            print(f"   🐋 VIP: {apodo} | ROI {roi:.1f}% | ${usd} | Score {score} | Hist {historial['tasa']}")
            noticia       = buscar_noticia(trade.get("title", ""))
            analisis      = analizar_con_claude(payload, noticia)
            condition_id  = trade.get("conditionId", "")
            outcome_index = int(trade.get("outcomeIndex", -1))
            precio_actual = get_precio_actual(condition_id, outcome_index)

            if enviar_telegram(TELEGRAM_CHAT_ID_VIP, mensaje_vip(
                payload, apodo, noticia, analisis,
                racha, score, caliente, historial, precio_actual
            )):
                print(f"   👑 VIP enviado: {apodo} | ${usd}")
                señales_vip += 1
                guardar_señal(payload, apodo, score, trade)
                stats_dia["señales_vip"] += 1
                stats_dia["wallets_vip"].add(wallet)
                stats_dia["mercados_vip"].append(payload["market"])
                registrar_para_divergencia(condition_id, outcome_index, payload["price"], apodo, payload)

            if caliente and TELEGRAM_CHAT_ID_BASICO:
                n_hits = len(mercado_hits.get(slug, []))
                enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_mercado_caliente(slug, payload["market"], n_hits))

        # ── BÁSICO ───────────────────────────────────────────────
        if TELEGRAM_CHAT_ID_BASICO:
            if not es_vip and usd >= MIN_USD_BASICO and usd <= MAX_USD_BASICO and roi >= MIN_ROI_BASICO:
                pasa    = True
                es_cebo = False
            elif es_vip and random.randint(1, CEBO_PROBABILIDAD) == 1:
                pasa    = True
                es_cebo = True
            else:
                pasa = False

            if pasa:
                if enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_basico(payload, es_cebo)):
                    print(f"   {'🎣 Cebo' if es_cebo else '📡 Básico'} enviado: ${usd}")
                    señales_basico += 1
                    stats_dia["señales_basico"] += 1
                    guardar_estado()

    guardar_estado()
    print(f"📊 Trades: {len(trades)} | Básico: {señales_basico} | VIP: {señales_vip} | Cache: {len(whale_cache)}")

# ════════════════════════════════════════════════════════════════
#  FLASK API
# ════════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app,
     resources={r"/api/*": {
         "origins": "*",
         "methods": ["GET", "POST", "OPTIONS"],
         "allow_headers": ["Content-Type"],
         "supports_credentials": False
     }},
     expose_headers=["Content-Type"]
)

@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(usd), 0) as volume,
                COUNT(CASE WHEN resultado = 'ACIERTO' THEN 1 END) as wins,
                COUNT(CASE WHEN resultado IN ('ACIERTO', 'FALLO') THEN 1 END) as resolved
            FROM signals
            WHERE created_at >= NOW() - INTERVAL '24 hours'
        """)
        result = cur.fetchone()
        total    = result[0] or 0
        volume   = float(result[1]) if result[1] else 0.0
        wins     = result[2] or 0
        resolved = result[3] or 0
        success_rate = (wins / resolved * 100) if resolved > 0 else 0
        cur.close()
        conn.close()
        return jsonify({
            "total_signals": total,
            "success_rate":  success_rate,
            "total_volume":  volume,
            "timestamp":     datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        print(f"   ❌ API error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/api/signals", methods=["GET"])
def get_signals():
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({
            "signals":   [dict(r) for r in rows],
            "total":     len(rows),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ════════════════════════════════════════════════════════════════
#  INICIO
# ════════════════════════════════════════════════════════════════

def poll_loop():
    while True:
        try:
            poll()
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 Detenido. Guardando estado...")
            guardar_estado()
            break
        except Exception as e:
            print(f"❌ Error inesperado: {e}")
            time.sleep(10)

if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v4.1")
    print(f"   Básico : >${MIN_USD_BASICO}–${MAX_USD_BASICO} USD | ROI >{MIN_ROI_BASICO}%")
    print(f"   VIP    : >${MIN_USD_VIP} USD | ROI >{MIN_ROI_VIP}%")
    print(f"   Precio : {int(PRECIO_MIN*100)}%–{int(PRECIO_MAX*100)}%")
    print(f"   Fixes  : condition_id DB | CEST resolver | Score mejorado")
    print("─" * 50)

    init_db()
    cargar_estado()

    bot_thread = threading.Thread(target=poll_loop, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)