"""
Polymarket Smart Money Tracker v3.6 - Bot + API Combined
────────────────────────────────────────────────────────
Un único proceso que corre:
  1. El bot (rastrea Polymarket, envía alertas, análisis IA, noticias)
  2. Flask API (expone endpoints para la página)
  3. Stripe webhooks (pagos)
  
TODO EN UN SOLO ARCHIVO SIN PÉRDIDA DE FUNCIONALIDAD
"""

import os
import json
import time
import random
import requests
import stripe
import threading
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS

# ══════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════

# ── Bot Config ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID_BASICO = os.getenv("TELEGRAM_CHAT_ID_BASICO")
TELEGRAM_CHAT_ID_VIP    = os.getenv("TELEGRAM_CHAT_ID_VIP")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")

# ── API Config ─────────────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET")
TELEGRAM_CHANNEL_LINK  = os.getenv("TELEGRAM_CHANNEL_LINK", "t.me/+8Nl-SQBk7JkYTQ0")

stripe.api_key = STRIPE_SECRET_KEY

# ── Shared Paths ───────────────────────────────────────────────────
SIGNALS_LOG_PATH = Path("signals_log.json")
PAYMENTS_LOG = Path("payments.json")
PERSIST_PATH = Path("state.json")

# ── Bot Thresholds ────────────────────────────────────────────────
MIN_USD_BASICO      = 50
MIN_ROI_BASICO      = 0
MIN_USD_VIP         = 500
MAX_USD_BASICO      = 499
MIN_ROI_VIP         = 10
PRECIO_MIN          = 0.15
PRECIO_MAX          = 0.85

# ── Bot Operational ───────────────────────────────────────────────
POLL_INTERVAL        = 5
MAX_SEEN             = 3000
CACHE_TTL_HORAS      = 6
WALLET_API_DELAY     = 0.3
CEBO_PROBABILIDAD    = 4
ANTI_SPAM_MINUTOS    = 30
MERCADO_CALIENTE_N   = 3
MERCADO_CALIENTE_MIN = 10
RESOLVER_CADA_HORAS  = 1

# ── Bot State ──────────────────────────────────────────────────────
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

stats_dia = {
    "señales_vip":    0,
    "señales_basico": 0,
    "wallets_vip":    set(),
    "mercados_vip":   [],
}

# ── Constants ──────────────────────────────────────────────────────
APODOS_EPICOS = [
    "El Oráculo", "El Arquitecto", "La Sombra", "El Mago de Washington",
    "El Tiburón Silencioso", "La Mano Invisible", "El Estratega",
    "El Profeta", "El Alquimista", "El Lobo Solitario",
    "La Ballena Blanca", "El Gran Maestro", "El Fantasma",
    "El Sabio del Mercado", "El Cazador de Tendencias",
    "El Señor del Margen", "La Serpiente Fría", "El Coloso",
]

SLUGS_IGNORADOS = [
    "btc-updown", "eth-updown", "sol-updown",
    "matic-updown", "xrp-updown", "bnb-updown",
    "highest-temperature", "lowest-temperature",
    "will-the-price-of", "crypto-", "bitcoin-price",
]

MENSAJE_BIENVENIDA = """👋 <b>Bienvenido al canal de señales gratuito</b>

Aquí recibirás alertas en tiempo real de traders rentables operando en Polymarket.

📡 <b>Este canal (GRATIS):</b>
• Trades de $50 a $500
• Wallet y mercado verificados
• Señales en tiempo real

🐋 <b>Canal VIP ($15/mes):</b>
• Ballenas gordas +$500
• Score de confianza 0-100
• Precio entrada vs precio actual
• Análisis IA de cada operación
• Noticias del mercado
• Track record con tasa de acierto
• Resumen semanal automático

👇 <b>Acceso inmediato al pagar:</b>
<a href="t.me/send?start=s-VIPaccess">🔐 Unirse al VIP — $15/mes</a>"""

MENSAJE_PIN_VIP = """🐋 <b>¿Quieres las ballenas gordas?</b>

Este canal es GRATIS y muestra señales de $50–$500.

En el canal <b>VIP ($15/mes)</b> recibes:
✅ Ballenas de +$500 USD
✅ Score de confianza 0–100
✅ Precio de entrada vs precio actual
✅ Análisis IA de cada jugada
✅ Noticias del mercado en tiempo real
✅ Track record con tasa de acierto verificada
✅ Resumen semanal automático

<b>Los traders que seguimos tienen ROI >10% verificado.</b>

👇 <b>Acceso inmediato al pagar:</b>
<a href="t.me/send?start=s-VIPaccess">🔐 Unirse al VIP — $15/mes</a>"""

# ── Pin State ──────────────────────────────────────────────────────
mensaje_pinned_id = None
ultimo_pin        = None
ultimo_lunes_enviado = None
ultimo_limpieza   = datetime.now(timezone.utc) - timedelta(hours=25)

# ══════════════════════════════════════════════════════════════════
#  FLASK APP
# ══════════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app, 
     resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"]}})

# ── Signals API ────────────────────────────────────────────────────

@app.route("/api/signals", methods=["GET"])
def get_signals():
    """Devuelve todas las señales registradas (últimas 50)"""
    try:
        if not SIGNALS_LOG_PATH.exists():
            return jsonify({"signals": [], "total": 0})
        
        with open(SIGNALS_LOG_PATH) as f:
            signals = json.load(f)
        
        signals_recientes = signals[-50:][::-1]
        
        return jsonify({
            "signals": signals_recientes,
            "total": len(signals),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/signals/vip", methods=["GET"])
def get_signals_vip():
    """Devuelve solo señales VIP (>$500)"""
    try:
        if not SIGNALS_LOG_PATH.exists():
            return jsonify({"signals": [], "total": 0})
        
        with open(SIGNALS_LOG_PATH) as f:
            signals = json.load(f)
        
        vip_signals = [s for s in signals if s.get("usd", 0) >= 500]
        vip_recientes = vip_signals[-30:][::-1]
        
        return jsonify({
            "signals": vip_recientes,
            "total": len(vip_signals),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/signals/latest", methods=["GET"])
def get_latest_signal():
    """Devuelve la última señal registrada"""
    try:
        if not SIGNALS_LOG_PATH.exists():
            return jsonify({"signal": None})
        
        with open(SIGNALS_LOG_PATH) as f:
            signals = json.load(f)
        
        if signals:
            return jsonify({"signal": signals[-1], "timestamp": datetime.now(timezone.utc).isoformat()})
        return jsonify({"signal": None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Devuelve estadísticas del track record"""
    try:
        if not SIGNALS_LOG_PATH.exists():
            return jsonify({
                "total": 0,
                "aciertos": 0,
                "fallos": 0,
                "pendientes": 0,
                "tasa_acierto": "0%",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        
        with open(SIGNALS_LOG_PATH) as f:
            signals = json.load(f)
        
        total = len(signals)
        aciertos = sum(1 for s in signals if s.get("resultado") == "ACIERTO")
        fallos = sum(1 for s in signals if s.get("resultado") == "FALLO")
        pendientes = sum(1 for s in signals if s.get("resultado") == "PENDIENTE")
        resueltas = aciertos + fallos
        tasa = f"{(aciertos/resueltas*100):.0f}%" if resueltas > 0 else "N/A"
        
        return jsonify({
            "total": total,
            "aciertos": aciertos,
            "fallos": fallos,
            "pendientes": pendientes,
            "tasa_acierto": tasa,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Stripe API ─────────────────────────────────────────────────────

def cargar_pagos():
    if not PAYMENTS_LOG.exists():
        return []
    try:
        return json.loads(PAYMENTS_LOG.read_text())
    except Exception:
        return []

def guardar_pago(customer_email: str, session_id: str, link_telegram: str):
    """Guarda el pago en el log."""
    try:
        pagos = cargar_pagos()
        pagos.append({
            "email": customer_email,
            "session_id": session_id,
            "link_telegram": link_telegram,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "enviado": False,
        })
        PAYMENTS_LOG.write_text(json.dumps(pagos, indent=2))
    except Exception as e:
        print(f"❌ Error guardando pago: {e}")

def enviar_email_con_link(email: str, nombre: str, link_telegram: str) -> bool:
    """Envía email con el link de acceso VIP."""
    try:
        resend_api_key = os.getenv("RESEND_API_KEY")
        if resend_api_key:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": "noreply@smartmoneytracker.com",
                    "to": email,
                    "subject": "🐋 Your VIP Access is Ready — Polymarket Smart Money Tracker",
                    "html": f"""
                    <h2>Welcome to VIP! 🎉</h2>
                    <p>Hi {nombre},</p>
                    <p>Your payment has been confirmed. Click below to join the exclusive VIP channel:</p>
                    <p style="margin: 20px 0;">
                        <a href="{link_telegram}" style="background-color: #00FF88; color: #080B10; padding: 12px 24px; text-decoration: none; border-radius: 8px; font-weight: bold; display: inline-block;">
                            Join VIP Channel Now →
                        </a>
                    </p>
                    <p>This link is unique and can only be used once.</p>
                    <p><strong>What you get:</strong></p>
                    <ul>
                        <li>🐋 Whale signals +$500 USD</li>
                        <li>⚡ Confidence Score 0–100</li>
                        <li>🧠 Claude AI Analysis</li>
                        <li>📰 Breaking news context</li>
                        <li>📊 Auto-audited track record</li>
                    </ul>
                    <p>Questions? Reply to this email or visit our site.</p>
                    <p>The whales are moving right now. Are you? 🚀</p>
                    """,
                },
                timeout=10,
            )
            if response.status_code == 200:
                print(f"✅ Email enviado a {email}")
                return True
        else:
            print("⚠️  RESEND_API_KEY no configurada")
            return False
    except Exception as e:
        print(f"❌ Error enviando email: {e}")
        return False

def notificar_vip_telegram(email: str, link: str):
    """Notifica al admin del VIP por Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        mensaje = (
            f"✅ <b>NUEVO PAGO VIP</b>\n\n"
            f"📧 Email: {email}\n"
            f"🔗 Link: <code>{link}</code>\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": os.getenv("TELEGRAM_CHAT_ID_VIP"),
                "text": mensaje,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"⚠️  No se pudo notificar por Telegram: {e}")

@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """Recibe webhooks de Stripe."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        print(f"❌ Invalid payload: {e}")
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError as e:
        print(f"❌ Invalid signature: {e}")
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] in ["charge.succeeded", "checkout.session.completed", "payment_intent.succeeded"]:
        customer_email = None
        
        if event["type"] == "charge.succeeded":
            charge = event["data"]["object"]
            customer_email = charge.get("billing_details", {}).get("email") or charge.get("receipt_email")
            session_id = charge["id"]
        elif event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            customer_email = session.get("customer_email")
            session_id = session["id"]
        elif event["type"] == "payment_intent.succeeded":
            intent = event["data"]["object"]
            session_id = intent["id"]
            customer_id = intent.get("customer")
            if customer_id:
                try:
                    customer = stripe.Customer.retrieve(customer_id)
                    customer_email = customer.get("email")
                except:
                    pass
        
        if customer_email:
            print(f"\n💳 Pago recibido: {customer_email}")
            link_telegram = TELEGRAM_CHANNEL_LINK
            guardar_pago(customer_email, session_id, link_telegram)
            enviar_email_con_link(customer_email, "VIP Member", link_telegram)
            notificar_vip_telegram(customer_email, link_telegram)
            return jsonify({"success": True}), 200

    return jsonify({"status": "received"}), 200

@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Crea una sesión de Stripe Checkout."""
    try:
        data = request.get_json()
        email = data.get("email")
        product_id = data.get("productId")
        
        if not email or not product_id:
            return jsonify({"error": "Email and product ID required"}), 400
        
        products = stripe.Product.list(ids=[product_id])
        if not products.data:
            return jsonify({"error": "Product not found"}), 404
        
        product = products.data[0]
        prices = stripe.Price.list(product=product_id, type="recurring")
        if not prices.data:
            return jsonify({"error": "Price not found"}), 404
        
        price_id = prices.data[0].id
        
        session = stripe.checkout.Session.create(
            customer_email=email,
            payment_method_types=["card"],
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            mode="subscription",
            success_url=f"{os.getenv('FRONTEND_URL', 'https://smart-money-pulse-59.adrianquintanarobles.workers.dev')}/?success=true",
            cancel_url=f"{os.getenv('FRONTEND_URL', 'https://smart-money-pulse-59.adrianquintanarobles.workers.dev')}/?canceled=true",
        )
        
        return jsonify({
            "clientSecret": session.url,
            "sessionId": session.id,
        }), 200
        
    except Exception as e:
        print(f"❌ Error creating checkout: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/health", methods=["GET"])
def health():
    """Health check"""
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route("/", methods=["GET"])
def index():
    """Info del API"""
    return jsonify({
        "name": "Polymarket Smart Money Tracker v3.6 API",
        "endpoints": {
            "/api/signals": "Últimas 50 señales",
            "/api/signals/vip": "Últimas 30 señales VIP (>$500)",
            "/api/signals/latest": "Última señal registrada",
            "/api/stats": "Estadísticas del track record",
            "/api/create-checkout-session": "Crear sesión de pago Stripe",
            "/api/stripe-webhook": "Webhook de Stripe",
            "/api/health": "Health check"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - SIGNALS LOG
# ══════════════════════════════════════════════════════════════════

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
    """Guarda señal VIP con conditionId y outcomeIndex para resolución automática."""
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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - RESOLUCIÓN AUTOMÁTICA
# ══════════════════════════════════════════════════════════════════

def resolver_pendientes():
    """
    Consulta la API de Polymarket para cada señal PENDIENTE.
    Si el mercado resolvió, marca ACIERTO o FALLO automáticamente.
    """
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
        condition_id = señal.get("conditionId", "")
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
            try:
                if isinstance(outcome_prices_raw, str):
                    outcome_prices = json.loads(outcome_prices_raw)
                else:
                    outcome_prices = outcome_prices_raw
            except Exception:
                continue

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
                msg = (
                    f"{emoji} <b>RESULTADO CONFIRMADO</b>\n\n"
                    f"🏷️ <b>Apodo:</b> {señal['apodo']}\n"
                    f"📋 <b>Mercado:</b> {señal['mercado']}\n"
                    f"🎯 <b>Posición:</b> {señal['posicion']}\n"
                    f"💰 <b>Invertido:</b> ${señal['usd']:,.2f} USD\n"
                    f"📊 <b>Prob. entrada:</b> {señal['prob']}%\n"
                    f"🎯 <b>Score:</b> {señal['score']}/100\n\n"
                    f"🔗 <a href=\"{señal['url']}\">Ver mercado</a>"
                )
                enviar_telegram(TELEGRAM_CHAT_ID_VIP, msg)

            time.sleep(0.5)

        except Exception as e:
            print(f"   ⚠️  Error resolviendo {condition_id[:10]}: {e}")
            continue

    if actualizadas > 0:
        guardar_signals(log)
        print(f"   ✅ {actualizadas} señales resueltas automáticamente")

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - COMANDOS TELEGRAM
# ══════════════════════════════════════════════════════════════════

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
        ultimas_txt += f"   {s['posicion']} | Score {s['score']}\n"

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
            msg    = update.get("message", {})
            texto  = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            canales = {TELEGRAM_CHAT_ID_VIP, TELEGRAM_CHAT_ID_BASICO}
            if chat_id not in canales:
                continue

            if "/resultados" in texto:
                print(f"   📩 /resultados desde {chat_id}")
                enviar_telegram(chat_id, generar_texto_resultados())

    except Exception as e:
        print(f"   ⚠️  Comandos: {e}")

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - GESTIÓN DEL CANAL
# ══════════════════════════════════════════════════════════════════

def fijar_mensaje_vip():
    """Fija el mensaje de promoción VIP arriba del canal básico."""
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
        print("   📌 Mensaje VIP fijado en canal básico")
    except Exception as e:
        print(f"   ⚠️  Pin VIP: {e}")

def limpiar_mensajes_antiguos():
    """Borra mensajes de más de 7 días del canal básico."""
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
            print(f"   🗑️  {borrados} mensajes antiguos borrados")
    except Exception as e:
        print(f"   ⚠️  Limpieza: {e}")

def procesar_nuevos_miembros(updates: list):
    """Detecta nuevos miembros y manda bienvenida."""
    for update in updates:
        msg     = update.get("message", {})
        nuevos  = msg.get("new_chat_members", [])
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if nuevos and chat_id == TELEGRAM_CHAT_ID_BASICO:
            print("   👋 Nuevo miembro en básico")
            enviar_telegram(TELEGRAM_CHAT_ID_BASICO, MENSAJE_BIENVENIDA)

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - RESÚMENES
# ══════════════════════════════════════════════════════════════════

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

    top = sorted(semana, key=lambda x: x["score"], reverse=True)[:3]
    top_txt = ""
    for s in top:
        emoji = "✅" if s["resultado"] == "ACIERTO" else "❌" if s["resultado"] == "FALLO" else "⏳"
        top_txt += f"\n{emoji} <b>{s['apodo']}</b>\n   {s['mercado'][:40]}\n   Score {s['score']} | ROI wallet {s['roi_wallet']}%\n"

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - PERSISTENCIA
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - FILTROS
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - HTTP CON RETRY
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - VERIFICACIÓN DE WALLET
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - SCORE & APODOS
# ══════════════════════════════════════════════════════════════════

def calcular_score(usd: float, roi: float, racha: int, caliente: bool) -> int:
    score = 0
    if usd >= 5000:   score += 40
    elif usd >= 2000: score += 30
    elif usd >= 1000: score += 20
    else:             score += 10
    if roi >= 75:     score += 30
    elif roi >= 50:   score += 25
    elif roi >= 25:   score += 15
    score += min(racha * 5, 20)
    if caliente:      score += 10
    return min(score, 100)

def score_emoji(score: int) -> str:
    if score >= 80: return "🔥🔥🔥"
    if score >= 60: return "🔥🔥"
    if score >= 40: return "🔥"
    return "⚡"

def get_apodo(wallet: str) -> str:
    wallet = wallet.lower()
    if wallet not in whale_apodos:
        whale_apodos[wallet] = random.choice(APODOS_EPICOS)
    return whale_apodos[wallet]

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - NOTICIAS
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - ANÁLISIS IA
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - MENSAJE HELPERS
# ══════════════════════════════════════════════════════════════════

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

def get_precio_actual(condition_id: str, outcome_index: int) -> float | None:
    """Consulta el precio actual del mercado en tiempo real."""
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
        mercado = mercados[0] if isinstance(mercados, list) else mercados
        prices_raw = mercado.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if outcome_index < len(prices):
            return round(float(prices[outcome_index]) * 100, 1)
    except Exception:
        pass
    return None

def mensaje_vip(payload: dict, apodo: str, noticia, analisis,
                racha: int, score: int, caliente: bool,
                precio_actual: float | None = None) -> str:
    racha_txt    = f"\n🔥 <b>RACHA: {racha} ops en sesión</b>" if racha >= 2 else ""
    caliente_txt = "\n🌡️ <b>MERCADO CALIENTE — múltiples ballenas detectadas</b>" if caliente else ""
    noticia_txt  = f"\n\n📰 <b>Contexto:</b> <i>{noticia}</i>" if noticia else ""
    analisis_txt = f"\n\n🤖 <b>Análisis IA:</b>\n{analisis}" if analisis else ""

    if precio_actual and precio_actual != payload['price']:
        diff = precio_actual - payload['price']
        flecha = "📈" if diff > 0 else "📉"
        precio_txt = (
            f"\n📊 <b>Precio entrada ballena:</b> {payload['price']}%"
            f"\n{flecha} <b>Precio actual ahora:</b> {precio_actual}% "
            f"({'+'if diff>0 else ''}{diff:.1f}%)"
        )
    else:
        precio_txt = f"\n📊 <b>Probabilidad:</b> {payload['price']}%"

    return (
        f"🐋 <b>ALERTA VIP — BALLENA VERIFICADA</b> 🐋{racha_txt}{caliente_txt}\n\n"
        f"🏷️ <b>Apodo:</b> {apodo}\n"
        f"📋 <b>Mercado:</b> {payload['market']}\n"
        f"🎯 <b>Posición:</b> {payload['side']} → <b>{payload['outcome']}</b>\n"
        f"💰 <b>Invertido:</b> ${payload['usd_invested']:,.2f} USD"
        f"{precio_txt}\n"
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
        f"🐋 <b>{n} ballenas en los últimos {MERCADO_CALIENTE_MIN} minutos</b>\n\n"
        f"🔗 <a href=\"https://polymarket.com/event/{slug}\">Ver mercado</a>\n\n"
        f"<i>Señal de alta convicción institucional.</i>"
    )

# ══════════════════════════════════════════════════════════════════
#  BOT FUNCTIONS - TELEGRAM
# ══════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════
#  BOT MAIN LOOP
# ══════════════════════════════════════════════════════════════════

def bot_loop():
    global ciclo_actual
    print("\n🚀 Bot iniciado")
    cargar_estado()
    
    while True:
        try:
            ciclo_actual += 1
            print(f"\n🔍 Ciclo {ciclo_actual} — {datetime.now().strftime('%H:%M:%S')}")

            procesar_comandos()
            check_resumen_diario()
            check_resumen_semanal()
            resolver_pendientes()
            fijar_mensaje_vip()
            limpiar_mensajes_antiguos()

            r = _get_with_retry("https://data-api.polymarket.com/trades?limit=100")
            if r is None:
                print("❌ API Polymarket no responde")
                time.sleep(POLL_INTERVAL)
                continue

            try:
                r.raise_for_status()
                trades = r.json()
            except Exception as e:
                print(f"❌ Error parsing: {e}")
                time.sleep(POLL_INTERVAL)
                continue

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
                    apodo = get_apodo(wallet)
                    if len(whale_streaks) >= 500:
                        del whale_streaks[next(iter(whale_streaks))]
                    whale_streaks[wallet] = whale_streaks.get(wallet, 0) + 1
                    racha = whale_streaks[wallet]
                    score = calcular_score(usd, roi, racha, caliente)

                    print(f"   🐋 VIP: {apodo} | ROI {roi:.1f}% | ${usd} | Score {score}")
                    noticia       = buscar_noticia(trade.get("title", ""))
                    analisis      = analizar_con_claude(payload, noticia)
                    precio_actual = get_precio_actual(
                        trade.get("conditionId", ""),
                        int(trade.get("outcomeIndex", -1))
                    )

                    if enviar_telegram(TELEGRAM_CHAT_ID_VIP, mensaje_vip(payload, apodo, noticia, analisis, racha, score, caliente, precio_actual)):
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
                            guardar_señal(payload, apodo, score, trade)  
                            guardar_estado()

                time.sleep(0.5)

            guardar_estado()
            print(f"📊 Trades: {len(trades)} | Básico: {señales_basico} | VIP: {señales_vip} | Cache: {len(whale_cache)}")
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n🛑 Detenido. Guardando estado...")
            guardar_estado()
            break
        except Exception as e:
            print(f"❌ Error inesperado: {e}")
            time.sleep(10)

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🚀 Polymarket Smart Money Tracker v3.6 - Bot + API Combined")
    print(f"   Básico : >${MIN_USD_BASICO} USD | ROI >{MIN_ROI_BASICO}%")
    print(f"   VIP    : >${MIN_USD_VIP} USD | ROI >{MIN_ROI_VIP}%")
    print(f"   Precio : {int(PRECIO_MIN*100)}%–{int(PRECIO_MAX*100)}%")
    print(f"   Cebo   : 1/{CEBO_PROBABILIDAD} señales VIP al básico")
    print("─" * 50)

    # Bot en thread daemon
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()

    # Flask en main thread
    port = 5000
    print(f"🌐 API Flask en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)