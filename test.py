"""
Polymarket Smart Money Tracker v3.2
─────────────────────────────────────────────────────────────────
Fixes respecto a v3.1:
  - Modelo Claude corregido → claude-sonnet-4-5
  - ROI corregido → pnl / (volume - pnl) * 100
  - Anti-spam → misma wallet + mercado en <30min se ignora
  - Retry en Polymarket API → 3 intentos con backoff
  - guardar_estado() cada 10 ciclos, no en cada ciclo
  - whale_streaks limitado a 500 entradas en memoria
  - Filtro side eliminado → detecta BUY y SELL (posiciones SHORT)
  - Score de confianza (0-100) visible en mensajes VIP
  - Alerta mercado caliente → 3+ ballenas en <10min
"""

import os
import json
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from pathlib import Path

# ── CONFIGURACIÓN (variables de entorno en Railway) ──────────────
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID_BASICO = os.getenv("TELEGRAM_CHAT_ID_BASICO")
TELEGRAM_CHAT_ID_VIP    = os.getenv("TELEGRAM_CHAT_ID_VIP")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")

# ── UMBRALES ─────────────────────────────────────────────────────
MIN_USD_BASICO  = 50
MIN_ROI_BASICO  = 0
MIN_USD_VIP     = 500
MIN_ROI_VIP     = 10

# ── PARÁMETROS OPERACIONALES ─────────────────────────────────────
POLL_INTERVAL        = 5
MAX_SEEN             = 3000
CACHE_TTL_HORAS      = 6
WALLET_API_DELAY     = 0.8
CEBO_PROBABILIDAD    = 4
PERSIST_PATH         = Path("state.json")
SAVE_EVERY_N_CYCLES  = 10
ANTI_SPAM_MINUTOS    = 30
MERCADO_CALIENTE_N   = 3
MERCADO_CALIENTE_MIN = 10

# ── ESTADO EN MEMORIA ────────────────────────────────────────────
seen_hashes     = deque(maxlen=MAX_SEEN)
whale_cache     = {}
whale_streaks   = {}
whale_apodos    = {}
anti_spam       = {}
mercado_hits    = defaultdict(list)
ciclo_actual    = 0

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

# ════════════════════════════════════════════════════════════════
#  PERSISTENCIA
# ════════════════════════════════════════════════════════════════

def cargar_estado():
    global whale_apodos
    if not PERSIST_PATH.exists():
        return
    try:
        data = json.loads(PERSIST_PATH.read_text())
        whale_apodos.update(data.get("apodos", {}))
        for h in data.get("seen_hashes", []):
            seen_hashes.append(h)
        print(f"   💾 Estado cargado: {len(whale_apodos)} apodos | {len(seen_hashes)} hashes")
    except Exception as e:
        print(f"   ⚠️  No se pudo cargar estado: {e}")

def guardar_estado():
    try:
        data = {
            "apodos":      whale_apodos,
            "seen_hashes": list(seen_hashes)[-500:],
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
#  VERIFICACIÓN DE WALLET
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

    r = _get_with_retry(f"https://data-api.polymarket.com/profiles/{wallet}")
    if r is None:
        return None, None

    if r.status_code == 404:
        whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
        print("404")
        return None, None

    try:
        r.raise_for_status()
        data = r.json()

        pnl          = float(data.get("pnl", 0))
        trades_count = int(data.get("tradesCount", 0))
        volume       = float(data.get("volume", 0))

        if pnl <= 0 or volume == 0 or trades_count < 5:
            whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
            print(f"descartada (pnl={pnl:.0f}, trades={trades_count})")
            return None, None

        # ── ROI CORREGIDO ────────────────────────────────────────
        capital_arriesgado = volume - pnl
        if capital_arriesgado <= 0:
            capital_arriesgado = volume
        roi    = (pnl / capital_arriesgado) * 100
        perfil = f"ROI {roi:.1f}% | Profit ${pnl:,.0f} | {trades_count} trades"

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
#  SCORE DE CONFIANZA (0–100)
# ════════════════════════════════════════════════════════════════

def calcular_score(usd: float, roi: float, racha: int, caliente: bool) -> int:
    score = 0
    if usd >= 5000:   score += 40
    elif usd >= 2000: score += 30
    elif usd >= 1000: score += 20
    else:             score += 10
    if roi >= 50:     score += 30
    elif roi >= 25:   score += 20
    elif roi >= 10:   score += 10
    score += min(racha * 5, 20)
    if caliente:      score += 10
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

def buscar_noticia(query: str) -> str | None:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            resultados = list(ddgs.news(query, max_results=1))
        if resultados:
            return resultados[0].get("title")
    except Exception as e:
        print(f"   ⚠️  DuckDuckGo: {e}")
    return None

# ════════════════════════════════════════════════════════════════
#  ANÁLISIS IA
# ════════════════════════════════════════════════════════════════

def analizar_con_claude(payload: dict, noticia: str | None) -> str | None:
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
                "model":      "claude-sonnet-4-5",   # ← CORREGIDO
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

def mensaje_basico(payload: dict, es_cebo: bool = False) -> str:
    cebo_txt = ""
    if es_cebo:
        cebo_txt = (
            "\n\n⭐ <b>SEÑAL VIP FILTRADA</b>\n"
            "Esta wallet tiene perfil verificado y opera con sumas mayores.\n"
            "<i>En VIP recibes análisis completo, score de confianza, noticias y análisis IA.</i>"
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

def mensaje_vip(payload: dict, apodo: str, noticia: str | None,
                analisis: str | None, racha: int, score: int, caliente: bool) -> str:
    racha_txt    = f"\n🔥 <b>RACHA: {racha} ops en sesión</b>" if racha >= 2 else ""
    caliente_txt = "\n🌡️ <b>MERCADO CALIENTE — múltiples ballenas detectadas</b>" if caliente else ""
    noticia_txt  = f"\n\n📰 <b>Contexto:</b> <i>{noticia}</i>" if noticia else ""
    analisis_txt = f"\n\n🤖 <b>Análisis IA:</b>\n{analisis}" if analisis else ""

    return (
        f"🐋 <b>ALERTA VIP — BALLENA VERIFICADA</b> 🐋{racha_txt}{caliente_txt}\n\n"
        f"🏷️ <b>Apodo:</b> {apodo}\n"
        f"📋 <b>Mercado:</b> {payload['market']}\n"
        f"🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>\n"
        f"💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD\n"
        f"📊 <b>Probabilidad:</b> {payload['price']}%\n"
        f"📈 <b>Perfil:</b> {payload['perfil']}\n"
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
        f"🐋 <b>{n} ballenas han operado en los últimos {MERCADO_CALIENTE_MIN} minutos</b>\n\n"
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

        if usd < MIN_USD_BASICO:          continue
        if es_mercado_basura(trade):      continue
        if not (0.01 <= precio <= 0.99):  continue
        if not wallet:                    continue

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
            apodo = get_apodo(wallet)
            if len(whale_streaks) >= 500:
                del whale_streaks[next(iter(whale_streaks))]
            whale_streaks[wallet] = whale_streaks.get(wallet, 0) + 1
            racha = whale_streaks[wallet]
            score = calcular_score(usd, roi, racha, caliente)

            print(f"   🐋 VIP: {apodo} | ROI {roi:.1f}% | ${usd} | Score {score}")
            noticia  = buscar_noticia(trade.get("title", ""))
            analisis = analizar_con_claude(payload, noticia)

            if enviar_telegram(TELEGRAM_CHAT_ID_VIP, mensaje_vip(payload, apodo, noticia, analisis, racha, score, caliente)):
                print(f"   👑 VIP enviado: {apodo} | ${usd}")
                señales_vip += 1

            if caliente and TELEGRAM_CHAT_ID_BASICO:
                n_hits = len(mercado_hits.get(slug, []))
                enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_mercado_caliente(slug, payload["market"], n_hits))

        # ── BÁSICO ───────────────────────────────────────────────
        if TELEGRAM_CHAT_ID_BASICO:
            es_cebo = False
            if not es_vip and usd >= MIN_USD_BASICO and roi >= MIN_ROI_BASICO:
                pasa = True
            elif es_vip and random.randint(1, CEBO_PROBABILIDAD) == 1:
                pasa = True; es_cebo = True
            else:
                pasa = False

            if pasa:
                if enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_basico(payload, es_cebo)):
                    print(f"   {'🎣 Cebo' if es_cebo else '📡 Básico'} enviado: ${usd}")
                    señales_basico += 1

        time.sleep(0.5)

    if ciclo_actual % SAVE_EVERY_N_CYCLES == 0:
        guardar_estado()

    print(f"📊 Trades: {len(trades)} | Básico: {señales_basico} | VIP: {señales_vip} | Cache: {len(whale_cache)}")

# ════════════════════════════════════════════════════════════════
#  INICIO
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v3.2")
    print(f"   Básico : >${MIN_USD_BASICO} USD | ROI >{MIN_ROI_BASICO}%")
    print(f"   VIP    : >${MIN_USD_VIP} USD | ROI >{MIN_ROI_VIP}%")
    print(f"   Cebo   : 1/{CEBO_PROBABILIDAD} señales VIP al básico")
    print(f"   Anti-spam: {ANTI_SPAM_MINUTOS}min | Cache TTL: {CACHE_TTL_HORAS}h")
    print(f"   Mercado caliente: {MERCADO_CALIENTE_N}+ ballenas en {MERCADO_CALIENTE_MIN}min")
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