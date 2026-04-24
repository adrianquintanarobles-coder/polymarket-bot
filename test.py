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
import stripe
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
WHOP_API_KEY            = os.getenv("WHOP_API_KEY", "")
WHOP_WEBHOOK_SECRET     = os.getenv("WHOP_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY       = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET   = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID         = os.getenv("STRIPE_PRICE_ID", "price_1TNpFHLQKsHvzszRsKQ5Pk78")
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
ultimo_contador_basico    = datetime.now(timezone.utc) - timedelta(hours=25)
wallets_conocidas         = set()
consenso_tracker          = defaultdict(list)
racha_aciertos            = defaultdict(int)  # {apodo: racha_actual}
ultimo_resumen_nocturno   = datetime.now(timezone.utc) - timedelta(hours=25)

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vip_users (
                chat_id    TEXT PRIMARY KEY,
                nombre     TEXT,
                whop_id    TEXT,
                added_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("   🗄️  Base de datos inicializada")
    except Exception as e:
        print(f"   ⚠️  DB init error: {e}")

# ════════════════════════════════════════════════════════════════
#  VIP USERS — gestión automática
# ════════════════════════════════════════════════════════════════

def es_vip_user(chat_id: str) -> bool:
    if chat_id in {"1387775814", str(TELEGRAM_CHAT_ID_VIP), str(TELEGRAM_CHAT_ID_BASICO)}:
        return True
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT 1 FROM vip_users WHERE chat_id = %s", (chat_id,))
        result = cur.fetchone()
        cur.close(); conn.close()
        return result is not None
    except Exception:
        return False

def añadir_vip_user(chat_id: str, nombre: str, whop_id: str = "") -> bool:
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO vip_users (chat_id, nombre, whop_id) VALUES (%s,%s,%s) ON CONFLICT (chat_id) DO UPDATE SET nombre=%s, whop_id=%s",
            (chat_id, nombre, whop_id, nombre, whop_id)
        )
        conn.commit()
        cur.close(); conn.close()
        print(f"   ✅ VIP añadido: {nombre} ({chat_id})")
        return True
    except Exception as e:
        print(f"   ⚠️  Error añadiendo VIP: {e}")
        return False

def eliminar_vip_user(chat_id: str) -> bool:
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("DELETE FROM vip_users WHERE chat_id = %s", (chat_id,))
        conn.commit()
        cur.close(); conn.close()
        print(f"   🗑️  VIP eliminado: {chat_id}")
        return True
    except Exception as e:
        print(f"   ⚠️  Error eliminando VIP: {e}")
        return False

def listar_vip_users() -> list:
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM vip_users ORDER BY added_at DESC")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def generar_lista_ballenas() -> str:
    from collections import Counter as Cnt
    log = cargar_signals()
    if not log:
        return "No hay señales registradas aún."
    apodos = Cnt(s.get("apodo") for s in log)
    txt = "🐋 <b>BALLENAS ACTIVAS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for apodo, n in apodos.most_common(10):
        h = get_historial_ballena(apodo)
        txt += f"• <b>{apodo}</b> — {n} señales | {h['tasa']} {h['emoji']}\n"
    txt += "\n<i>Usa /ballena [apodo] para ver la ficha completa</i>"
    return txt

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

            prices_raw = mercado.get("outcomePrices", "[]")
            prices     = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            if outcome_index >= len(prices):
                continue

            precio_final = float(prices[outcome_index])
            cerrado      = mercado.get("closed", False) or mercado.get("resolved", False)

            # Método 1: mercado oficialmente cerrado
            # Método 2: precio extremo aunque no esté cerrado (≥95% o ≤5%)
            if cerrado:
                if precio_final >= 0.9:
                    resultado = "ACIERTO"
                    emoji     = "✅"
                elif precio_final <= 0.1:
                    resultado = "FALLO"
                    emoji     = "❌"
                else:
                    continue
            elif precio_final >= 0.95:
                resultado = "ACIERTO"
                emoji     = "✅"
            elif precio_final <= 0.05:
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
            check_racha_aciertos(señal["apodo"])
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

    ranking_txt = get_ranking_ballenas()
    msg_vip = (
        f"📅 <b>RESUMEN SEMANAL VIP</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🐋 <b>Señales esta semana:</b> {total_sem}\n"
        f"✅ <b>Acertadas:</b> {acertadas}\n"
        f"❌ <b>Falladas:</b> {falladas}\n"
        f"⏳ <b>Pendientes:</b> {pendientes}\n"
        f"🎯 <b>Tasa de acierto:</b> {tasa}\n\n"
        f"🏆 <b>Top señales:</b>{top_txt}"
        f"{ranking_txt}"
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
            texto   = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            nombre  = msg.get("from", {}).get("first_name", "VIP")
            texto_l = texto.lower()

            if not texto or not chat_id:
                continue

            es_admin   = chat_id == "1387775814"
            es_canal   = chat_id in {TELEGRAM_CHAT_ID_VIP, TELEGRAM_CHAT_ID_BASICO}
            es_vip     = es_vip_user(chat_id)

            # ── /start — cualquiera puede iniciarlo ─────────────
            if texto_l.startswith("/start"):
                # Registrar chat_id real cuando escriben /start
                # Esto vincula al usuario con su pago de Whop
                añadir_vip_user(chat_id, nombre)
                es_vip = es_vip_user(chat_id)

                if es_vip:
                    enviar_telegram(chat_id,
                        f"🐋 <b>Bienvenido al VIP, {nombre}!</b>\n\n"
                        f"Tienes acceso completo a todos los comandos:\n\n"
                        f"📊 /resultados — Track record completo\n"
                        f"🐋 /ballena [apodo] — Ficha de una ballena\n"
                        f"   Ejemplo: /ballena El Oráculo\n"
                        f"📋 /lista — Todas las ballenas activas\n"
                        f"ℹ️ /ayuda — Ver todos los comandos\n\n"
                        f"<i>Las señales llegan automáticamente al canal VIP.</i>"
                    )
                else:
                    enviar_telegram(chat_id,
                        f"👋 <b>Hola {nombre}!</b>\n\n"
                        f"Soy el bot de PolyWhales 🐋\n\n"
                        f"Para acceder a los comandos VIP necesitas suscribirte:\n\n"
                        f"<a href=\"https://whop.com/PolyWhales\">🔐 Suscribirse al VIP — $15/mes</a>\n\n"
                        f"<i>Una vez suscrito recibirás acceso automáticamente.</i>"
                    )
                continue

            # ── /ayuda ───────────────────────────────────────────
            if texto_l.startswith("/ayuda") or texto_l.startswith("/help"):
                if es_vip:
                    enviar_telegram(chat_id,
                        f"🐋 <b>Comandos PolyWhales VIP</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📊 /resultados — Track record completo\n"
                        f"🐋 /ballena [apodo] — Ficha de una ballena\n"
                        f"   Ejemplo: /ballena El Oráculo\n"
                        f"📋 /lista — Todas las ballenas activas\n\n"
                        f"<i>Las señales llegan al canal VIP automáticamente.</i>"
                    )
                continue

            # ── Comandos VIP (requieren acceso) ─────────────────
            if not es_vip and not es_canal:
                enviar_telegram(chat_id,
                    f"🔒 Necesitas suscripción VIP para usar este comando.\n\n"
                    f"<a href=\"https://whop.com/PolyWhales\">🔐 Suscribirse — $15/mes</a>"
                )
                continue

            if "/resultados" in texto_l:
                print(f"   📩 /resultados desde {chat_id}")
                enviar_telegram(chat_id, generar_texto_resultados())

            elif texto_l.startswith("/ballena"):
                partes = texto[8:].strip()
                if partes:
                    apodo_buscado = partes.title()
                    print(f"   📩 /ballena {apodo_buscado} desde {chat_id}")
                    enviar_telegram(chat_id, generar_ficha_completa(apodo_buscado))
                else:
                    enviar_telegram(chat_id,
                        "🐋 Uso: /ballena [apodo]\nEjemplo: /ballena El Oráculo\n\n"
                        "Usa /lista para ver todas las ballenas."
                    )

            elif texto_l.startswith("/lista"):
                print(f"   📩 /lista desde {chat_id}")
                enviar_telegram(chat_id, generar_lista_ballenas())

            # ── Comandos admin ───────────────────────────────────
            elif texto_l.startswith("/addvip") and es_admin:
                partes = texto[7:].strip().split()
                if len(partes) >= 1:
                    target_id = partes[0]
                    target_nombre = " ".join(partes[1:]) if len(partes) > 1 else "VIP"
                    if añadir_vip_user(target_id, target_nombre):
                        enviar_telegram(chat_id, f"✅ VIP añadido: {target_nombre} ({target_id})")
                        enviar_telegram(target_id,
                            f"🐋 <b>¡Bienvenido al VIP, {target_nombre}!</b>\n\n"
                            f"Ya tienes acceso a todos los comandos:\n\n"
                            f"📊 /resultados\n"
                            f"🐋 /ballena [apodo]\n"
                            f"📋 /lista\n"
                            f"ℹ️ /ayuda"
                        )

            elif texto_l.startswith("/removevip") and es_admin:
                partes = texto[10:].strip()
                if partes:
                    if eliminar_vip_user(partes):
                        enviar_telegram(chat_id, f"✅ VIP eliminado: {partes}")

            elif texto_l.startswith("/listvips") and es_admin:
                vips = listar_vip_users()
                if vips:
                    txt = f"👑 <b>VIPs activos: {len(vips)}</b>\n\n"
                    for v in vips[:20]:
                        txt += f"• {v['nombre']} — {v['chat_id']}\n"
                    enviar_telegram(chat_id, txt)
                else:
                    enviar_telegram(chat_id, "No hay VIPs registrados.")

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

FRASES_INTRIGA = [
    "\U0001f440 Una ballena acaba de moverse en silencio",
    "\U0001f988 Movimiento inteligente detectado",
    "\U0001f50d Wallet verificada en accion",
    "\u26a1 Trader rentable acaba de entrar",
    "\U0001f9e0 Dinero inteligente en movimiento",
    "\U0001f3af Operacion calculada detectada",
]

def mensaje_basico(payload: dict, es_cebo: bool = False) -> str:
    frase = random.choice(FRASES_INTRIGA)
    if es_cebo:
        lines = [
            "\U0001f510 <b>SENAL VIP FILTRADA</b>",
            "",
            frase,
            "",
            "<b>Mercado:</b> " + payload["market"],
            "<b>Posicion:</b> " + payload["side"] + " -> <b>" + payload["outcome"] + "</b>",
            "<b>Invertido:</b> $" + "{:,.2f}".format(payload["usd_invested"]) + " USD",
            "<b>Prob. entrada:</b> " + str(payload["price"]) + "%",
            "",
            "<b>\U0001f510 ROI, score y analisis IA en VIP</b>",
            "<i>Esta ballena tiene historial verificado de aciertos.</i>",
            "",
            "\U0001f447 Que sabe esta wallet que tu no?",
            '<a href="t.me/send?start=s-VIPaccess">\U0001f510 Unirse al VIP - $15/mes</a>',
            "",
            '<a href="' + payload["url"] + '">\U0001f517 Ver mercado</a>',
            "<i>\u23f0 " + payload["timestamp"] + "</i>",
        ]
        return "\n".join(lines)
    lines = [
        "\U0001f4e1 <b>SENAL DETECTADA</b>",
        "",
        frase,
        "",
        "<b>Mercado:</b> " + payload["market"],
        "<b>Posicion:</b> " + payload["side"] + " -> <b>" + payload["outcome"] + "</b>",
        "<b>Invertido:</b> $" + "{:,.2f}".format(payload["usd_invested"]) + " USD",
        "<b>Probabilidad:</b> " + str(payload["price"]) + "%",
        "<b>Wallet:</b> <code>" + payload["wallet"][:10] + "...</code>",
        "",
        '<a href="' + payload["url"] + '">\U0001f517 Ver en Polymarket</a>',
        "<i>\u23f0 " + payload["timestamp"] + "</i>",
        "",
        "<i>\U0001f510 Score, ROI y analisis completo en VIP</i>",
    ]
    return "\n".join(lines)

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
        # Advertencia si la diferencia es brutal
        if diff <= -15:
            aviso = f"\n⚠️ <b>PRECIO CAYÓ {diff:.1f}% desde la entrada — entrada temprana o ineficiencia</b>"
        elif diff >= 15:
            aviso = f"\n🚀 <b>PRECIO SUBIÓ +{diff:.1f}% desde la entrada — momentum confirmado</b>"
        else:
            aviso = ""
        precio_txt = (
            f"\n📊 <b>Precio entrada:</b> {payload['price']}%"
            f"\n{flecha} <b>Precio ahora:</b> {precio_actual}% "
            f"({'+'if diff>0 else ''}{diff:.1f}%)"
            f"{aviso}"
        )
    else:
        precio_txt = f"\n📊 <b>Probabilidad:</b> {payload['price']}%"

    if historial["total"] >= 3:
        hist_txt = f"\n📜 <b>Historial:</b> {historial['aciertos']}/{historial['total']} aciertos ({historial['tasa']}) {historial['emoji']}"
    elif historial["total"] > 0:
        hist_txt = f"\n📜 <b>Historial:</b> {historial['aciertos']}/{historial['total']} aciertos 🆕"
    else:
        hist_txt = "\n📜 <b>Historial:</b> Primera señal detectada 🆕"

    ficha        = get_ficha_ballena(apodo, payload["wallet"])
    ficha_txt    = ("\n\n" + ficha) if ficha else ""
    objetivo_txt = calcular_precio_objetivo(payload["price"], historial, payload["usd_invested"])

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
        f"{ficha_txt}"
        f"{objetivo_txt}"
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


# ══════════════════════════════════════════════════════════════
#  CONTADOR PUBLICO DE ACIERTOS
# ══════════════════════════════════════════════════════════════

def check_contador_basico():
    global ultimo_contador_basico
    ahora = datetime.now(timezone.utc)
    if ahora - ultimo_contador_basico < timedelta(hours=6):
        return
    ultimo_contador_basico = ahora
    log = cargar_signals()
    if not log:
        return
    total     = len(log)
    acertadas = sum(1 for s in log if s["resultado"] == "ACIERTO")
    falladas  = sum(1 for s in log if s["resultado"] == "FALLO")
    resueltas = acertadas + falladas
    if resueltas < 3:
        return
    tasa = str(round(acertadas / resueltas * 100)) + "%"
    lines = [
        "\U0001f4ca <b>Track record PolyWhales VIP</b>",
        "",
        "\U0001f40b <b>" + str(total) + " senales</b> detectadas",
        "\u2705 <b>" + str(acertadas) + " acertadas</b> -- tasa " + tasa,
        "\u23f3 " + str(total - resueltas) + " pendientes de resolver",
        "",
        "<i>Cuantas perdiste por no estar en VIP?</i>",
        "",
        '<a href="t.me/send?start=s-VIPaccess">\U0001f510 Unirse al VIP -- $15/mes</a>',
    ]
    if TELEGRAM_CHAT_ID_BASICO:
        enviar_telegram(TELEGRAM_CHAT_ID_BASICO, "\n".join(lines))
        print("   Contador basico enviado")

# ══════════════════════════════════════════════════════════════
#  BALLENA NUEVA
# ══════════════════════════════════════════════════════════════

def check_ballena_nueva(wallet: str, apodo: str, payload: dict) -> bool:
    if wallet.lower() in wallets_conocidas:
        return False
    wallets_conocidas.add(wallet.lower())
    if payload["usd_invested"] < 1000:
        return False
    lines = [
        "\U0001f195 <b>BALLENA NUEVA DETECTADA</b> \U0001f195",
        "",
        "Primera operacion registrada de esta wallet",
        "",
        "<b>Apodo asignado:</b> " + apodo,
        "<b>Mercado:</b> " + payload["market"],
        "<b>Posicion:</b> " + payload["side"] + " -> <b>" + payload["outcome"] + "</b>",
        "<b>Invertido:</b> $" + "{:,.2f}".format(payload["usd_invested"]) + " USD",
        "<b>Precio entrada:</b> " + str(payload["price"]) + "%",
        "",
        "<i>Insider? Institucion? Seguimiento activado</i>",
        "",
        '<a href="' + payload["url"] + '">\U0001f517 Ver mercado</a>',
    ]
    if TELEGRAM_CHAT_ID_VIP:
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, "\n".join(lines))
        print("   Ballena nueva: " + apodo)
    return True

# ══════════════════════════════════════════════════════════════
#  CONSENSO INSTITUCIONAL
# ══════════════════════════════════════════════════════════════

def check_consenso(wallet: str, slug: str, outcome: str, payload: dict):
    ahora  = datetime.now(timezone.utc)
    limite = ahora - timedelta(hours=1)
    clave  = slug + "::" + outcome.lower()
    consenso_tracker[clave] = [(w, ts) for w, ts in consenso_tracker[clave] if ts > limite]
    wallets_clave = [w for w, _ in consenso_tracker[clave]]
    if wallet.lower() not in wallets_clave:
        consenso_tracker[clave].append((wallet.lower(), ahora))
    if len(consenso_tracker[clave]) == 3:
        lines = [
            "\U0001f3db <b>CONSENSO INSTITUCIONAL</b> \U0001f3db",
            "",
            "<b>3 ballenas distintas</b> han apostado al mismo outcome en menos de 1 hora",
            "",
            "<b>Mercado:</b> " + payload["market"],
            "<b>Outcome:</b> <b>" + outcome + "</b>",
            "<b>Ultima entrada:</b> $" + "{:,.2f}".format(payload["usd_invested"]) + " USD",
            "",
            "<i>Cuando el dinero inteligente converge, el mercado suele seguirlo.</i>",
            "",
            '<a href="' + payload["url"] + '">\U0001f517 Ver mercado</a>',
        ]
        if TELEGRAM_CHAT_ID_VIP:
            enviar_telegram(TELEGRAM_CHAT_ID_VIP, "\n".join(lines))
            print("   Consenso institucional: " + slug[:25])

# ══════════════════════════════════════════════════════════════
#  FICHA DE BALLENA
# ══════════════════════════════════════════════════════════════

def get_ficha_ballena(apodo: str, wallet: str):
    from collections import Counter as Cnt
    log     = cargar_signals()
    senales = [s for s in log if s.get("apodo") == apodo]
    if len(senales) < 5:
        return None
    total_usd = sum(s.get("usd", 0) for s in senales)
    resueltas = [s for s in senales if s.get("resultado") in ("ACIERTO", "FALLO")]
    aciertos  = sum(1 for s in resueltas if s.get("resultado") == "ACIERTO")
    tasa      = str(round(aciertos / len(resueltas) * 100)) + "%" if resueltas else "En curso"
    mercados  = Cnt(s.get("mercado", "")[:30] for s in senales)
    merc_txt  = " | ".join(m for m, _ in mercados.most_common(2))
    outcomes  = Cnt(s.get("outcome", "") for s in senales)
    espec     = outcomes.most_common(1)[0][0] if outcomes else "Variada"
    return (
        "\U0001f4c4 <b>" + apodo + " -- Ficha</b>\n"
        "\U0001f4b0 Total invertido: $" + "{:,.0f}".format(total_usd) + "\n"
        "\U0001f3af Tasa de acierto: " + tasa + " (" + str(aciertos) + "/" + str(len(resueltas)) + ")\n"
        "\U0001f4cb Mercados: " + merc_txt + "\n"
        "\u26a1 Outcome frecuente: " + espec
    )

# ══════════════════════════════════════════════════════════════
#  RANKING SEMANAL DE BALLENAS
# ══════════════════════════════════════════════════════════════

def get_ranking_ballenas() -> str:
    from collections import Counter as Cnt
    log = cargar_signals()
    if not log:
        return ""
    hace_7 = datetime.now(timezone.utc) - timedelta(days=7)
    semana  = [
        s for s in log
        if datetime.strptime(s["fecha"], "%Y-%m-%d").replace(tzinfo=timezone.utc) >= hace_7
        and s.get("resultado") in ("ACIERTO", "FALLO")
    ]
    if not semana:
        return ""
    stats = {}
    for s in semana:
        a = s.get("apodo", "?")
        if a not in stats:
            stats[a] = {"aciertos": 0, "total": 0}
        stats[a]["total"] += 1
        if s.get("resultado") == "ACIERTO":
            stats[a]["aciertos"] += 1
    ranking = sorted(
        [(ap, d["aciertos"], d["total"]) for ap, d in stats.items() if d["total"] >= 2],
        key=lambda x: (x[1] / x[2] if x[2] > 0 else 0, x[2]),
        reverse=True
    )[:3]
    if not ranking:
        return ""
    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
    txt = "\n\U0001f3c6 <b>Ranking ballenas esta semana:</b>\n"
    for i, (apodo, aciertos, total) in enumerate(ranking):
        tasa = str(round(aciertos / total * 100)) + "%"
        txt += medals[i] + " <b>" + apodo + "</b> -- " + str(aciertos) + "/" + str(total) + " (" + tasa + ")\n"
    return txt


# ════════════════════════════════════════════════════════════════
#  COMANDO /ballena
# ════════════════════════════════════════════════════════════════

def generar_ficha_completa(apodo: str) -> str:
    log     = cargar_signals()
    senales = [s for s in log if s.get("apodo") == apodo]
    if not senales:
        return f"No encontré ninguna ballena con el apodo <b>{apodo}</b>."

    total_usd  = sum(s.get("usd", 0) for s in senales)
    resueltas  = [s for s in senales if s.get("resultado") in ("ACIERTO", "FALLO")]
    aciertos   = sum(1 for s in resueltas if s.get("resultado") == "ACIERTO")
    fallos     = len(resueltas) - aciertos
    pendientes = len(senales) - len(resueltas)
    tasa       = str(round(aciertos / len(resueltas) * 100)) + "%" if resueltas else "Sin datos"

    from collections import Counter as Cnt
    mercados  = Cnt(s.get("mercado", "")[:35] for s in senales)
    merc_txt  = ""
    for m, n in mercados.most_common(3):
        merc_txt += f"\n   • {m} ({n}x)"

    ultimas = senales[-5:][::-1]
    ult_txt = ""
    for s in ultimas:
        e = "✅" if s["resultado"] == "ACIERTO" else "❌" if s["resultado"] == "FALLO" else "⏳"
        ult_txt += f"\n{e} {s.get('mercado','?')[:35]}\n   {s.get('posicion','?')} | ${s.get('usd',0):,.0f} | Score {s.get('score','?')}"

    h = get_historial_ballena(apodo)

    return (
        f"🐋 <b>FICHA COMPLETA — {apodo}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Señales totales:</b> {len(senales)}\n"
        f"✅ <b>Acertadas:</b> {aciertos}\n"
        f"❌ <b>Falladas:</b> {fallos}\n"
        f"⏳ <b>Pendientes:</b> {pendientes}\n"
        f"🎯 <b>Tasa de acierto:</b> {tasa} {h['emoji']}\n"
        f"💰 <b>Total invertido:</b> ${total_usd:,.0f}\n\n"
        f"📋 <b>Mercados favoritos:</b>{merc_txt}\n\n"
        f"🕐 <b>Últimas jugadas:</b>{ult_txt}"
    )

# ════════════════════════════════════════════════════════════════
#  ALERTA DE SALIDA DE POSICIÓN
# ════════════════════════════════════════════════════════════════

# Guardamos las posiciones conocidas de cada ballena VIP
# {wallet: {conditionId: {"side": "BUY", "outcome": "Yes", "usd": 500}}}
posiciones_ballenas = defaultdict(dict)

def check_salida_posicion(wallet: str, apodo: str, trade: dict, payload: dict):
    condition_id  = trade.get("conditionId", "")
    outcome_index = int(trade.get("outcomeIndex", -1))
    side          = trade.get("side", "").upper()
    clave         = f"{condition_id}:{outcome_index}"

    entrada = posiciones_ballenas[wallet].get(clave)

    if entrada and entrada["side"] == "BUY" and side == "SELL":
        # Ballena está saliendo de una posición que tenía
        ganancia_txt = ""
        try:
            precio_actual = get_precio_actual(condition_id, outcome_index)
            if precio_actual and entrada.get("precio_entrada"):
                diff = precio_actual - entrada["precio_entrada"]
                ganancia_txt = f"\n📊 Entrada: {entrada['precio_entrada']}% → Ahora: {precio_actual}% ({'+'if diff>0 else ''}{diff:.1f}%)"
        except Exception:
            pass

        msg_lines = [
            "🚪 <b>BALLENA SALIENDO DE POSICIÓN</b>",
            "",
            f"🏷️ <b>{apodo}</b> está vendiendo",
            "",
            f"📋 <b>Mercado:</b> {payload['market']}",
            f"🎯 <b>Vendiendo:</b> {trade.get('outcome', '?')}",
            f"💰 <b>USD:</b> ${payload['usd_invested']:,.2f}",
        ]
        if ganancia_txt:
            msg_lines.append(ganancia_txt)
        msg_lines += [
            "",
            "<i>Cuando una ballena sale, el mercado puede girar.</i>",
            "",
            f'<a href="{payload["url"]}">🔗 Ver mercado</a>',
        ]
        if TELEGRAM_CHAT_ID_VIP:
            enviar_telegram(TELEGRAM_CHAT_ID_VIP, "\n".join(msg_lines))
            print(f"   🚪 Salida detectada: {apodo}")

    # Registrar posición actual
    if side == "BUY":
        posiciones_ballenas[wallet][clave] = {
            "side":           "BUY",
            "outcome":        trade.get("outcome", ""),
            "precio_entrada": payload["price"],
            "usd":            payload["usd_invested"],
        }
    elif side == "SELL" and clave in posiciones_ballenas[wallet]:
        del posiciones_ballenas[wallet][clave]

# ════════════════════════════════════════════════════════════════
#  PRECIO OBJETIVO AUTOMÁTICO
# ════════════════════════════════════════════════════════════════

def calcular_precio_objetivo(precio_entrada: float, historial: dict, usd: float) -> str:
    if historial["total"] < 3:
        return ""
    tasa_num = (historial["aciertos"] / historial["total"]) * 100 if historial["total"] > 0 else 0
    # Objetivo basado en historial: si acierta mucho, el precio suele llegar a 85%+
    if tasa_num >= 70:
        objetivo = min(precio_entrada + 20, 90)
    elif tasa_num >= 50:
        objetivo = min(precio_entrada + 12, 85)
    else:
        objetivo = min(precio_entrada + 8, 80)

    if objetivo <= precio_entrada:
        return ""

    puntos   = round(objetivo - precio_entrada, 1)
    potencial = round(usd * (puntos / precio_entrada), 0) if precio_entrada > 0 else 0
    return (
        f"\n🎯 <b>Precio objetivo:</b> {objetivo:.0f}% (+{puntos} puntos)"
        f"\n💸 <b>Potencial:</b> +${potencial:,.0f} sobre ${usd:,.0f} invertidos"
    )

# ════════════════════════════════════════════════════════════════
#  MAPA DE CALOR DIARIO
# ════════════════════════════════════════════════════════════════

def check_mapa_calor():
    global ultimo_resumen_nocturno
    ahora = datetime.now(CEST)
    # Enviar a las 20:00 CEST
    if ahora.hour != 20 or ahora.minute > 5:
        return
    if datetime.now(timezone.utc) - ultimo_resumen_nocturno < timedelta(hours=20):
        return
    ultimo_resumen_nocturno = datetime.now(timezone.utc)

    log = cargar_signals()
    if not log:
        return

    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hoy_signals = [s for s in log if s.get("fecha") == hoy]
    if not hoy_signals:
        return

    from collections import Counter as Cnt
    mercados = Cnt(s.get("mercado", "")[:40] for s in hoy_signals)
    top5     = mercados.most_common(5)
    if not top5:
        return

    top_txt = ""
    for i, (mercado, n) in enumerate(top5):
        calor = "🔥" * min(n, 4)
        top_txt += f"\n{i+1}. {calor} <b>{mercado}</b> — {n} ballena{'s' if n>1 else ''}"

    total_usd = sum(s.get("usd", 0) for s in hoy_signals)

    msg = (
        f"🗺️ <b>MAPA DE CALOR — HOY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>Volumen total ballenas:</b> ${total_usd:,.0f}\n"
        f"🐋 <b>Señales hoy:</b> {len(hoy_signals)}\n\n"
        f"🔥 <b>Mercados más activos:</b>{top_txt}"
    )
    if TELEGRAM_CHAT_ID_VIP:
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)
        print("   🗺️ Mapa de calor enviado")

# ════════════════════════════════════════════════════════════════
#  ALERTA CONTRARIAN
# ════════════════════════════════════════════════════════════════

def check_contrarian(precio: float, side: str, outcome: str, roi: float,
                     usd: float, apodo: str, payload: dict):
    # Si el mercado dice >75% para YES pero la ballena apuesta NO (o viceversa)
    es_contrarian = (
        (precio >= 0.75 and side.upper() == "BUY" and outcome.upper() in ("NO", "FALSE"))
        or
        (precio <= 0.25 and side.upper() == "BUY" and outcome.upper() in ("YES", "TRUE"))
    )
    if not es_contrarian or roi < 30 or usd < 500:
        return

    prob_display = round(precio * 100, 1)
    msg_lines = [
        "🔄 <b>APUESTA CONTRARIAN DETECTADA</b>",
        "",
        f"<b>{apodo}</b> va en contra del mercado",
        "",
        f"📋 <b>Mercado:</b> {payload['market']}",
        f"🎯 <b>Apuesta:</b> {side} → <b>{outcome}</b>",
        f"📊 <b>Precio mercado:</b> {prob_display}% (van contra esto)",
        f"💰 <b>Invertido:</b> ${usd:,.2f} USD",
        f"📈 <b>ROI wallet:</b> {roi:.1f}%",
        "",
        "<i>Las apuestas contrarian de wallets con ROI alto son señales muy valiosas.</i>",
        "",
        f'<a href="{payload["url"]}">🔗 Ver mercado</a>',
    ]
    if TELEGRAM_CHAT_ID_VIP:
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, "\n".join(msg_lines))
        print(f"   🔄 Contrarian: {apodo} | {outcome} al {prob_display}%")

# ════════════════════════════════════════════════════════════════
#  RACHA DE ACIERTOS
# ════════════════════════════════════════════════════════════════

def check_racha_aciertos(apodo: str):
    log      = cargar_signals()
    senales  = [s for s in log if s.get("apodo") == apodo and s.get("resultado") in ("ACIERTO", "FALLO")]
    if len(senales) < 3:
        return

    # Contar racha actual (desde el final)
    racha = 0
    for s in reversed(senales):
        if s["resultado"] == "ACIERTO":
            racha += 1
        else:
            break

    prev_racha = racha_aciertos.get(apodo, 0)
    racha_aciertos[apodo] = racha

    # Solo alertar cuando llega exactamente a 3 (no spam)
    if racha == 3 and prev_racha == 2:
        msg_lines = [
            "🔥 <b>RACHA ACTIVA — 3 ACIERTOS CONSECUTIVOS</b>",
            "",
            f"🐋 <b>{apodo}</b> está en racha",
            "",
            f"✅ 3 aciertos seguidos sin un solo fallo",
            f"📈 Su próxima señal lleva score bonus automático",
            "",
            "<i>Las rachas indican información privilegiada o análisis superior.</i>",
            "",
            f"Escribe /ballena {apodo} para ver su historial completo",
        ]
        if TELEGRAM_CHAT_ID_VIP:
            enviar_telegram(TELEGRAM_CHAT_ID_VIP, "\n".join(msg_lines))
            print(f"   🔥 Racha: {apodo} | 3 aciertos")

# ════════════════════════════════════════════════════════════════
#  RESUMEN NOCTURNO
# ════════════════════════════════════════════════════════════════

def check_resumen_nocturno():
    ahora = datetime.now(CEST)
    if ahora.hour != 22 or ahora.minute > 5:
        return
    if datetime.now(timezone.utc) - ultimo_resumen_nocturno < timedelta(hours=20):
        return

    log = cargar_signals()
    if not log:
        return

    hoy         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hoy_signals = [s for s in log if s.get("fecha") == hoy]
    if not hoy_signals:
        return

    acertadas  = sum(1 for s in hoy_signals if s["resultado"] == "ACIERTO")
    falladas   = sum(1 for s in hoy_signals if s["resultado"] == "FALLO")
    pendientes = sum(1 for s in hoy_signals if s["resultado"] == "PENDIENTE")
    resueltas  = acertadas + falladas
    tasa       = str(round(acertadas / resueltas * 100)) + "%" if resueltas > 0 else "Pendiente"
    scores     = [s.get("score", 0) for s in hoy_signals]
    score_med  = round(sum(scores) / len(scores)) if scores else 0
    mejor      = max(hoy_signals, key=lambda x: x.get("score", 0))
    total_usd  = sum(s.get("usd", 0) for s in hoy_signals)

    msg = (
        f"🌙 <b>RESUMEN NOCTURNO VIP</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🐋 <b>Señales hoy:</b> {len(hoy_signals)}\n"
        f"✅ <b>Acertadas:</b> {acertadas}\n"
        f"❌ <b>Falladas:</b> {falladas}\n"
        f"⏳ <b>Pendientes:</b> {pendientes}\n"
        f"🎯 <b>Tasa del día:</b> {tasa}\n"
        f"⚡ <b>Score medio:</b> {score_med}/100\n"
        f"💰 <b>Volumen ballenas:</b> ${total_usd:,.0f}\n\n"
        f"🏆 <b>Mejor jugada del día:</b>\n"
        f"   🐋 {mejor['apodo']} | Score {mejor.get('score', '?')}\n"
        f"   {mejor.get('mercado', '?')[:40]}\n"
        f"   {mejor.get('posicion', '?')} | ${mejor.get('usd', 0):,.0f}"
    )
    if TELEGRAM_CHAT_ID_VIP:
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)
        print("   🌙 Resumen nocturno enviado")

def poll():
    global ciclo_actual
    ciclo_actual += 1
    print(f"\n🔍 Ciclo {ciclo_actual} — {datetime.now(CEST).strftime('%H:%M:%S CEST')}")

    procesar_comandos()
    check_resumen_diario()
    check_resumen_semanal()
    check_contador_basico()
    check_mapa_calor()
    check_resumen_nocturno()
    resolver_pendientes()
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
            check_ballena_nueva(wallet, apodo, payload)
            check_consenso(wallet, slug, payload["outcome"], payload)
            check_salida_posicion(wallet, apodo, trade, payload)
            check_contrarian(precio, payload["side"], payload["outcome"], roi, usd, apodo, payload)

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

@app.route("/api/whop-webhook", methods=["POST"])
def whop_webhook():
    """
    Whop llama a este endpoint cuando alguien paga o cancela.
    Configurar en Whop -> Desarrollador -> Webhooks -> URL: https://tudominio.railway.app/api/whop-webhook
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "no data"}), 400

        event = data.get("event", "")
        print(f"   📦 Whop webhook: {event}")

        # Pago completado o suscripción renovada
        if event in ("membership.went_valid", "payment.succeeded"):
            membership = data.get("data", {})
            user       = membership.get("user", {})
            whop_id    = str(user.get("id", ""))
            nombre     = user.get("name") or user.get("username") or "VIP"
            telegram   = user.get("telegram_username", "")

            print(f"   💰 Nuevo VIP: {nombre} | Whop: {whop_id} | TG: {telegram}")

            # Guardar en DB — el chat_id se actualiza cuando el usuario escribe /start
            añadir_vip_user(f"whop_{whop_id}", nombre, whop_id)

            # Notificar al admin
            if TELEGRAM_CHAT_ID_VIP:
                enviar_telegram("1387775814",
                    f"💰 <b>NUEVO SUSCRIPTOR VIP</b>\n\n"
                    f"👤 {nombre}\n"
                    f"🆔 Whop ID: {whop_id}\n"
                    f"📱 Telegram: @{telegram if telegram else 'no vinculado'}\n\n"
                    f"<i>Cuando escriba /start al bot quedará activado automáticamente.</i>"
                )

        # Cancelación o expiración
        elif event in ("membership.went_invalid", "membership.expired"):
            membership = data.get("data", {})
            user       = membership.get("user", {})
            whop_id    = str(user.get("id", ""))
            nombre     = user.get("name") or user.get("username") or "VIP"

            print(f"   ❌ VIP cancelado: {nombre} | Whop: {whop_id}")
            eliminar_vip_user(f"whop_{whop_id}")

            enviar_telegram("1387775814",
                f"❌ <b>VIP CANCELADO</b>\n\n"
                f"👤 {nombre}\n"
                f"🆔 Whop ID: {whop_id}"
            )

        return jsonify({"ok": True})

    except Exception as e:
        print(f"   ⚠️  Whop webhook error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/whop-start", methods=["POST"])
def whop_start():
    """
    Cuando el usuario escribe /start al bot, si tiene whop_id pendiente
    actualizamos su chat_id real en la DB.
    """
    try:
        data    = request.get_json()
        chat_id = str(data.get("chat_id", ""))
        nombre  = data.get("nombre", "VIP")
        if not chat_id:
            return jsonify({"error": "no chat_id"}), 400
        añadir_vip_user(chat_id, nombre)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ════════════════════════════════════════════════════════════════
#  INICIO
# ════════════════════════════════════════════════════════════════

@app.route("/api/create-checkout-session", methods=["POST", "OPTIONS"])
def create_checkout_session():
    if request.method == "OPTIONS":
        return "", 200
    try:
        stripe.api_key = STRIPE_SECRET_KEY
        data      = request.get_json() or {}
        price_id  = data.get("price_id", STRIPE_PRICE_ID)
        base_url  = request.headers.get("Origin", "https://smart-money-pulse-59.adrianquintanarobles.workers.dev")

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            subscription_data={"trial_period_days": 3},
            success_url=f"https://t.me/PolyWhalesAutomatic_bot?start=stripe_vip",
            cancel_url=f"{base_url}/?cancelled=true",
            allow_promotion_codes=True,
            billing_address_collection="auto",
        )
        return jsonify({"url": session.url})
    except Exception as e:
        print(f"   ❌ Stripe checkout error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(request.get_json(), stripe.api_key)
    except Exception as e:
        print(f"   ⚠️  Stripe webhook error: {e}")
        return jsonify({"error": str(e)}), 400

    stripe.api_key = STRIPE_SECRET_KEY
    etype = event["type"]
    print(f"   📦 Stripe webhook: {etype}")

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        sub    = event["data"]["object"]
        status = sub.get("status", "")
        if status in ("active", "trialing"):
            customer_id = sub.get("customer", "")
            try:
                customer = stripe.Customer.retrieve(customer_id)
                nombre   = customer.get("name") or customer.get("email", "VIP")
                stripe_id = customer_id
                añadir_vip_user(f"stripe_{stripe_id}", nombre, stripe_id)
                enviar_telegram("1387775814",
                    f"💰 <b>NUEVO SUSCRIPTOR VIP — STRIPE</b>\n\n"
                    f"👤 {nombre}\n"
                    f"🆔 Stripe ID: {stripe_id}\n"
                    f"📊 Estado: {status}\n\n"
                    f"<i>Cuando escriba /start al bot quedará activado.</i>"
                )
                print(f"   ✅ Stripe VIP: {nombre}")
            except Exception as e:
                print(f"   ⚠️  Stripe customer error: {e}")

    elif etype == "customer.subscription.deleted":
        sub         = event["data"]["object"]
        customer_id = sub.get("customer", "")
        eliminar_vip_user(f"stripe_{customer_id}")
        enviar_telegram("1387775814",
            f"❌ <b>SUSCRIPCIÓN CANCELADA — STRIPE</b>\n\n"
            f"🆔 Stripe ID: {customer_id}"
        )
        print(f"   ❌ Stripe cancelación: {customer_id}")

    return jsonify({"ok": True})


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