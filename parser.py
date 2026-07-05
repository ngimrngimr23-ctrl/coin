import requests
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# --- ТВОИ НАСТРОЙКИ ---
LUNARCRUSH_API_KEY = "kd11rj0np0lclid39wsi29ogwqmr7se7n6py44jhe"
TELEGRAM_BOT_TOKEN = "8237191776:AAEQNWzRpXhX2ckfOreGN53BUhWd7lugkKU" 
TELEGRAM_CHAT_ID = "368097348"

# --- РЕЖИМ ДИАГНОСТИКИ API ---
def get_crypto_data():
    llama_url = "https://api.llama.fi/protocols"
    try:
        protocols = requests.get(llama_url).json()
    except Exception as e:
        return f"Ошибка при запросе к DefiLlama: {e}"
    
    analyzed_array = []
    
    for p in protocols:
        tvl = p.get("tvl", 0)
        token = p.get("symbol")
        
        if token and token != "-" and 10_000_000 <= tvl <= 500_000_000:
            # Стучимся в самый базовый эндпоинт по конкретной монете
            lunar_url = f"https://lunarcrush.com/api4/public/coins/{token.lower()}/v1"
            headers = {"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"}
            
            try:
                lunar_resp = requests.get(lunar_url, headers=headers)
                # ВАЖНО: Записываем статус ответа (например, 401 или 403) и сам текст ошибки
                raw_response_text = lunar_resp.text.replace('"', "'") # Убираем кавычки для красивого JSON
                social_volume = f"Код: {lunar_resp.status_code} | Ответ: {raw_response_text[:100]}..."
            except Exception as e:
                social_volume = f"Сбой скрипта: {str(e)}"

            analyzed_array.append({
                "ticker": f"${token.upper()}",
                "tvl_change_7d": round(p.get("change_7d", 0), 2) if p.get("change_7d") else 0,
                "social_mentions": social_volume
            })
            time.sleep(1) 
            
            # Собираем всего 3 монеты для быстрого теста!
            if len(analyzed_array) >= 3:
                break

    return json.dumps(analyzed_array, indent=2, ensure_ascii=False)

# --- ЛОГИКА ТЕЛЕГРАМ-БОТА ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)

def start_bot():
    offset = 0
    print("Бот (ДИАГНОСТИКА) запущен...")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url).json()
            
            if "result" in response:
                for update in response["result"]:
                    offset = update["update_id"] + 1
                    
                    if "message" in update and "text" in update["message"]:
                        chat_id = update["message"]["chat"]["id"]
                        text = update["message"]["text"]
                        
                        if str(chat_id) != TELEGRAM_CHAT_ID:
                            continue
                        
                        if text == "/push":
                            send_telegram_message(chat_id, "🔍 Отправляю тестовый запрос в LunarCrush...")
                            market_data = get_crypto_data()
                            send_telegram_message(chat_id, f"🛠 <b>Отчет об ошибке API:</b>\n<pre>{market_data}</pre>")
                            
        except Exception as e:
            time.sleep(5)

# --- СЕРВЕР ДЛЯ RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_server():
    server = HTTPServer(('0.0.0.0', 10000), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    start_bot()
    
