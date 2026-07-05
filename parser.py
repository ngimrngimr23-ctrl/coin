import requests
import json
import time

# --- ТВОИ НАСТРОЙКИ ---
LUNARCRUSH_API_KEY = "kd11rj0np0lclid39wsi29ogwqmr7se7n6py44jhe"
TELEGRAM_BOT_TOKEN = "8237191776:AAEQNWzRpXhX2ckfOreGN53BUhWd7lugkKU" 
TELEGRAM_CHAT_ID = "368097348"

def send_to_telegram(message_text):
    """Отправляет готовый массив тебе в личку в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message_text, "parse_mode": "HTML"}
    requests.post(url, json=payload)

def get_data():
    # 1. Забираем деньги (DefiLlama)
    llama_url = "https://api.llama.fi/protocols"
    protocols = requests.get(llama_url).json()
    
    analyzed_array = []
    
    # Берем топ-30 проектов для скорости
    for p in protocols[:30]:
        tvl = p.get("tvl", 0)
        token = p.get("tokenSymbol")
        
        if token and 10_000_000 <= tvl <= 500_000_000:
            # 2. Забираем социальный хайп (LunarCrush)
            lunar_url = f"https://lunarcrush.com/api/4/public/coins/{token}"
            headers = {"Authorization": f"Bearer {LUNARCRUSH_API_KEY}"}
            
            try:
                lunar_resp = requests.get(lunar_url, headers=headers).json()
                social_volume = lunar_resp.get("data", {}).get("social_volume", 0)
            except:
                social_volume = "Нет данных"

            analyzed_array.append({
                "ticker": f"${token.upper()}",
                "tvl_change_7d": round(p.get("change_7d", 0), 2) if p.get("change_7d") else 0,
                "social_mentions": social_volume
            })
            time.sleep(1) # Пауза, чтобы LunarCrush не забанил за спам запросами

    # 3. Формируем красивый текст и кидаем в Telegram
    final_json = json.dumps(analyzed_array, indent=2, ensure_ascii=False)
    
    # Telegram режет длинные сообщения, поэтому обрезаем, если массив огромный
    send_to_telegram(f"🔥 <b>Свежий срез рынка:</b>\n<pre>{final_json[:3900]}</pre>")
    print("Данные успешно отправлены в Telegram!")

if __name__ == "__main__":
    get_data()
  
