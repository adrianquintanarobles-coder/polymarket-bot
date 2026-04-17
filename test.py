"""
Polymarket Smart Money Tracker v3.1
─────────────────────────────────────────────────────────────────
Mejoras respecto a v3.0:
  - Cache de wallets con TTL (6h) → evita aprobar wallets degradadas
  - Rate limiting interno → protege contra ban de la API de Polymarket
  - Lógica de "cebo" recuperada → señales VIP filtradas al básico para conversión
  - Persistencia ligera en JSON → survive Railway restarts
  - Cálculo de racha real → solo cuenta ops consecutivas en la misma sesión
  - Fallback robusto en noticias y Claude → nunca bloquea el flujo
  - Mensaje básico mejorado → más FOMO, más conversión
"""

import os
import json
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from collections import deque
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
POLL_INTERVAL        = 5          # segundos entre ciclos
MAX_SEEN             = 3000       # hashes recordados
CACHE_TTL_HORAS      = 6         # tiempo de vida del cache de wallets
WALLET_API_DELAY     = 0.8       # segundos entre llamadas al perfil de wallet
CEBO_PROBABILIDAD    = 4         # 1 de cada N señales VIP se filtra al básico
PERSIST_PATH         = Path("state.json")   # archivo de persistencia

# ── ESTADO EN MEMORIA ────────────────────────────────────────────
seen_hashes   = deque(maxlen=MAX_SEEN)
whale_cache   = {}   # wallet → {"roi": float, "perfil": str, "ts": datetime}
whale_streaks = {}   # wallet → int (racha en sesión actual)
whale_apodos  = {}   # wallet → str (apodo asignado, persiste en sesión)

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
    """Carga apodos y hashes vistos desde disco al arrancar."""
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
    """Persiste apodos y hashes vistos en disco."""
    try:
        data = {
            "apodos":      whale_apodos,
            "seen_hashes": list(seen_hashes)[-500:],  # solo los últimos 500
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

# ════════════════════════════════════════════════════════════════
#  VERIFICACIÓN DE WALLET (con TTL)
# ════════════════════════════════════════════════════════════════

def verificar_wallet(wallet: str):
    """
    Retorna (roi, perfil_str) o (None, None).
    Usa cache con TTL de CACHE_TTL_HORAS para no re-consultar wallets recientes.
    """
    wallet = wallet.lower()
    ahora  = datetime.now(timezone.utc)

    # ── Cache hit ────────────────────────────────────────────────
    if wallet in whale_cache:
        entrada = whale_cache[wallet]
        edad    = ahora - entrada["ts"]
        if edad < timedelta(hours=CACHE_TTL_HORAS):
            return entrada["roi"], entrada["perfil"]
        # expirado → borramos y reconsultamos
        del whale_cache[wallet]

    print(f"   🕵️  Analizando {wallet[:10]}... ", end="", flush=True)
    time.sleep(WALLET_API_DELAY)   # rate limiting

    try:
        r = requests.get(
            f"https://data-api.polymarket.com/profiles/{wallet}",
            timeout=6
        )
        if r.status_code == 404:
            whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
            print("404")
            return None, None

        r.raise_for_status()
        data = r.json()

        pnl          = float(data.get("pnl", 0))
        trades_count = int(data.get("tradesCount", 0))
        volume       = float(data.get("volume", 0))

        if pnl <= 0 or volume == 0 or trades_count < 5:
            whale_cache[wallet] = {"roi": None, "perfil": None, "ts": ahora}
            print(f"descartada (pnl={pnl:.0f}, trades={trades_count})")
            return None, None

        roi    = (pnl / volume) * 100
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
#  APODOS
# ════════════════════════════════════════════════════════════════

def get_apodo(wallet: str) -> str:
    wallet = wallet.lower()
    if wallet not in whale_apodos:
        whale_apodos[wallet] = random.choice(APODOS_EPICOS)
    return whale_apodos[wallet]

# ════════════════════════════════════════════════════════════════
#  NOTICIAS (DuckDuckGo)
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
#  ANÁLISIS IA (Claude)
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
                "model":      "claude-sonnet-4-20250514",
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
#  CONSTRUCCIÓN DE MENSAJES
# ════════════════════════════════════════════════════════════════

def mensaje_basico(payload: dict, es_cebo: bool = False) -> str:
    """
    Mensaje para el canal gratuito.
    Si es_cebo=True, es una señal VIP filtrada → incluye aviso de conversión.
    """
    cebo_txt = ""
    if es_cebo:
        cebo_txt = (
            "\n\n⭐ <b>SEÑAL VIP FILTRADA</b>\n"
            f"Esta wallet tiene un perfil verificado y opera con sumas mayores.\n"
            "<i>En VIP recibes análisis completo, apodo del trader, noticias y análisis IA.</i>"
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
                analisis: str | None, racha: int) -> str:
    racha_txt   = f"\n🔥 <b>RACHA: {racha} ops en sesión</b>" if racha >= 2 else ""
    noticia_txt = f"\n\n📰 <b>Contexto:</b> <i>{noticia}</i>" if noticia else ""
    analisis_txt = f"\n\n🤖 <b>Análisis IA:</b>\n{analisis}" if analisis else ""

    return (
        f"🐋 <b>ALERTA VIP — BALLENA VERIFICADA</b> 🐋{racha_txt}\n\n"
        f"🏷️ <b>Apodo:</b> {apodo}\n"
        f"📋 <b>Mercado:</b> {payload['market']}\n"
        f"🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>\n"
        f"💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD\n"
        f"📊 <b>Probabilidad:</b> {payload['price']}%\n"
        f"📈 <b>Perfil:</b> {payload['perfil']}\n"
        f"🔑 <b>Wallet:</b> <code>{payload['wallet'][:10]}...</code>"
        f"{noticia_txt}{analisis_txt}\n\n"
        f"🔗 <a href=\"{payload['url']}\">Ver mercado en Polymarket</a>\n"
        f"⏰ {payload['timestamp']}"
    )

# ════════════════════════════════════════════════════════════════
#  ENVÍO A TELEGRAM
# ════════════════════════════════════════════════════════════════

def enviar_telegram(chat_id: str, texto: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("   ❌ Token o chat_id no configurado")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id":                  chat_id,
                "text":                     texto,
                "parse_mode":               "HTML",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        if resp.status_code == 400:
            # Fallback sin HTML por si hay caracteres problemáticos
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": texto},
                timeout=10,
            )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")
        return False

# ════════════════════════════════════════════════════════════════
#  BUCLE PRINCIPAL
# ════════════════════════════════════════════════════════════════

def poll():
    print(f"\n🔍 Escaneando... {datetime.now().strftime('%H:%M:%S')}")

    try:
        r = requests.get(
            "https://data-api.polymarket.com/trades?limit=100",
            timeout=10,
        )
        r.raise_for_status()
        trades = r.json()
    except Exception as e:
        print(f"❌ Error API Polymarket: {e}")
        return

    señales_basico = 0
    señales_vip    = 0

    for trade in trades:
        tx = trade.get("transactionHash", "")
        if not tx or tx in seen_hashes:
            continue
        seen_hashes.append(tx)

        # ── Cálculo USD real ─────────────────────────────────────
        try:
            usd    = round(float(trade.get("size", 0)) * float(trade.get("price", 0)), 2)
            precio = float(trade.get("price", 0))
        except (ValueError, TypeError):
            continue

        wallet = trade.get("proxyWallet", "")

        # ── Filtros rápidos (sin llamadas API) ───────────────────
        if usd < MIN_USD_BASICO:          continue
        if es_mercado_basura(trade):      continue
        if not (0.01 <= precio <= 0.99):  continue
        if trade.get("side") != "BUY":    continue
        if not wallet:                    continue

        # ── Verificación de wallet ───────────────────────────────
        roi, perfil = verificar_wallet(wallet)
        if roi is None:
            continue

        # ── Payload base ─────────────────────────────────────────
        try:
            ts = datetime.fromtimestamp(
                int(trade["timestamp"]), timezone.utc
            ).strftime('%H:%M:%S UTC')
        except Exception:
            ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')

        payload = {
            "wallet":       wallet,
            "side":         trade.get("side", "BUY"),
            "outcome":      trade.get("outcome", ""),
            "usd_invested": usd,
            "price":        round(precio * 100, 1),
            "market":       trade.get("title", "Sin título"),
            "url":          f"https://polymarket.com/event/{trade.get('eventSlug', '')}",
            "tx_hash":      tx,
            "timestamp":    ts,
            "perfil":       perfil,
            "roi":          roi,
        }

        es_vip = usd >= MIN_USD_VIP and roi >= MIN_ROI_VIP

        # ────────────────────────────────────────────────────────
        #  TIER VIP
        # ────────────────────────────────────────────────────────
        if es_vip and TELEGRAM_CHAT_ID_VIP:
            apodo = get_apodo(wallet)
            whale_streaks[wallet] = whale_streaks.get(wallet, 0) + 1
            racha = whale_streaks[wallet]

            print(f"   🐋 VIP: {apodo} | ROI {roi:.1f}% | ${usd}")
            print(f"   📰 Buscando noticias...")
            noticia  = buscar_noticia(trade.get("title", ""))
            print(f"   🤖 Consultando Claude...")
            analisis = analizar_con_claude(payload, noticia)

            txt_vip = mensaje_vip(payload, apodo, noticia, analisis, racha)
            if enviar_telegram(TELEGRAM_CHAT_ID_VIP, txt_vip):
                print(f"   👑 VIP enviado: {apodo} | ${usd}")
                señales_vip += 1

        # ────────────────────────────────────────────────────────
        #  TIER BÁSICO
        #  - Siempre si cumple umbral básico y NO es VIP
        #  - Si es VIP → cebo probabilístico (1 de cada CEBO_PROBABILIDAD)
        # ────────────────────────────────────────────────────────
        if TELEGRAM_CHAT_ID_BASICO:
            es_cebo = False

            if not es_vip and usd >= MIN_USD_BASICO and roi >= MIN_ROI_BASICO:
                pasa_al_basico = True
            elif es_vip and random.randint(1, CEBO_PROBABILIDAD) == 1:
                pasa_al_basico = True
                es_cebo        = True
            else:
                pasa_al_basico = False

            if pasa_al_basico:
                txt_basico = mensaje_basico(payload, es_cebo=es_cebo)
                if enviar_telegram(TELEGRAM_CHAT_ID_BASICO, txt_basico):
                    tipo = "🎣 Cebo básico" if es_cebo else "📡 Básico"
                    print(f"   {tipo} enviado: ${usd}")
                    señales_basico += 1

        time.sleep(0.5)

    # Guardamos estado al final de cada ciclo
    guardar_estado()
    print(f"📊 Trades: {len(trades)} | Básico: {señales_basico} | VIP: {señales_vip} | Cache: {len(whale_cache)} wallets")

# ════════════════════════════════════════════════════════════════
#  INICIO
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v3.1")
    print(f"   Básico : >${MIN_USD_BASICO} USD | ROI >{MIN_ROI_BASICO}%")
    print(f"   VIP    : >${MIN_USD_VIP} USD | ROI >{MIN_ROI_VIP}%")
    print(f"   Cebo   : 1 de cada {CEBO_PROBABILIDAD} señales VIP al básico")
    print(f"   Cache TTL: {CACHE_TTL_HORAS}h | Intervalo: {POLL_INTERVAL}s")
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
            time.sleep(10)   # espera antes de reintentar