"""
2rate — Платёжный сервер (NOWPayments + AnyPay)
================================================
Запуск:  python server.py
Порт:    8555

Эндпоинты:
  POST /create-payment        — крипта (NOWPayments) — БЕЗ ИЗМЕНЕНИЙ
  POST /create-payment-card   — карты (AnyPay) — НОВЫЙ
  POST /ipn                   — IPN от NOWPayments
  POST /anypay-notify         — Notify URL для AnyPay
  GET  /payment-info          — данные заказа для страницы success (по pay_id)
  GET  /status                — health-check

Установка:
    pip install flask flask-cors requests python-dotenv
"""

import os
import json
import hmac
import hashlib
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# =============================================
# НАСТРОЙКИ (читаются из переменных окружения)
# =============================================
# ⚠️ НИКОГДА не коммитьте эти значения в git. Используйте .env файл.

# --- NOWPayments (крипта) ---
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "REPLACE_WITH_NEW_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "REPLACE_WITH_NEW_SECRET")

# --- AnyPay (карты) ---
ANYPAY_PROJECT_ID = os.getenv("ANYPAY_PROJECT_ID", "17633")  # ваш merchant_id
ANYPAY_SECRET_KEY = os.getenv("ANYPAY_SECRET_KEY", "REPLACE_WITH_NEW_SECRET")
ANYPAY_API_ID     = os.getenv("ANYPAY_API_ID",     "REPLACE_WITH_NEW_API_ID")
ANYPAY_API_KEY    = os.getenv("ANYPAY_API_KEY",    "REPLACE_WITH_NEW_API_KEY")
ANYPAY_CURRENCY   = "KZT"

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "REPLACE_WITH_NEW_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "8058299958")

# --- Публичные URL ---
SERVER_URL  = os.getenv("SERVER_URL",  "https://suzette-immiscible-matthew.ngrok-free.dev")
SUCCESS_URL = "https://2rate.kz/order-success.html"
FAIL_URL    = "https://2rate.kz/order.html"

# Хранилище заказов в памяти.
# ⚠️ Для продакшна замените на SQLite/Postgres — иначе при рестарте сервера
#    все заказы теряются и пользователь не сможет открыть success-страницу.
orders = {}


# =============================================
# TELEGRAM
# =============================================
def send_telegram(message: str) -> bool:
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
# NOWPayments — СОЗДАНИЕ ПЛАТЕЖА (крипта)
# =============================================
@app.route("/create-payment", methods=["POST"])
def create_payment():
    """Крипто-оплата через NOWPayments — без изменений."""
    try:
        data = request.json or {}
        amount_kzt = data.get("amount", 0)
        order_info = data.get("order_info", "")
        order_id   = data.get("order_id", "")

        if amount_kzt <= 0:
            return jsonify({"error": "Неверная сумма"}), 400

        resp = requests.post(
            "https://api.nowpayments.io/v1/invoice",
            headers={
                "x-api-key": NOWPAYMENTS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "price_amount":     amount_kzt,
                "price_currency":   "kzt",
                "order_id":         order_id,
                "order_description": f"2rate - заказ отзывов ({order_id})",
                "ipn_callback_url": f"{SERVER_URL}/ipn",
                "success_url":      data.get("success_url", f"{SUCCESS_URL}?id={order_id}"),
                "cancel_url":       data.get("cancel_url",  FAIL_URL),
            },
            timeout=15
        )
        result = resp.json()

        if resp.status_code in (200, 201) and result.get("invoice_url"):
            orders[order_id] = {
                "info":     order_info,
                "amount":   amount_kzt,
                "currency": "KZT",
                "method":   "crypto",
                "status":   "waiting",
                "created":  datetime.now().isoformat(),
            }
            print(f"[NOWPAY] Создан крипто-платёж {order_id} на {amount_kzt} KZT")
            return jsonify({
                "success":     True,
                "invoice_url": result["invoice_url"],
                "payment_id":  result.get("id"),
            })

        print(f"[NOWPAY ERROR] {result}")
        return jsonify({"error": "Ошибка NOWPayments", "details": result}), 500

    except Exception as e:
        print(f"[ERROR] create_payment: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================
# AnyPay — СОЗДАНИЕ ПЛАТЕЖА (карты)
# =============================================
@app.route("/create-payment-card", methods=["POST"])
def create_payment_card():
    """
    Создаёт платёж в AnyPay через SCI-форму.
    Возвращает готовую URL для редиректа пользователя на страницу оплаты.

    Формула подписи (по документации anypay.io/doc/sci, SHA256):
        sha256( merchant_id : pay_id : amount : currency : desc : success_url : fail_url : secret_key )
    Параметры склеиваются через двоеточие именно в этом порядке.
    """
    try:
        data = request.json or {}
        amount_kzt = data.get("amount", 0)
        order_info = data.get("order_info", "")
        order_id   = str(data.get("order_id", "")).strip()
        email      = data.get("email", "")
        phone      = data.get("phone", "")

        if amount_kzt <= 0:
            return jsonify({"error": "Неверная сумма"}), 400
        if not order_id:
            return jsonify({"error": "Нет order_id"}), 400

        # AnyPay требует amount как строку с двумя знаками после запятой
        amount_str = f"{float(amount_kzt):.2f}"

        # Описание заказа (видит пользователь на странице оплаты AnyPay)
        desc = f"Заказ отзывов 2rate №{order_id}"

        # URL'ы возврата
        success_url = f"{SUCCESS_URL}?id={order_id}"
        fail_url    = FAIL_URL

        # Подпись SHA256
        sign_str = ":".join([
            ANYPAY_PROJECT_ID,
            order_id,
            amount_str,
            ANYPAY_CURRENCY,
            desc,
            success_url,
            fail_url,
            ANYPAY_SECRET_KEY,
        ])
        sign = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

        # Параметры для редиректа на страницу оплаты AnyPay
        params = {
            "merchant_id": ANYPAY_PROJECT_ID,
            "pay_id":      order_id,
            "amount":      amount_str,
            "currency":    ANYPAY_CURRENCY,
            "desc":        desc,
            "success_url": success_url,
            "fail_url":    fail_url,
            "sign":        sign,
        }
        if email:
            params["email"] = email
        if phone:
            params["phone"] = phone

        payment_url = "https://anypay.io/merchant?" + urlencode(params)

        # Сохраняем заказ
        orders[order_id] = {
            "info":     order_info,
            "amount":   amount_kzt,
            "currency": "KZT",
            "method":   "card",
            "status":   "waiting",
            "email":    email,
            "phone":    phone,
            "created":  datetime.now().isoformat(),
        }

        print(f"[ANYPAY] Создан карт-платёж {order_id} на {amount_str} KZT")
        return jsonify({"success": True, "payment_url": payment_url})

    except Exception as e:
        print(f"[ERROR] create_payment_card: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================
# AnyPay — NOTIFY URL
# =============================================
@app.route("/anypay-notify", methods=["GET", "POST"])
def anypay_notify():
    """
    AnyPay присылает сюда уведомление об оплате (POST или GET).
    Передаются параметры: merchant_id, amount, pay_id, transaction_id,
    profit, currency, method, sign и др.

    Подпись для проверки (из доки AnyPay):
        sha256( merchant_id : amount : secret_key : pay_id )

    Сервер должен ответить строго "OK" (латиницей) — иначе AnyPay
    будет повторять уведомление.
    """
    try:
        # AnyPay по умолчанию шлёт POST, но можно настроить и GET — поддерживаем оба
        params = request.form.to_dict() if request.method == "POST" else request.args.to_dict()

        merchant_id    = params.get("merchant_id", "")
        amount         = params.get("amount", "")
        pay_id         = params.get("pay_id", "")
        transaction_id = params.get("transaction_id", "")
        currency       = params.get("currency", "")
        method         = params.get("method", "")
        profit         = params.get("profit", "")
        received_sign  = params.get("sign", "")

        # Проверяем merchant_id
        if merchant_id != ANYPAY_PROJECT_ID:
            print(f"[ANYPAY NOTIFY] ❌ Чужой merchant_id: {merchant_id}")
            return "Bad merchant", 400

        # Считаем ожидаемую подпись
        sign_str = f"{merchant_id}:{amount}:{ANYPAY_SECRET_KEY}:{pay_id}"
        expected_sign = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

        if not hmac.compare_digest(expected_sign, received_sign.lower()):
            print(f"[ANYPAY NOTIFY] ❌ Неверная подпись для заказа {pay_id}")
            print(f"  Получена:   {received_sign}")
            print(f"  Ожидается:  {expected_sign}")
            return "Bad sign", 400

        # Проверяем сумму (защита от подмены)
        order = orders.get(pay_id)
        if not order:
            print(f"[ANYPAY NOTIFY] ⚠️ Неизвестный pay_id: {pay_id}")
            # Всё равно отвечаем OK, чтобы AnyPay не долбил, но в работу не берём
            return "OK", 200

        try:
            if abs(float(amount) - float(order["amount"])) > 0.01:
                print(f"[ANYPAY NOTIFY] ❌ Сумма не совпадает: пришло {amount}, ожидалось {order['amount']}")
                return "Bad amount", 400
        except ValueError:
            return "Bad amount format", 400

        # Защита от двойной обработки
        if order.get("status") == "paid":
            print(f"[ANYPAY NOTIFY] ℹ️ Заказ {pay_id} уже оплачен ранее")
            return "OK", 200

        # Помечаем заказ оплаченным
        order["status"]         = "paid"
        order["paid_at"]        = datetime.now().isoformat()
        order["transaction_id"] = transaction_id

        # Уведомление в Telegram
        message = (
            f"✅ <b>ОПЛАТА ПОЛУЧЕНА (карта)</b>\n"
            f"\n"
            f"💳 Транзакция AnyPay: #{transaction_id}\n"
            f"💵 Сумма: {amount} {currency}\n"
            f"💰 К зачислению: {profit} {currency}\n"
            f"🏦 Метод: {method}\n"
            f"\n"
            f"{order.get('info', '')}"
        )
        send_telegram(message)

        print(f"[ANYPAY NOTIFY] ✅ Заказ {pay_id} оплачен")
        return "OK", 200

    except Exception as e:
        print(f"[ANYPAY NOTIFY ERROR] {e}")
        # Не отвечаем OK — AnyPay повторит, и мы успеем починить
        return "Error", 500


# =============================================
# NOWPayments IPN (без изменений, только мелкая правка статусов)
# =============================================
@app.route("/ipn", methods=["POST"])
def ipn_handler():
    try:
        payload = request.json
        if not payload:
            return "OK", 200

        received_sig = request.headers.get("x-nowpayments-sig", "")
        sig_valid = verify_nowpayments_signature(payload, received_sig)
        if not sig_valid:
            print(f"[IPN] ⚠️ Подпись NOWPayments не совпала")

        payment_status = payload.get("payment_status", "")
        order_id       = payload.get("order_id", "unknown")
        pay_amount     = payload.get("pay_amount", 0)
        pay_currency   = payload.get("pay_currency", "")
        price_amount   = payload.get("price_amount", 0)
        price_currency = payload.get("price_currency", "")
        payment_id     = payload.get("payment_id", "")

        print(f"[IPN] {order_id}: {payment_status} | {pay_amount} {pay_currency}")

        if payment_status in ("finished", "confirmed"):
            order = orders.get(order_id, {})
            if order.get("status") == "paid":
                return "OK", 200

            order_info = order.get("info", "Данные заказа не найдены")
            send_telegram(
                f"✅ <b>ОПЛАТА ПОЛУЧЕНА (крипто)</b>\n\n"
                f"💳 Платёж: #{payment_id}\n"
                f"💰 Оплачено: {pay_amount} {pay_currency.upper()}\n"
                f"💵 Сумма заказа: {price_amount} {price_currency.upper()}\n\n"
                f"{order_info}"
            )
            if order_id in orders:
                orders[order_id]["status"]  = "paid"
                orders[order_id]["paid_at"] = datetime.now().isoformat()

        return "OK", 200

    except Exception as e:
        print(f"[IPN ERROR] {e}")
        return "Error", 500


def verify_nowpayments_signature(payload, received_sig):
    if not received_sig:
        return False

    def sort_dict(d):
        return {k: sort_dict(v) if isinstance(v, dict) else v for k, v in sorted(d.items())}

    sorted_payload = sort_dict(payload)
    payload_str = json.dumps(sorted_payload, separators=(",", ":"))
    expected = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, received_sig)


# =============================================
# ИНФО О ЗАКАЗЕ (для страницы success)
# =============================================
@app.route("/payment-info", methods=["GET"])
def payment_info():
    """
    Страница order-success.html запрашивает сюда статус заказа по pay_id из URL.
    Возвращаем минимум публичной информации — без email/phone/info.
    """
    pay_id = request.args.get("id", "")
    order = orders.get(pay_id)
    if not order:
        return jsonify({"found": False}), 404

    return jsonify({
        "found":    True,
        "pay_id":   pay_id,
        "amount":   order.get("amount"),
        "currency": order.get("currency"),
        "method":   order.get("method"),
        "status":   order.get("status"),
        "paid_at":  order.get("paid_at"),
    })


# =============================================
# HEALTH CHECK
# =============================================
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status":       "running",
        "orders_count": len(orders),
        "paid_count":   sum(1 for o in orders.values() if o.get("status") == "paid"),
        "time":         datetime.now().isoformat(),
    })


# =============================================
# ЗАПУСК
# =============================================
if __name__ == "__main__":
    print("=" * 55)
    print("  2rate Payment Server (NOWPayments + AnyPay)")
    print(f"  Порт: 8555")
    print(f"  NOWPayments IPN:  {SERVER_URL}/ipn")
    print(f"  AnyPay Notify:    {SERVER_URL}/anypay-notify")
    print("=" * 55)
    app.run(host="0.0.0.0", port=8555, debug=False)
