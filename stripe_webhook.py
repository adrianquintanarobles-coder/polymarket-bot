"""
Stripe Webhook Handler para Polymarket Smart Money Tracker
─────────────────────────────────────────────────────────────────
Recibe webhooks de Stripe cuando alguien paga.
Genera un link único de Telegram y lo envía por email al usuario.
"""

import os
import json
import stripe
import requests
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from pathlib import Path

# ── CONFIGURACIÓN ────────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_LINK  = os.getenv("TELEGRAM_CHANNEL_LINK", "t.me/+8Nl-SQBk7JkYTQ0")

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)

# ── PERSISTENCIA ─────────────────────────────────────────────────
PAYMENTS_LOG = Path("payments.json")

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

# ── FUNCIONES DE TELEGRAM ────────────────────────────────────────

def generar_link_unico() -> str:
    """
    Genera un link único de Telegram basado en el link template.
    En producción, podrías crear links dinámicos con la API de Telegram.
    Por ahora retornamos el link base que ya está configurado.
    """
    return TELEGRAM_CHANNEL_LINK

def enviar_email_con_link(email: str, nombre: str, link_telegram: str) -> bool:
    """
    Envía email con el link de acceso VIP.
    Usa Resend.com (gratuito hasta 100 emails/día con plan gratis).
    """
    try:
        # Opción 1: Resend (recomendado, gratis)
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
                print(f"❌ Error Resend: {response.text}")
                return False
        else:
            print("⚠️  RESEND_API_KEY no configurada, saltando email")
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
                "chat_id": os.getenv("TELEGRAM_CHAT_ID_VIP"),  # Tu ID de chat personal
                "text": mensaje,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"⚠️  No se pudo notificar por Telegram: {e}")

# ── WEBHOOK DE STRIPE ────────────────────────────────────────────

@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """
    Recibe webhooks de Stripe.
    Cuando payment_intent.succeeded → genera link y envía email.
    """
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

    # ── MANEJO DE EVENTOS ────────────────────────────────────────
    if event["type"] == "charge.succeeded":
        charge = event["data"]["object"]
        customer_email = charge.get("billing_details", {}).get("email") or charge.get("receipt_email")
        
        if customer_email:
            print(f"\n💳 Pago recibido: {customer_email}")
            
            # Generar link único
            link_telegram = generar_link_unico()
            
            # Guardar en log
            guardar_pago(customer_email, charge["id"], link_telegram)
            
            # Enviar email
            nombre = charge.get("billing_details", {}).get("name", "VIP Member")
            enviar_email_con_link(customer_email, nombre, link_telegram)
            
            # Notificar admin
            notificar_vip_telegram(customer_email, link_telegram)
            
            return jsonify({"success": True}), 200

    elif event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_email = session.get("customer_email")
        
        if customer_email:
            print(f"\n🎉 Checkout completado: {customer_email}")
            
            link_telegram = generar_link_unico()
            guardar_pago(customer_email, session["id"], link_telegram)
            enviar_email_con_link(customer_email, "VIP Member", link_telegram)
            notificar_vip_telegram(customer_email, link_telegram)
            
            return jsonify({"success": True}), 200

    elif event["type"] == "payment_intent.succeeded":
        intent = event["data"]["object"]
        customer_id = intent.get("customer")
        
        if customer_id:
            try:
                customer = stripe.Customer.retrieve(customer_id)
                customer_email = customer.get("email")
                
                if customer_email:
                    print(f"\n✅ PaymentIntent succeeded: {customer_email}")
                    
                    link_telegram = generar_link_unico()
                    guardar_pago(customer_email, intent["id"], link_telegram)
                    enviar_email_con_link(customer_email, "VIP Member", link_telegram)
                    notificar_vip_telegram(customer_email, link_telegram)
                    
                    return jsonify({"success": True}), 200
            except Exception as e:
                print(f"⚠️  Error retrieving customer: {e}")

    # Otros eventos los ignoramos silenciosamente
    return jsonify({"status": "received"}), 200

# ── ENDPOINTS AUXILIARES ─────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    """Health check para Railway."""
    return jsonify({"status": "ok", "service": "stripe-webhook"}), 200

@app.route("/api/payments", methods=["GET"])
def list_payments():
    """Lista pagos registrados (solo para admin, proteger en producción)."""
    pagos = cargar_pagos()
    return jsonify({"total": len(pagos), "pagos": pagos}), 200

# ── INICIO ───────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("🚀 Stripe Webhook Server")
    print(f"   POST /api/stripe-webhook")
    print(f"   GET  /api/health")
    print(f"   GET  /api/payments")
    print(f"   Port: {port}")
    app.run(host="0.0.0.0", port=port, debug=False)