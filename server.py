"""
2rate — NOWPayments IPN сервер (Render.com версия)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import hmac
import hashlib
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# =============================================
# НАСТРОЙКИ
# =============================================
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "MYVWG6Y-0XNM05S-PD9RR9J-3N4XTFK")
IPN_SECRET = os.environ.get("IPN_SECRET", "QquSRQnsCpwC6WKe4eatSt1HJmKTwC9v")
TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8064886932:AAFsaZj_iXhWbsclgXNog22SQPIng5i9Fyc")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID", "8058299958")
SERVER_URL = os.environ.get("SERVER_URL", "https://tworate-server.onrender.com")

orders = {}

# =============================================
# TELEGRAM
# =============================================
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"[TG ERROR] {e}")
        return False


# =============================================
# СОЗДАНИЕ ПЛАТЕЖА
# =============================================
@app.route("/create-payment", methods=["POST"])
def create_payment():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "Нет данных"}), 400

        amount_kzt = data.get("amount", 0)
        order_info = data.get("order_info", "")
        order_id = data.get("order_id", "")

        if amount_kzt <= 0:
            return jsonify({"error": "Неверная сумма"}), 400

        resp = requests.post(
            "https://api.nowpayments.io/v1/invoice",
            headers={
                "x-api-key": NOWPAYMENTS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "price_amount": amount_kzt,
                "price_currency": "kzt",
                "order_id": order_id,
                "order_description": f"2rate - заказ отзывов ({order_id})",
                "ipn_callback_url": f"{SERVER_URL}/ipn",
                "success_url": data.get("success_url", "https://2rate.kz/order-success.html"),
                "cancel_url": data.get("cancel_url", "https://2rate.kz/order.html"),
            },
            timeout=15
        )

        result = resp.json()

        if resp.status_code in (200, 201) and result.get("invoice_url"):
            orders[order_id] = {
                "info": order_info,
                "amount": amount_kzt,
                "status": "waiting",
                "created": datetime.now().isoformat()
            }
            print(f"[PAYMENT] Создан платёж {order_id} на {amount_kzt} KZT")
            return jsonify({
                "success": True,
                "invoice_url": result["invoice_url"],
                "payment_id": result.get("id")
            })
        else:
            print(f"[ERROR] NOWPayments: {result}")
            return jsonify({"error": "Ошибка NOWPayments", "details": result}), 500

    except Exception as e:
        print(f"[ERROR] create_payment: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================
# IPN (УВЕДОМЛЕНИЕ ОБ ОПЛАТЕ)
# =============================================
@app.route("/ipn", methods=["POST"])
def ipn_handler():
    try:
        payload = request.json
        if not payload:
            return "OK", 200

        received_sig = request.headers.get("x-nowpayments-sig", "")
        sig_valid = verify_signature(payload, received_sig)

        if not sig_valid:
            print(f"[IPN] Подпись не совпала (обрабатываем)")

        payment_status = payload.get("payment_status", "")
        order_id = payload.get("order_id", "unknown")
        pay_amount = payload.get("pay_amount", 0)
        pay_currency = payload.get("pay_currency", "")
        price_amount = payload.get("price_amount", 0)
        price_currency = payload.get("price_currency", "")
        payment_id = payload.get("payment_id", "")

        print(f"[IPN] Заказ {order_id}: статус={payment_status}")

        if payment_status in ("finished", "confirmed"):
            order_data = orders.get(order_id, {})
            order_info = order_data.get("info", "Данные заказа не найдены")

            message = (
                f"✅ <b>ОПЛАТА ПОЛУЧЕНА (крипто)</b>\n"
                f"\n"
                f"💳 Платёж: #{payment_id}\n"
                f"💰 Оплачено: {pay_amount} {pay_currency.upper()}\n"
                f"💵 Сумма заказа: {price_amount} {price_currency.upper()}\n"
                f"\n"
                f"{order_info}"
            )
            send_telegram(message)

            if order_id in orders:
                orders[order_id]["status"] = "paid"

            print(f"[IPN] ✅ Заказ {order_id} оплачен!")

        return "OK", 200

    except Exception as e:
        print(f"[IPN ERROR] {e}")
        return "Error", 500


def verify_signature(payload, received_sig):
    if not received_sig:
        return False

    def sort_dict(d):
        result = {}
        for key in sorted(d.keys()):
            if isinstance(d[key], dict):
                result[key] = sort_dict(d[key])
            else:
                result[key] = d[key]
        return result

    sorted_payload = sort_dict(payload)
    payload_str = json.dumps(sorted_payload, separators=(',', ':'))

    expected_sig = hmac.new(
        IPN_SECRET.encode('utf-8'),
        payload_str.encode('utf-8'),
        hashlib.sha512
    ).hexdigest()

    return hmac.compare_digest(expected_sig, received_sig)


# =============================================
# СТАТУС
# =============================================
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "running",
        "orders_count": len(orders),
        "time": datetime.now().isoformat()
    })

@app.route("/", methods=["GET"])
def home():
    return jsonify({"service": "2rate payment server", "status": "running"})


# =============================================
# ЗАПУСК
# =============================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8555))
    print(f"  2rate Server | Port: {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
