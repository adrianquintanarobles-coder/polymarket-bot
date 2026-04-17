import requests
import time
import os
import random
from datetime import datetime, timezone
from collections import deque

# ── CONFIGURACIÓN ───────────────────────────────────────────────
TELEGRAM_BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID_BASICO  = os.getenv("TELEGRAM_CHAT_ID_BASICO")   # Canal gratuito
TELEGRAM_CHAT_ID_VIP     = os.getenv("TELEGRAM_CHAT_ID_VIP")      # Canal de pago
ANTHROPIC_API_KEY        = os.getenv("ANTHROPIC_API_KEY")

# ── UMBRALES POR TIER ───────────────────────────────────────────
MIN_USD_BASICO  = 50
MIN_ROI_BASICO  = 0
MIN_USD_VIP     = 500
MIN_ROI_VIP     = 10

POLL_INTERVAL   = 5
MAX_SEEN        = 3000

# ── ESTADO ──────────────────────────────────────────────────────
seen_hashes   = deque(maxlen=MAX_SEEN)
whale_cache   = {}
whale_streaks = {}
whale_apodos  = {}   # {wallet: apodo} — persiste en memoria durante la sesión

# ── APODOS ÉPICOS ────────────────────────────────────────────────
APODOS_EPICOS = [
    "El Oráculo", "El Arquitecto", "La Sombra", "El Mago de Washington",
    "El Tiburón Silencioso", "La Mano Invisible", "El Estratega",
    "El Profeta", "El Alquimista", "El Lobo Solitario",
    "La Ballena Blanca", "El Gran Maestro", "El Fantasma",
    "El Sabio del Mercado", "El Cazador de Tendencias"
]

# ── MERCADOS BASURA ──────────────────────────────────────────────
SLUGS_IGNORADOS = [
    "btc-updown", "eth-updown", "sol-updown",
    "matic-updown", "xrp-updown", "bnb-updown",
    "highest-temperature", "lowest-temperature",
    "will-the-price-of"
]

def es_mercado_basura(trade):
    slug  = trade.get("eventSlug", "").lower()
    title = trade.get("title", "").lower()
    return any(p in slug or p in title for p in SLUGS_IGNORADOS)

# ── VERIFICACIÓN DE WALLET ───────────────────────────────────────
def verificar_wallet(wallet):
    """
    Devuelve (roi, perfil_str) si pasa el mínimo básico,
    o (None, None) si es descartada.
    """
    wallet = wallet.lower()

    if wallet in whale_cache:
        return whale_cache[wallet]

    print(f"   🕵️  Analizando {wallet[:10]}...")

    try:
        r = requests.get(
            f"https://data-api.polymarket.com/profiles/{wallet}",
            timeout=5
        )
        if r.status_code == 404:
            whale_cache[wallet] = (None, None)
            return None, None

        r.raise_for_status()
        data = r.json()

        pnl          = float(data.get("pnl", 0))
        trades_count = int(data.get("tradesCount", 0))
        volume       = float(data.get("volume", 0))

        if pnl <= 0 or volume == 0 or trades_count < 5:
            whale_cache[wallet] = (None, None)
            return None, None

        roi    = (pnl / volume) * 100
        perfil = f"ROI {roi:.1f}% | Profit ${pnl:,.0f} | {trades_count} trades"

        if roi >= MIN_ROI_BASICO:
            whale_cache[wallet] = (roi, perfil)
            return roi, perfil

        whale_cache[wallet] = (None, None)
        return None, None

    except Exception:
        return None, None

# ── GAMIFICACIÓN: APODOS ─────────────────────────────────────────
def get_apodo(wallet):
    wallet = wallet.lower()
    if wallet not in whale_apodos:
        whale_apodos[wallet] = random.choice(APODOS_EPICOS)
    return whale_apodos[wallet]

# ── MEJORA 3A: NOTICIAS (DuckDuckGo, gratis) ────────────────────
def buscar_noticia(query):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            resultados = list(ddgs.news(query, max_results=1))
        if resultados:
            return resultados[0].get("title", "Sin titular")
    except Exception as e:
        print(f"   ⚠️  DuckDuckGo error: {e}")
    return None

# ── MEJORA 3B: ANÁLISIS IA CON CLAUDE ───────────────────────────
def analizar_con_claude(payload, noticia):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        contexto_noticia = f'Titular reciente: "{noticia}"' if noticia else "Sin noticias recientes encontradas."
        prompt = f"""Eres un analista financiero experto en mercados de predicción.
Analiza esta operación en Polymarket en MÁXIMO 3 líneas. Sé directo y concreto.

DATOS:
- Mercado: {payload['market']}
- Posición: {payload['side']} → {payload['outcome']}
- USD invertido: ${payload['usd_invested']:,.2f}
- Probabilidad implícita: {payload['price']}%
- Perfil del trader: {payload['perfil']}
- {contexto_noticia}

¿Cuál es el posible motivo institucional de esta jugada?"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"   ⚠️  Claude error: {e}")
        return None

# ── CONSTRUCCIÓN DE MENSAJES ─────────────────────────────────────
def mensaje_basico(payload):
    return f"""📡 <b>SEÑAL DETECTADA</b>

📋 <b>Mercado:</b> {payload['market']}
🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>
💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD
📊 <b>Probabilidad:</b> {payload['price']}%
🔑 <b>Wallet:</b> <code>{payload['wallet'][:10]}...</code>

🔗 <a href="{payload['url']}">Ver en Polymarket</a>

<i>⚠️ Canal básico — Actualiza a VIP para ver análisis completo.</i>"""

def mensaje_vip(payload, apodo, noticia, analisis, racha):
    racha_txt  = f"\n🔥 <b>RACHA: {racha} operaciones seguidas</b>" if racha >= 2 else ""
    noticia_txt = f"\n\n📰 <b>Contexto:</b> <i>{noticia}</i>" if noticia else ""
    analisis_txt = f"\n\n🤖 <b>Análisis IA:</b>\n{analisis}" if analisis else ""

    return f"""🐋 <b>ALERTA VIP — BALLENA VERIFICADA</b> 🐋{racha_txt}

🏷️ <b>Apodo:</b> {apodo}
📋 <b>Mercado:</b> {payload['market']}
🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>
💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD
📊 <b>Probabilidad:</b> {payload['price']}%
📈 <b>Perfil:</b> {payload['perfil']}
🔑 <b>Wallet:</b> <code>{payload['wallet'][:10]}...</code>{noticia_txt}{analisis_txt}

🔗 <a href="{payload['url']}">Ver mercado en Polymarket</a>
⏰ {payload['timestamp']}"""

# ── ENVÍO A TELEGRAM ─────────────────────────────────────────────
def enviar_telegram(chat_id, texto):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     texto,
                "parse_mode":               "HTML",
                "disable_web_page_preview": False
            },
            timeout=10
        )
        if r.status_code == 400:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": texto},
                timeout=10
            )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"   ❌ Error Telegram: {e}")
        return False

# ── BUCLE PRINCIPAL (MOTOR INTACTO) ─────────────────────────────
def poll():
    print(f"\n🔍 Escaneando... {datetime.now().strftime('%H:%M:%S')}")
    trades = []
    try:
        r = requests.get(
            "https://data-api.polymarket.com/trades?limit=100",
            timeout=10
        )
        r.raise_for_status()
        trades = r.json()
    except Exception as e:
        print(f"❌ Error API: {e}")
        return

    señales_basico = 0
    señales_vip    = 0

    for trade in trades:
        tx = trade.get("transactionHash", "")
        if not tx or tx in seen_hashes:
            continue
        seen_hashes.append(tx)

        # ── CÁLCULO BASE (INTACTO) ───────────────────────────────
        usd    = round(float(trade.get("size", 0)) * float(trade.get("price", 0)), 2)
        precio = float(trade.get("price", 0))
        wallet = trade.get("proxyWallet", "")

        if usd < MIN_USD_BASICO:          continue
        if es_mercado_basura(trade):      continue
        if not (0.01 <= precio <= 0.99):  continue
        if trade.get("side") != "BUY":    continue
        if not wallet:                    continue

        roi, perfil = verificar_wallet(wallet)
        if roi is None:
            continue

        # ── PAYLOAD BASE ─────────────────────────────────────────
        payload = {
            "wallet":       wallet,
            "side":         trade["side"],
            "outcome":      trade["outcome"],
            "usd_invested": usd,
            "price":        round(precio * 100, 1),
            "market":       trade["title"],
            "url":          f"https://polymarket.com/event/{trade.get('eventSlug', '')}",
            "tx_hash":      trade["transactionHash"],
            "timestamp":    datetime.fromtimestamp(trade["timestamp"], timezone.utc).strftime('%H:%M:%S UTC'),
            "perfil":       perfil,
            "roi":          roi
        }

        # ── TIER BÁSICO ──────────────────────────────────────────
        if usd >= MIN_USD_BASICO and roi >= MIN_ROI_BASICO and TELEGRAM_CHAT_ID_BASICO:
            if enviar_telegram(TELEGRAM_CHAT_ID_BASICO, mensaje_basico(payload)):
                print(f"   📡 Básico enviado: ${usd}")
                señales_basico += 1

        # ── TIER VIP ─────────────────────────────────────────────
        if usd >= MIN_USD_VIP and roi >= MIN_ROI_VIP and TELEGRAM_CHAT_ID_VIP:
            apodo  = get_apodo(wallet)
            whale_streaks[wallet] = whale_streaks.get(wallet, 0) + 1
            racha  = whale_streaks[wallet]

            print(f"   🐋 VIP detectada: {apodo} | ROI {roi:.1f}% | ${usd}")
            print(f"   📰 Buscando noticias...")
            noticia  = buscar_noticia(trade["title"])

            print(f"   🤖 Consultando Claude...")
            analisis = analizar_con_claude(payload, noticia)

            texto_vip = mensaje_vip(payload, apodo, noticia, analisis, racha)
            if enviar_telegram(TELEGRAM_CHAT_ID_VIP, texto_vip):
                print(f"   👑 VIP enviado: {apodo} | ${usd}")
                señales_vip += 1

        time.sleep(0.5)

    print(f"📊 Analizados: {len(trades)} | Básico: {señales_basico} | VIP: {señales_vip}")

# ── INICIO ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v3.0")
    print(f"   Básico: >${MIN_USD_BASICO} | VIP: >${MIN_USD_VIP} ROI>{MIN_ROI_VIP}%")
    print(f"   Intervalo: {POLL_INTERVAL}s")
    print("─" * 50)
    while True:
        try:
            poll()
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 Detenido.")
            break