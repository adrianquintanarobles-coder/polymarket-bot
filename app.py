"""
Polymarket Smart Money Tracker - Combined Flask App
Combina server.py + stripe_webhook.py en una sola app
"""

import os
import json
import stripe
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
from pathlib import Path

# ── CONFIGURACIÓN ────────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_LINK  = os.getenv("TELEGRAM_CHANNEL_LINK", "t.me/+8Nl-SQBk7JkYTQ0")

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)
CORS(app)

# ── PATHS ────────────────────────────────────────────────────────
SIGNALS_LOG_PATH = Path("signals_log.json")
PAYMENTS_LOG = Path("payments.json")

# ════════════════════════════════════════════════════════════════
#  SIGNALS API ENDPOINTS
# ════════════════════════════════════════════════════════════════

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

# ════════════════════════════════════════════════════════════════
#  STRIPE WEBHOOK & CHECKOUT
# ════════════════════════════════════════════════════════════════

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

# ════════════════════════════════════════════════════════════════
#  HEALTH & INFO
# ════════════════════════════════════════════════════════════════

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

# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("🚀 Polymarket Smart Money Tracker - Combined App")
    print(f"   Port: {port}")
    print(f"   API endpoints ready")
    app.run(host="0.0.0.0", port=port, debug=False)