import requests
import json
import time
import os
from datetime import datetime, timezone
from collections import deque

# ── CONFIGURACIÓN ───────────────────────────────────────────────
# Ahora el bot buscará las llaves en el "sistema" de la nube
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")

MIN_TRADE_USD  = 1000
POLL_INTERVAL  = 5
MAX_SEEN       = 3000

# ── ESTADO ──────────────────────────────────────────────────────
seen_hashes    = deque(maxlen=MAX_SEEN)
whale_cache    = {}   # Cache de wallets ya verificadas {wallet: bool}
whale_streaks = {}   # Diccionario para contar racha de señales {wallet: contador}

# ── MERCADOS BASURA (ruido puro) ─────────────────────────────────
SLUGS_IGNORADOS = [
    "btc-updown", "eth-updown", "sol-updown",
    "matic-updown", "xrp-updown", "bnb-updown",
    "highest-temperature", "lowest-temperature",
    "will-the-price-of"
]

def es_mercado_basura(trade):
    slug = trade.get("eventSlug", "").lower()
    title = trade.get("title", "").lower()
    for patron in SLUGS_IGNORADOS:
        if patron in slug or patron in title:
            return True
    return False

# ── VERIFICACIÓN DE WALLET (REEMPLAZA THE GRAPH) ─────────────────
def es_ballena_rentable(wallet):
    wallet = wallet.lower()
    
    # 1. Si ya la hemos verificado hoy, devolvemos la pareja (bool, etiqueta) guardada
    if wallet in whale_cache:
        return whale_cache[wallet]

    print(f"   🕵️ Analizando historial de {wallet[:10]}...")

    try:
        r = requests.get(f"https://data-api.polymarket.com/profiles/{wallet}", timeout=5)
        
        # Si es nueva, la descartamos. Queremos historial probado.
        if r.status_code == 404:
            whale_cache[wallet] = (False, "NUEVA") 
            return False, None
        
        r.raise_for_status()
        data = r.json()

        pnl = float(data.get("pnl", 0))
        trades_count = int(data.get("tradesCount", 0))
        volume = float(data.get("volume", 0))

        # ── LA MÁQUINA DE FILTRADO ESTRICTO ──
        
        # 1. Si no ha ganado dinero o no tiene historial suficiente, a la basura
        if pnl <= 0 or volume == 0 or trades_count < 5:
            whale_cache[wallet] = (False, "PERDEDOR/NOVATO")
            return False, None

        # 2. Calculamos su ROI (Beneficio / Volumen apostado)
        roi = (pnl / volume) * 100

        # 3. Solo pasa si su ROI es superior al 10% (Es un trader top)
        if roi >= 10:
            res = (True, f"🎯 SNIPER PRO (ROI: {roi:.1f}% | Profit: ${pnl:,.0f})")
            whale_cache[wallet] = res
            return res
        else:
            # Gana dinero, pero falla mucho (ROI bajo). Lo ignoramos.
            whale_cache[wallet] = (False, "TRADER MEDIOCRE")
            return False, None

    except Exception as e:
        return False, None

# ── FILTRO PRINCIPAL ─────────────────────────────────────────────
# REEMPLAZA TU FUNCIÓN es_señal POR ESTA:
def es_señal(trade, usd):
    # En todos los descartes, devolvemos False y None
    if usd < MIN_TRADE_USD: return False, None
    if es_mercado_basura(trade): return False, None
    
    precio = float(trade.get("price", 0))
    if not (0.01 <= precio <= 0.99): return False, None
    if trade.get("side") != "BUY": return False, None
    
    wallet = trade.get("proxyWallet", "")
    if not wallet: return False, None

    # Si llega aquí, llama a la otra función que ya devuelve (True/False, Etiqueta)
    return es_ballena_rentable(wallet)

# ── FORMATEAR PAYLOAD ────────────────────────────────────────────
# Reemplaza tu función actual por esta
def formatear_alerta(trade, usd, perfil, racha):
    return {
        "wallet":       trade["proxyWallet"],
        "side":         trade["side"],
        "outcome":      trade["outcome"],
        "usd_invested": usd,
        "price":        round(float(trade["price"]) * 100, 1),
        "market":       trade["title"],
        "url":          f"https://polymarket.com/event/{trade.get('eventSlug', '')}",
        "tx_hash":      trade["transactionHash"],
        "timestamp":    datetime.fromtimestamp(trade["timestamp"], timezone.utc).strftime('%H:%M:%S UTC'),
        "perfil":       perfil,  # <--- NUEVO
        "racha":        racha    # <--- NUEVO
    }

# ── MENSAJE PLANTILLA (hasta que actives Claude) ─────────────────
def construir_mensaje(payload):
    # Lógica de la Medalla de Racha
    racha_txt = ""
    if payload['racha'] >= 2:
        racha_txt = f"\n🔥 <b>RACHA: {payload['racha']} operaciones seguidas!</b>"

    return f"""🐋 <b>ALERTA DE BALLENA</b> 🐋
{racha_txt}

📋 <b>Mercado:</b> {payload['market']}
🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>
💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD
📊 <b>Probabilidad:</b> {payload['price']}%

🏷️ <b>Perfil:</b> {payload['perfil']}
🔑 <b>Wallet:</b> <code>{payload['wallet'][:10]}...</code>

🔗 <a href="{payload['url']}">Ver mercado en Polymarket</a>"""

# ── ENVÍO A TELEGRAM ─────────────────────────────────────────────
def enviar_telegram(payload):
    mensaje = construir_mensaje(payload)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     mensaje,
                "parse_mode":               "HTML",
                "disable_web_page_preview": False
            },
            timeout=10
        )
        if r.status_code == 400:
            # Fallback a texto plano si falla el HTML
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje},
                timeout=10
            )
        r.raise_for_status()
        print(f"   📲 ¡Alerta enviada a Telegram! ${payload['usd_invested']}")
    except Exception as e:
        print(f"   ❌ Error Telegram: {e}")

# ── BUCLE PRINCIPAL ──────────────────────────────────────────────
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

    señales = 0
    # Busca este trozo dentro de tu función poll() y cámbialo:
    for trade in trades:
        tx = trade.get("transactionHash", "")
        if not tx or tx in seen_hashes:
            continue
        seen_hashes.append(tx)

        usd = round(float(trade.get("size", 0)) * float(trade.get("price", 0)), 2)

        # 1. Recogemos el booleano (pasa) y el texto (perfil)
        pasa, perfil = es_señal(trade, usd)
        
        if not pasa:
            continue

        # 2. Calculamos la racha antes de formatear
        wallet = trade["proxyWallet"]
        whale_streaks[wallet] = whale_streaks.get(wallet, 0) + 1
        racha_actual = whale_streaks[wallet]

        # 3. Le pasamos todo a la nueva función
        payload = formatear_alerta(trade, usd, perfil, racha_actual)
        enviar_telegram(payload)
        señales += 1
        time.sleep(1)

    print(f"📊 Analizados: {len(trades)} | Señales reales: {señales}")

# ── INICIO ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v2.0")
    print(f"   Umbral: ${MIN_TRADE_USD} | Intervalo: {POLL_INTERVAL}s")
    print("─" * 50)
    while True:
        try:
            poll()
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 Detenido.")
            break