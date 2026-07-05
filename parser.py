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
    
    # 1. ОДНИМ ЗАПРОСОМ СКАЧИВАЕМ ВСЕ МОНЕТЫ ИЗ LUNARCRUSH v4
    lunar_map = {}
    try:
        list_url = "https://lunarcrush.com/api4/public/coins/list/v2?limit=4000"
        headers = {"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"}
        list_resp = requests.get(list_url, headers=headers)
        if list_resp.status_code == 200:
            list_json = list_resp.json()
            # В коин-листе v2 данные лежат внутри ключа "data"
            coins_list = list_json.get("data", [])
            for item in coins_list:
                sym = item.get("symbol")
                if sym:
                    lunar_map[sym.upper()] = item
    except Exception as e:
        print(f"Предупреждение: не удалось предзагрузить список LunarCrush: {e}")
    
    analyzed_array = []
    
    # 2. ФИЛЬТРУЕМ И СОПОСТАВЛЯЕМ
    for p in protocols:
        tvl = p.get("tvl", 0)
        token = p.get("symbol")
        
        if token and token != "-" and 10_000_000 <= tvl <= 500_000_000:
            token_upper = token.upper()
            social_volume = "Нет данных"
            
            # Шаг А: Ищем токен в глобальном предзагруженном списке
            if token_upper in lunar_map:
                coin_item = lunar_map[token_upper]
                # Извлекаем объем социалки (social_volume_24h)
                social_volume = coin_item.get("social_volume_24h", coin_item.get("num_posts", "Нет данных"))
                if social_volume == "Нет данных":
                    social_volume = coin_item.get("interactions_24h", "Нет данных")
            
            # Шаг Б: ФОЛЛБЭК (если монета мелкая и не попала в топ-4000 списка)
            if social_volume == "Нет данных":
                headers = {"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"}
                # Пробуем найти точечно по тикеру или полному имени
                for search_term in [token.lower(), p.get("name", "").lower()]:
                    if not search_term or search_term == "-":
                        continue
                    lunar_url = f"https://lunarcrush.com/api4/public/topic/{search_term}/v1"
                    try:
                        lunar_resp = requests.get(lunar_url, headers=headers)
                        if lunar_resp.status_code == 200:
                            resp_json = lunar_resp.json()
                            
                            # Проверяем плоскую структуру топика v4
                            if "num_posts" in resp_json:
                                social_volume = resp_json["num_posts"]
                                break
                            elif "social_volume_24h" in resp_json:
                                social_volume = resp_json["social_volume_24h"]
                                break
                            elif "data" in resp_json:
                                d = resp_json["data"]
                                if isinstance(d, list) and len(d) > 0:
                                    social_volume = d[0].get("num_posts", d[0].get("social_volume_24h", "Нет данных"))
                                elif isinstance(d, dict):
                                    social_volume = d.get("num_posts", d.get("social_volume_24h", "Нет данных"))
                                if social_volume != "Нет данных":
                                    break
                    except Exception:
                        pass
                time.sleep(0.3) # Легкая пауза, чтобы не спамить

            analyzed_array.append({
                "ticker": f"${token_upper}",
                "tvl_change_7d": round(p.get("change_7d", 0), 2) if p.get("change_7d") else 0,
                "social_mentions": social_volume
            })
            
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
                            send_telegram_message(chat_id, "⏳ Скрипт запущен. Анализирую блокчейн и соцсети...")
                            market_data = get_crypto_data()
                            send_telegram_message(chat_id, f"🔥 <b>Ваш свежий срез рынка:</b>\n<pre>{market_data[:3900]}</pre>")
                        elif text == "/start":
                            send_telegram_message(chat_id, "Привет! Отправь команду <b>/push</b>, чтобы получить актуальный массив токенов.")
                            
        except Exception as e:
            print(f"Ошибка в цикле бота: {e}")
            time.sleep(5)

# --- ДЕФОЛТНЫЙ ХЕНДЛЕР ДЛЯ RENDER ---
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
    
