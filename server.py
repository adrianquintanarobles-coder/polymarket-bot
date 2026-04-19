"""
Flask API Server para Polymarket Smart Money Tracker v3.6
Expone endpoints para que el frontend obtenga señales en tiempo real
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import json
from pathlib import Path
from datetime import datetime, timezone
import os

app = Flask(__name__)
CORS(app)

SIGNALS_LOG_PATH = Path("signals_log.json")

# ════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.route("/api/signals", methods=["GET"])
def get_signals():
    """Devuelve todas las señales registradas (últimas 50)"""
    try:
        if not SIGNALS_LOG_PATH.exists():
            return jsonify({"signals": [], "total": 0})
        
        with open(SIGNALS_LOG_PATH) as f:
            signals = json.load(f)
        
        # Últimas 50 señales, ordenadas de más reciente a más antigua
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
        
        # Filtrar solo VIP (>$500)
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
            "/api/health": "Health check"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)