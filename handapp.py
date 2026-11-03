from flask import Flask, request, jsonify
import requests, re, json, time, configparser, sys, urllib.parse
from bs4 import BeautifulSoup
from typing import Optional, Dict

app = Flask(__name__)

# --- GLOBALS ---
BITRIX_WEBHOOK_URL = ""
SERVER_PORT = 5000
ALLOWED_USERS = []

# Правильный маппинг полей из crm.deal.fields.json
FIELD_MAP = {
    "TITLE": "TITLE",                          # Название сделки
    "PRICE": "UF_CRM_1621497190023",          # Стоимость объекта +//
    "STREET": "UF_CRM_1622791092",            # Адрес (улица) + //
    "HOUSE_NUMBER": "UF_CRM_1622607023",   # Номер дома //
    "AREA": "UF_CRM_1624341576",              # Площадь объекта + //
    "FLOOR": "UF_CRM_1621591973519",          # Этаж +
    "FLOORS_TOTAL": "UF_CRM_1621591973519",   # Этажность +
    "HOUSE_TYPE": "UF_CRM_1623396322",     # Тип дома + //
    "YEAR": "UF_CRM_1641969918631",           # Год постройки + //
    "BATHROOM": "UF_CRM_1621928135362",          # Санузел + //
    "BALCONY": "UF_CRM_1621928221551",        # Балкон + //
    "STATE": "UF_CRM_1621591769132",         # Состояние +
    "ROOMS": "UF_CRM_1621849186398",          # Количество комнат
    "DESCRIPTION": "COMMENTS",                 # Описание
    "KRISHA_LINK": "UF_CRM_1618989147417"        # Ссылка на Krisha или стоимость
}

# Маппинг значений для типа дома (из JSON списка 67)
HOUSE_TYPE_MAP = {
    "кирпичный": "145",
    "панельный": "147",
    "монолитный": "169",
    "пеноблок": "199"
}

# Маппинг для санузла (из JSON списка 69)
BATHROOM_MAP = {
    "раздельный": "91",
    "совмещенный": "93",
    "два и более": "433",
    "во дворе": "177"
}

# Маппинг для балкона (из JSON списка 68)
BALCONY_MAP = {
    "балкон": "95",
    "лоджия": "133",
    "балкон + лоджия": "97",
    "нет балкона": "135"
}

# Maппинг для состояния (из JSON списка 70)
STATE_MAP = {
    "хорошее": "53",
    "среднее": "55",
    "требует ремонта": "57",
    "евро - ремонт": "59",
}

def load_config():
    global BITRIX_WEBHOOK_URL, SERVER_PORT, ALLOWED_USERS
    config = configparser.ConfigParser()
    try:
        config.read('config.ini', encoding='utf-8')
        BITRIX_WEBHOOK_URL = config.get('BITRIX24', 'WEBHOOK_URL')
        SERVER_PORT = config.getint('SERVER', 'PORT', fallback=5000)
        allowed = config.get('ACCESS', 'ALLOWED_USER_IDS', fallback='')
        ALLOWED_USERS = [x.strip() for x in allowed.split(',') if x.strip()]
        print(f"✅ Разрешённые пользователи: {ALLOWED_USERS}")
    except Exception as e:
        print("❌ Ошибка чтения config.ini:", e)
        sys.exit(1)

def extract_window_data(html: str) -> Optional[Dict]:
    """Ищем в HTML window.data = {...}"""
    m = re.search(r'window\.data\s*=\s*({.*?});\s*</script>', html, flags=re.S)
    if not m:
        m = re.search(r'window\.data\s*=\s*({.+});', html, flags=re.S)
    if not m:
        return None
    raw = m.group(1)
    try:
        cleaned = re.sub(r',(\s*[}\]])', r'\1', raw)
        return json.loads(cleaned)
    except Exception as e:
        print(f"⚠️ Ошибка парсинга window.data: {e}")
        return None

def parse_address(full_address: str) -> tuple:
    """
    Парсит полный адрес и разделяет на улицу и номер дома.
    Примеры:
      'улица Абая 16' -> ('улица Абая', '16')
      'Абая 16' -> ('Абая', '16')
      'Жандосова 16' -> ('Жандосова', '16')
      'улица Зейна Шашкина 16-14' -> ('улица Зейна Шашкина', '16-14')
    """
    if not full_address:
        return None, None
    
    # Убираем лишние символы
    address = full_address.replace(' - ', '-').replace('- ', '-').strip()
    
    # Ищем последнюю последовательность, которая выглядит как номер дома
    # Номер дома может быть: 16, 16А, 16/2, 16-14, 16а/3
    match = re.search(r'^(.+?)\s+([\d]+[а-яА-Яa-zA-Z]?(?:[/-][\d]+[а-яА-Яa-zA-Z]?)?)$', address)
    
    if match:
        street = match.group(1).strip()
        house_num = match.group(2).strip()
        return street, house_num
    
    # Если не нашли номер дома - вся строка это улица
    return address, None


def parse_krisha_page(krisha_url: str) -> Optional[Dict]:
    """Скачивает страницу и извлекает все нужные поля"""
    print(f"🔗 Парсинг URL: {krisha_url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        resp = requests.get(krisha_url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"❌ Ошибка скачивания: {e}")
        return None

    result = {}
    
    # Пробуем извлечь window.data
    wd = extract_window_data(html)
    
    if wd and 'advert' in wd:
        adv = wd['advert']
        print("✅ Найден window.data")
        
        # Цена
        if adv.get('price'):
            result['price'] = int(adv['price'])
            print(f"  💰 Цена: {result['price']}")
        
        # Количество комнат
        if adv.get('rooms'):
            result['rooms'] = int(adv['rooms'])
            print(f"  🚪 Комнат: {result['rooms']}")
        
        # Адрес
        full_address = adv.get('addressTitle') or adv.get('fullAddress') or adv.get('address')
        if full_address:
            street, house_num = parse_address(full_address)
            result['street'] = street
            result['house_number'] = house_num
            print(f"  📍 Улица: {street}, Дом: {house_num}")
        
        # Площадь
        if adv.get('square'):
            result['area'] = float(adv['square'])
            print(f"  📏 Площадь: {result['area']}")
        
        # Заголовок
        result['title'] = adv.get('title')
        
        # Описание
        result['description'] = adv.get('description', '')
    
    # Парсим HTML для остальных полей
    soup = BeautifulSoup(html, 'html.parser')
    
    # Ищем блоки offer__info-item
    for item in soup.find_all('div', class_='offer__info-item'):
        title_div = item.find('div', class_='offer__info-title')
        value_div = item.find('div', class_='offer__advert-short-info')
        
        if not title_div or not value_div:
            continue
            
        label = title_div.get_text(strip=True)
        value = value_div.get_text(strip=True)
        
        print(f"  🔍 Найдено поле: {label} = {value}")
        
        # Тип дома
        if 'Тип дома' in label:
            result['house_type'] = value.lower()
            print(f"    🏠 Тип дома: {value}")
        
        # Год постройки
        elif 'Год постройки' in label:
            year_match = re.search(r'(\d{4})', value)
            if year_match:
                result['year'] = int(year_match.group(1))
                print(f"    📅 Год: {result['year']}")
        
        # Этаж
        elif 'Этаж' in label:
            floor_match = re.search(r'(\d+)\s+из\s+(\d+)', value)
            if floor_match:
                result['floor'] = int(floor_match.group(1))
                result['floors_total'] = int(floor_match.group(2))
                print(f"    🏢 Этаж: {result['floor']}/{result['floors_total']}")
        
        # Площадь (если не было в window.data)
        elif 'Площадь' in label and not result.get('area'):
            area_match = re.search(r'([\d.]+)', value)
            if area_match:
                result['area'] = float(area_match.group(1))
                print(f"    📏 Площадь: {result['area']}")
        
        # Санузел
        elif 'Санузел' in label:
            result['bathroom'] = value.lower()
            print(f"    🚽 Санузел: {value}")
        
        # Балкон
        elif 'Балкон' in label:
            result['balcony'] = value.lower()
            print(f"    🪟 Балкон: {value}")
        
        # Состояние
        elif 'Состояние' in label:
            result['state'] = value.lower()
            print(f"    🛠️ Состояние: {value}")
    
    # Формируем заголовок
    if result.get('rooms') and result.get('street'):
        rooms_str = f"{result['rooms']}к"
        street = result['street']
        house = result.get('house_number', '')
        result['title_generated'] = f"пр {rooms_str} {street} {house}".strip()
    else:
        result['title_generated'] = result.get('title', '')
    
    result['link'] = krisha_url
    
    print(f"\n📊 Всего извлечено полей: {len([k for k,v in result.items() if v is not None])}")
    return result

def update_bitrix_deal(deal_id: int, krisha_url: str) -> bool:
    """Парсим страницу и обновляем сделку"""
    parsed = parse_krisha_page(krisha_url)
    if not parsed:
        print("❌ Не удалось распарсить страницу")
        return False

    fields = {}
    
    if parsed.get('title_generated'):
        fields[FIELD_MAP['TITLE']] = parsed['title_generated']
    if parsed.get('price'):
        fields[FIELD_MAP['PRICE']] = parsed['price']
    if parsed.get('street'):
        fields[FIELD_MAP['STREET']] = parsed['street']
    if parsed.get('house_number'):
        fields[FIELD_MAP['HOUSE_NUMBER']] = parsed['house_number']
    if parsed.get('area'):
        fields[FIELD_MAP['AREA']] = parsed['area']
        
    if parsed.get('floor') and parsed.get('floors_total'):
        fields[FIELD_MAP['FLOOR']] = f"{parsed['floor']}/{parsed['floors_total']}"
    elif parsed.get('floor'):
        fields[FIELD_MAP['FLOOR']] = str(parsed['floor'])

    if parsed.get('house_type'):
        for key, value_id in HOUSE_TYPE_MAP.items():
            if key in parsed['house_type']:
                fields[FIELD_MAP['HOUSE_TYPE']] = value_id
                break
    if parsed.get('year'):
        fields[FIELD_MAP['YEAR']] = parsed['year']
    if parsed.get('bathroom'):
        for key, value_id in BATHROOM_MAP.items():
            if key in parsed['bathroom']:
                fields[FIELD_MAP['BATHROOM']] = value_id
                break
    if parsed.get('balcony'):
        for key, value_id in BALCONY_MAP.items():
            if key in parsed['balcony']:
                fields[FIELD_MAP['BALCONY']] = value_id
                break
    if parsed.get('state'):
        for key, value_id in STATE_MAP.items():
            if key in parsed['state']:
                fields[FIELD_MAP['STATE']] = value_id
                break
    if parsed.get('rooms'):
        fields[FIELD_MAP['ROOMS']] = parsed['rooms']
    if parsed.get('description'):
        fields[FIELD_MAP['DESCRIPTION']] = parsed['description'][:1000]
    if parsed.get('link'):
        fields[FIELD_MAP['KRISHA_LINK']] = parsed['link']

    print(f"\n🔧 Отправка {len(fields)} полей в Bitrix...")

    url = urllib.parse.urljoin(BITRIX_WEBHOOK_URL, 'crm.deal.update')
    payload = {
        "id": deal_id,
        "fields": fields
    }
    
    try:
        r = requests.post(url, json=payload, timeout=15)
        
        # Подробный вывод ошибки от Битрикс
        if not r.ok:
            print(f"❌ Битрикс вернул ошибку {r.status_code}:")
            try:
                print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            except:
                print(r.text)
            return False
            
        resp = r.json()
        
        if resp.get('result'):
            print(f"✅ Сделка {deal_id} успешно обновлена!")
            return True
        else:
            print(f"❌ Ошибка внутри ответа Битрикс:")
            print(json.dumps(resp, ensure_ascii=False, indent=2))
            return False
    except Exception as e:
        print(f"❌ Ошибка соединения с Битрикс: {e}")
        return False

@app.route('/bitrix/krisha_update', methods=['POST', 'GET'])
def handle_bitrix_webhook():
    print("\n" + "="*70)
    print("📩 НОВЫЙ ЗАПРОС ОТ БИТРИКС24")
    print("="*70)
    
    # Собираем данные
    data = request.values.to_dict()
    if request.is_json:
        data.update(request.get_json(silent=True) or {})
    if not data:
        raw_body = request.data.decode('utf-8', errors='ignore').strip()
        if "=" in raw_body:
            raw_body = raw_body.replace('&', '&')
            raw_body = re.sub(r'\s*\[.*\]$', '', raw_body).strip()
            data.update(dict(urllib.parse.parse_qsl(raw_body)))

    print(f"📦 ВСЕ ПОЛУЧЕННЫЕ ДАННЫЕ: {data}")
    
    # Проверка доступа
    user_id = str(data.get("user_id", "")).strip().replace('{', '').replace('}', '')
    
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        print(f"🚫 Доступ запрещён для пользователя '{user_id}'")
        return jsonify({"error": "forbidden"}), 403
    else:
        print(f"✅ Пользователь '{user_id}' имеет доступ")
    
    deal_id_str = data.get('deal_id') or data.get('deal')
    krisha_link = data.get('link') or data.get('url')
    
    if not deal_id_str or not krisha_link:
        print("❌ Отсутствует deal_id или link")
        return jsonify({'status': 'error', 'message': 'Missing deal_id or link'}), 400
    
    try:
        deal_id = int(deal_id_str)
    except:
        print(f"❌ Неверный формат deal_id: {deal_id_str}")
        return jsonify({'status': 'error', 'message': 'Invalid deal_id'}), 400
    
    print(f"🎯 Deal ID: {deal_id}")
    print(f"🔗 Krisha URL: {krisha_link}")
    
    start_time = time.time()
    success = update_bitrix_deal(deal_id, krisha_link)
    duration = round(time.time() - start_time, 2)
    
    print(f"\n⏱️ Время выполнения: {duration} сек")
    print("="*70 + "\n")
    
    return jsonify({
        'status': 'ok' if success else 'failed',
        'duration': duration
    }), 200

if __name__ == '__main__':
    load_config()
    
    if not BITRIX_WEBHOOK_URL.endswith('/'):
        print("⚠️ ВНИМАНИЕ: Webhook URL должен заканчиваться на /")
        sys.exit(1)
    
    print("\n" + "="*70)
    print("⚙️  KRISHA.KZ → BITRIX24 ИНТЕГРАЦИЯ")
    print("="*70)
    print(f"🌐 Webhook: {BITRIX_WEBHOOK_URL}")
    print(f"🔌 Порт: {SERVER_PORT}")
    print(f"📡 URL для робота: http://ВАШ_IP:{SERVER_PORT}/bitrix/krisha_update")
    print("="*70 + "\n")
    
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False)