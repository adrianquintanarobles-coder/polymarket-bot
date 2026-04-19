"""
Polymarket Smart Money Tracker v3.8
─────────────────────────────────────────────────────────────────
Nuevas mejoras respecto a v3.7:
  1. Historial de ballenas → cada apodo muestra aciertos/fallos históricos
     en el mensaje VIP: "El Oráculo | 8/10 aciertos (80%) 🔥"
  2. Alta convicción → si la misma wallet vuelve al mismo mercado en <24h
     manda alerta especial al VIP: "⚡ DOBLE APUESTA DETECTADA"
  3. Divergencia de precio → si una ballena compra YES pero el precio baja
     en los 5 min siguientes, alerta de ineficiencia al VIP
"""

import os
import json
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from pathlib import Path

# ── CONFIGURACIÓN ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID_BASICO = os.getenv("TELEGRAM_CHAT_ID_BASICO")
TELEGRAM_CHAT_ID_VIP    = os.getenv("TELEGRAM_CHAT_ID_VIP")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")

# ── UMBRALES ─────────────────────────────────────────────────────
MIN_USD_BASICO      = 50
MIN_ROI_BASICO      = 0
MIN_USD_VIP         = 500
MAX_USD_BASICO      = 499
MIN_ROI_VIP         = 10
PRECIO_MIN          = 0.15
PRECIO_MAX          = 0.85

# ── PARÁMETROS OPERACIONALES ─────────────────────────────────────
POLL_INTERVAL            = 5
MAX_SEEN                 = 3000
CACHE_TTL_HORAS          = 6
WALLET_API_DELAY         = 0.3
CEBO_PROBABILIDAD        = 4
PERSIST_PATH             = Path("state.json")
SIGNALS_LOG_PATH         = Path("signals_log.json")
SAVE_EVERY_N_CYCLES      = 10
ANTI_SPAM_MINUTOS        = 30
MERCADO_CALIENTE_N       = 3
MERCADO_CALIENTE_MIN     = 10
RESOLVER_CADA_HORAS      = 1
ALTA_CONVICCION_HORAS    = 24   # ventana para detectar doble apuesta
DIVERGENCIA_MINUTOS      = 5    # minutos para revisar si el precio bajó

# ── ESTADO EN MEMORIA ────────────────────────────────────────────
seen_hashes          = deque(maxlen=MAX_SEEN)
whale_cache          = {}
whale_streaks        = {}
whale_apodos         = {}
anti_spam            = {}
mercado_hits         = defaultdict(list)
ciclo_actual         = 0
ultimo_resumen       = datetime.now(timezone.utc)
ultima_resolucion    = datetime.now(timezone.utc) - timedelta(hours=2)
ultimo_update_id     = 0

# ── NUEVOS: estado para mejoras ──────────────────────────────────
# {wallet: {slug: datetime}} — última vez que operó en cada mercado
wallet_mercado_ultima_vez = defaultdict(dict)

# Cola de divergencia: [(condition_id, outcome_index, precio_entrada, apodo, payload, ts)]
cola_divergencia = deque(maxlen=200)

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

# ── PIN STATE ────────────────────────────────────────────────────
mensaje_pinned_id    = None
ultimo_pin           = None
ultimo_lunes_enviado = None
ultimo_limpieza      = datetime.now(timezone.utc) - timedelta(hours=25)

# ════════════════════════════════════════════════════════════════
#  MEJORA 1: HISTORIAL DE BALLENAS
# ════════════════════════════════════════════════════════════════

def get_historial_ballena(apodo: str) -> dict:
    """
    Calcula aciertos/fallos históricos de una ballena a partir del signals_log.
    Devuelve {"aciertos": int, "fallos": int, "total": int, "tasa": str, "emoji": str}
    """
    log = cargar_signals()
    señales_ballena = [s for s in log if s.get("apodo") == apodo and s.get("resultado") != "PENDIENTE"]

    total    = len(señales_ballena)
    aciertos = sum(1 for s in señales_ballena if s.get("resultado") == "ACIERTO")
    fallos   = sum(1 for s in señales_ballena if s.get("resultado") == "FALLO")

    if total == 0:
        return {"aciertos": 0, "fallos": 0, "total": 0, "tasa": "Nuevo", "emoji": "🆕"}

    tasa_num = (aciertos / total) * 100
    tasa_str = f"{tasa_num:.0f}%"

    if tasa_num >= 75:   emoji = "🔥🔥🔥"
    elif tasa_num >= 60: emoji = "🔥🔥"
    elif tasa_num >= 50: emoji = "🔥"
    else:                emoji = "⚠️"

    return {
        "aciertos": aciertos,
        "fallos":   fallos,
        "total":    total,
        "tasa":     tasa_str,
        "emoji":    emoji,
    }

def texto_historial(apodo: str) -> str:
    h = get_historial_ballena(apodo)
    if h["total"] == 0:
        return "🆕 <b>Primera señal detectada</b>"
    return (
        f"📜 <b>Historial:</b> {h['aciertos']}/{h['total']} aciertos "
        f"({h['tasa']}) {h['emoji']}"
    )

# ════════════════════════════════════════════════════════════════
#  MEJORA 2: ALTA CONVICCIÓN (DOBLE APUESTA)
# ════════════════════════════════════════════════════════════════

def check_alta_conviccion(wallet: str, slug: str, apodo: str, payload: dict) -> bool:
    """
    Devuelve True si esta wallet ya operó en este mercado en las últimas
    ALTA_CONVICCION_HORAS horas. Si es así, manda alerta especial al VIP.
    """
    ahora  = datetime.now(timezone.utc)
    limite = ahora - timedelta(hours=ALTA_CONVICCION_HORAS)

    ultima = wallet_mercado_ultima_vez[wallet].get(slug)

    # Actualizamos el registro
    wallet_mercado_ultima_vez[wallet][slug] = ahora

    if ultima and ultima > limite:
        # ¡Doble apuesta detectada!
        diff_horas = (ahora - ultima).seconds // 3600
        diff_min   = ((ahora - ultima).seconds % 3600) // 60
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
#  MEJORA 3: DIVERGENCIA DE PRECIO
# ════════════════════════════════════════════════════════════════

def registrar_para_divergencia(condition_id: str, outcome_index: int,
                                precio_entrada: float, apodo: str, payload: dict):
    """Añade la señal a la cola de divergencia para revisión posterior."""
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
    """
    Revisa las señales en cola. Si han pasado DIVERGENCIA_MINUTOS y el precio
    bajó ≥3% respecto a la entrada, manda alerta de divergencia al VIP.
    """
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

            precio_actual = round(float(prices[entrada["outcome_index"]]) * 100, 1)
            precio_entrada = entrada["precio_entrada"]
            diff = precio_actual - precio_entrada

            # Solo alerta si bajó ≥3 puntos porcentuales
            if diff <= -3:
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
#  SIGNALS LOG
# ════════════════════════════════════════════════════════════════

def cargar_signals() -> list:
    if not SIGNALS_LOG_PATH.exists():
        return []
    try:
        return json.loads(SIGNALS_LOG_PATH.read_text())
    except Exception:
        return []

def guardar_signals(log: list):
    try:
        SIGNALS_LOG_PATH.write_text(json.dumps(log[-500:], indent=2))
    except Exception as e:
        print(f"   ⚠️  No se pudo guardar signals: {e}")

def guardar_señal(payload: dict, apodo: str, score: int, trade: dict):
    try:
        log = cargar_signals()
        log.append({
            "timestamp":    payload["timestamp"],
            "fecha":        datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            "apodo":        apodo,
            "wallet":       payload["wallet"][:12],
            "mercado":      payload["market"],
            "posicion":     f"{payload['side']} → {payload['outcome']}",
            "outcome":      payload["outcome"],
            "outcomeIndex": int(trade.get("outcomeIndex", -1)),
            "conditionId":  trade.get("conditionId", ""),
            "usd":          payload["usd_invested"],
            "prob":         payload["price"],
            "roi_wallet":   round(payload["roi"], 1),
            "score":        score,
            "url":          payload["url"],
            "resultado":    "PENDIENTE",
        })
        guardar_signals(log)
    except Exception as e:
        print(f"   ⚠️  No se pudo guardar señal: {e}")

# ════════════════════════════════════════════════════════════════
#  RESOLUCIÓN AUTOMÁTICA
# ════════════════════════════════════════════════════════════════

def resolver_pendientes():
    global ultima_resolucion
    ahora = datetime.now(timezone.utc)
    if ahora - ultima_resolucion < timedelta(hours=RESOLVER_CADA_HORAS):
        return

    ultima_resolucion = ahora
    log = cargar_signals()
    pendientes = [s for s in log if s.get("resultado") == "PENDIENTE" and s.get("conditionId")]

    if not pendientes:
        return

    print(f"   🔍 Revisando {len(pendientes)} señales pendientes...")
    actualizadas = 0

    for señal in pendientes:
        condition_id  = señal.get("conditionId", "")
        outcome_index = señal.get("outcomeIndex", -1)
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

            outcome_prices_raw = mercado.get("outcomePrices", "[]")
            outcome_prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw

            if outcome_index >= len(outcome_prices):
                continue

            precio_final = float(outcome_prices[outcome_index])

            if precio_final >= 0.9:
                señal["resultado"] = "ACIERTO"
                emoji = "✅"
            elif precio_final <= 0.1:
                señal["resultado"] = "FALLO"
                emoji = "❌"
            else:
                continue

            actualizadas += 1
            print(f"   {emoji} Resuelto: {señal['apodo']} → {señal['resultado']}")

            if TELEGRAM_CHAT_ID_VIP:
                h = get_historial_ballena(señal["apodo"])
                historial_txt = f"\n📜 Historial actualizado: {h['aciertos']}/{h['total']} ({h['tasa']}) {h['emoji']}" if h["total"] > 0 else ""
                msg = (
                    f"{emoji} <b>RESULTADO CONFIRMADO</b>\n\n"
                    f"🏷️ <b>Apodo:</b> {señal['apodo']}\n"
                    f"📋 <b>Mercado:</b> {señal['mercado']}\n"
                    f"🎯 <b>Posición:</b> {señal['posicion']}\n"
                    f"💰 <b>Invertido:</b> ${señal['usd']:,.2f} USD\n"
                    f"📊 <b>Prob. entrada:</b> {señal['prob']}%\n"
                    f"🎯 <b>Score:</b> {señal['score']}/100"
                    f"{historial_txt}\n\n"
                    f"🔗 <a href=\"{señal['url']}\">Ver mercado</a>"
                )
                enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)

            time.sleep(0.5)

        except Exception as e:
            print(f"   ⚠️  Error resolviendo: {e}")
            continue

    if actualizadas > 0:
        guardar_signals(log)
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

    ultimas = log[-5:][::-1]
    ultimas_txt = ""
    for s in ultimas:
        emoji = "✅" if s["resultado"] == "ACIERTO" else "❌" if s["resultado"] == "FALLO" else "⏳"
        ultimas_txt += f"\n{emoji} <b>{s['apodo']}</b>\n"
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
    semana = [s for s in log if datetime.strptime(s["fecha"], '%Y-%m-%d').replace(tzinfo=timezone.utc) >= hace_7_dias]
    if not semana:
        return

    total_sem  = len(semana)
    acertadas  = sum(1 for s in semana if s["resultado"] == "ACIERTO")
    falladas   = sum(1 for s in semana if s["resultado"] == "FALLO")
    pendientes = sum(1 for s in semana if s["resultado"] == "PENDIENTE")
    resueltas  = acertadas + falladas
    tasa       = f"{(acertadas/resueltas*100):.0f}%" if resueltas > 0 else "En curso"

    top = sorted(semana, key=lambda x: x.get("score", 0), reverse=True)[:3]
    top_txt = ""
    for s in top:
        emoji = "✅" if s["resultado"] == "ACIERTO" else "❌" if s["resultado"] == "FALLO" else "⏳"
        h = get_historial_ballena(s["apodo"])
        top_txt += f"\n{emoji} <b>{s['apodo']}</b> ({h['tasa']} histórico)\n   {s['mercado'][:40]}\n   Score {s.get('score','?')}\n"

    msg = (
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
        enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)
        print("   📅 Resumen semanal enviado")

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

            canales = {TELEGRAM_CHAT_ID_VIP, TELEGRAM_CHAT_ID_BASICO}
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
        msg     = update.get("message", {})
        nuevos  = msg.get("new_chat_members", [])
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
#  SCORE DE CONFIANZA
# ════════════════════════════════════════════════════════════════

def calcular_score(usd: float, roi: float, racha: int, caliente: bool, historial: dict) -> int:
    score = 0
    if usd >= 5000:   score += 35
    elif usd >= 2000: score += 25
    elif usd >= 1000: score += 15
    else:             score += 8
    if roi >= 75:     score += 25
    elif roi >= 50:   score += 20
    elif roi >= 25:   score += 12
    elif roi >= 10:   score += 6
    score += min(racha * 4, 15)
    if caliente:      score += 8
    # Bonus por historial
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
#  MENSAJES
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

    # Historial de la ballena
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
    print(f"\n🔍 Ciclo {ciclo_actual} — {datetime.now().strftime('%H:%M:%S')}")

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
            ts = datetime.fromtimestamp(int(trade["timestamp"]), timezone.utc).strftime('%H:%M:%S UTC')
        except Exception:
            ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')

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

            # Mejora 2: check alta convicción
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

                # Mejora 3: registrar para divergencia
                registrar_para_divergencia(condition_id, outcome_index, payload["price"], apodo, payload)

            if caliente and TELEGRAM_CHAT_ID_BASICO:
                n_hits = len(mercado_hits.get(slug, []))
                enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_mercado_caliente(slug, payload["market"], n_hits))

        # ── BÁSICO ───────────────────────────────────────────────
        if TELEGRAM_CHAT_ID_BASICO:
            es_cebo = False
            if not es_vip and usd >= MIN_USD_BASICO and usd <= MAX_USD_BASICO and roi >= MIN_ROI_BASICO:
                pasa = True
            elif es_vip and random.randint(1, CEBO_PROBABILIDAD) == 1:
                pasa = True; es_cebo = True
            else:
                pasa = False

            if pasa:
                if enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_basico(payload, es_cebo)):
                    print(f"   {'🎣 Cebo' if es_cebo else '📡 Básico'} enviado: ${usd}")
                    señales_basico += 1
                    stats_dia["señales_basico"] += 1
                    guardar_estado()

        time.sleep(0.5)

    guardar_estado()
    print(f"📊 Trades: {len(trades)} | Básico: {señales_basico} | VIP: {señales_vip} | Cache: {len(whale_cache)}")

# ════════════════════════════════════════════════════════════════
#  INICIO
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v3.8")
    print(f"   Básico : >${MIN_USD_BASICO}–${MAX_USD_BASICO} USD | ROI >{MIN_ROI_BASICO}%")
    print(f"   VIP    : >${MIN_USD_VIP} USD | ROI >{MIN_ROI_VIP}%")
    print(f"   Precio : {int(PRECIO_MIN*100)}%–{int(PRECIO_MAX*100)}%")
    print(f"   Nuevas : Historial ballenas | Alta convicción | Divergencia")
    print("─" * 50)

    cargar_estado()

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