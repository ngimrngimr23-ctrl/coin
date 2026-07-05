import requests
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# --- ТВОИ НАСТРОЙКИ ---
LUNARCRUSH_API_KEY = "kd11rj0np0lclid39wsi29ogwqmr7se7n6py44jhe"
TELEGRAM_BOT_TOKEN = "8237191776:AAEQNWzRpXhX2ckfOreGN53BUhWd7lugkKU" 
TELEGRAM_CHAT_ID = "368097348"

# --- ФУНКЦИЯ СБОРА ДАННЫХ КРИПТЫ ---
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
            # 1. Пробуем новый эндпоинт v4 (через coins)
            lunar_url = f"https://lunarcrush.com/api4/public/coins/{token.lower()}/v1"
            headers = {"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"}
            
            try:
                lunar_resp = requests.get(lunar_url, headers=headers)
                
                # 2. Если по coins 404, пробуем альтернативный эндпоинт topics
                if lunar_resp.status_code != 200:
                    lunar_url = f"https://lunarcrush.com/api4/public/topic/{token.lower()}/v1"
                    lunar_resp = requests.get(lunar_url, headers=headers)

                resp_json = lunar_resp.json()
                
                # 3. Достаем данные (в v4 они лежат внутри ключа "data")
                data_block = resp_json.get("data", {})
                
                if isinstance(data_block, list) and len(data_block) > 0:
                    social_data = data_block[0]
                elif isinstance(data_block, dict):
                    social_data = data_block
                else:
                    social_data = {}

                # 4. В API v4 метрика называется social_volume_24h
                social_volume = social_data.get("social_volume_24h", "Нет данных")
                
                # На всякий случай проверяем старое название
                if social_volume == "Нет данных":
                    social_volume = social_data.get("social_volume", "Нет данных")
                    
            except Exception:
                social_volume = "Ошибка API"

            analyzed_array.append({
                "ticker": f"${token.upper()}",
                "tvl_change_7d": round(p.get("change_7d", 0), 2) if p.get("change_7d") else 0,
                "social_mentions": social_volume
            })
            time.sleep(1) # Пауза, чтобы не забанили за спам
            
            if len(analyzed_array) >= 15:
                break

    return json.dumps(analyzed_array, indent=2, ensure_ascii=False)

# --- ЛОГИКА ТЕЛЕГРАМ-БОТА ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)

def start_bot():
    offset = 0
    print("Бот успешно запущен и слушает команды...")
    
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
                            send_telegram_message(chat_id, "⛔️ Извините, этот бот приватный.")
                            continue
                        
                        if text == "/push":
                            send_telegram_message(chat_id, "⏳ Скрипт запущен. Анализирую блокчейн и соцсети (около 15-20 сек)...")
                            market_data = get_crypto_data()
                            send_telegram_message(chat_id, f"🔥 <b>Ваш свежий срез рынка:</b>\n<pre>{market_data[:3900]}</pre>")
                        elif text == "/start":
                            send_telegram_message(chat_id, "Привет! Отправь команду <b>/push</b>, чтобы получить актуальный массив токенов.")
                            
        except Exception as e:
            print(f"Ошибка в цикле бота: {e}")
            time.sleep(5)

# --- «КОСТЫЛЬ» ДЛЯ БЕСПЛАТНОГО RENDER ---
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
