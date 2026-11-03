from flask import Flask, request, jsonify
import requests, re, json, time, configparser, sys, urllib.parse, os, threading
from bs4 import BeautifulSoup
from typing import Optional, Dict

app = Flask(__name__)

# --- GLOBALS ---
BITRIX_WEBHOOK_URL = ""
SERVER_PORT = 5000
ALLOWED_PHONES = []
ASSIGNED_USER_ID = ""

# Настройки авто-парсера
AUTO_PARSER_ENABLE = False
CATALOG_URL = ""
CHECK_INTERVAL_SEC = 10
DEFAULT_PHONE = ""
PHONE_FIELD_ID = ""

FIELD_MAP = {
    "TITLE": "TITLE",
    "PRICE": "UF_CRM_1621497190023",
    "STREET": "UF_CRM_1622791092",
    "HOUSE_NUMBER": "UF_CRM_1622607023",
    "AREA": "UF_CRM_1624341576",
    "FLOOR": "UF_CRM_1621591973519",
    "FLOORS_TOTAL": "UF_CRM_1621591973519",
    "HOUSE_TYPE": "UF_CRM_1623396322",
    "YEAR": "UF_CRM_1641969918631",
    "BATHROOM": "UF_CRM_1621928135362",
    "BALCONY": "UF_CRM_1621928221551",
    "STATE": "UF_CRM_1621591769132",
    "ROOMS": "UF_CRM_1621849186398",
    "DESCRIPTION": "COMMENTS",
    "KRISHA_LINK": "UF_CRM_1618989147417"
}

HOUSE_TYPE_MAP = {"кирпичный": "145", "панельный": "147", "монолитный": "169", "пеноблок": "199"}
BATHROOM_MAP = {"раздельный": "91", "совмещенный": "93", "два и более": "433", "во дворе": "177"}
BALCONY_MAP = {"балкон": "95", "лоджия": "133", "балкон + лоджия": "97", "нет балкона": "135"}
STATE_MAP = {"хорошее": "53", "среднее": "55", "требует ремонта": "57", "евро - ремонт": "59"}

DB_FILE = 'processed_ads.json'

def load_config():
    global BITRIX_WEBHOOK_URL, SERVER_PORT, ALLOWED_PHONES, ASSIGNED_USER_ID
    global AUTO_PARSER_ENABLE, CATALOG_URL, CHECK_INTERVAL_SEC, DEFAULT_PHONE, PHONE_FIELD_ID
    
    config = configparser.ConfigParser()
    try:
        config.read('config.ini', encoding='utf-8')
        BITRIX_WEBHOOK_URL = config.get('BITRIX24', 'WEBHOOK_URL')
        SERVER_PORT = config.getint('SERVER', 'PORT', fallback=5000)
        
        # Загружаем телефоны и оставляем только цифры
        phones_raw = config.get('ACCESS', 'ALLOWED_PHONES', fallback='')
        ALLOWED_PHONES = [re.sub(r'\D', '', p) for p in phones_raw.split(',') if p.strip()]
        ASSIGNED_USER_ID = config.get('ACCESS', 'ASSIGNED_USER_ID', fallback='')
        
        if config.has_section('AUTO_PARSER'):
            AUTO_PARSER_ENABLE = config.getboolean('AUTO_PARSER', 'ENABLE', fallback=False)
            CATALOG_URL = config.get('AUTO_PARSER', 'CATALOG_URL', fallback='')
            CHECK_INTERVAL_SEC = config.getint('AUTO_PARSER', 'CHECK_INTERVAL_SEC', fallback=20)
            DEFAULT_PHONE = config.get('AUTO_PARSER', 'DEFAULT_PHONE', fallback='+77777777777')
            PHONE_FIELD_ID = config.get('AUTO_PARSER', 'PHONE_FIELD_ID', fallback='')
            if PHONE_FIELD_ID:
                FIELD_MAP["PHONE"] = PHONE_FIELD_ID
            
    except Exception as e:
        print("❌ Ошибка чтения config.ini:", e)
        sys.exit(1)

def extract_window_data(html: str) -> Optional[Dict]:
    m = re.search(r'window\.data\s*=\s*({.*?});\s*</script>', html, flags=re.S)
    if not m:
        m = re.search(r'window\.data\s*=\s*({.+});', html, flags=re.S)
    if not m: return None
    try:
        return json.loads(re.sub(r',(\s*[}\]])', r'\1', m.group(1)))
    except:
        return None

def parse_address(full_address: str) -> tuple:
    if not full_address: return None, None
    address = full_address.replace(' - ', '-').replace('- ', '-').strip()
    match = re.search(r'^(.+?)\s+([\d]+[а-яА-Яa-zA-Z]?(?:[/-][\d]+[а-яА-Яa-zA-Z]?)?)$', address)
    if match: return match.group(1).strip(), match.group(2).strip()
    return address, None

def parse_krisha_page(krisha_url: str) -> Optional[Dict]:
    print(f"🔗 Парсинг URL: {krisha_url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(krisha_url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"❌ Ошибка скачивания: {e}")
        return None

    result = {}
    wd = extract_window_data(html)
    
    if wd and 'advert' in wd:
        adv = wd['advert']
        if adv.get('price'): result['price'] = int(adv['price'])
        if adv.get('rooms'): result['rooms'] = int(adv['rooms'])
        full_address = adv.get('addressTitle') or adv.get('fullAddress') or adv.get('address')
        if full_address:
            street, house_num = parse_address(full_address)
            result['street'] = street
            result['house_number'] = house_num
        if adv.get('square'): result['area'] = float(adv['square'])
        result['title'] = adv.get('title')
        result['description'] = adv.get('description', '')
    
    soup = BeautifulSoup(html, 'html.parser')
    for item in soup.find_all('div', class_='offer__info-item'):
        title_div = item.find('div', class_='offer__info-title')
        value_div = item.find('div', class_='offer__advert-short-info')
        if not title_div or not value_div: continue
        label = title_div.get_text(strip=True)
        value = value_div.get_text(strip=True)
        
        if 'Тип дома' in label: result['house_type'] = value.lower()
        elif 'Год постройки' in label:
            m = re.search(r'(\d{4})', value)
            if m: result['year'] = int(m.group(1))
        elif 'Этаж' in label:
            m = re.search(r'(\d+)\s+из\s+(\d+)', value)
            if m: result['floor'], result['floors_total'] = int(m.group(1)), int(m.group(2))
        elif 'Площадь' in label and not result.get('area'):
            m = re.search(r'([\d.]+)', value)
            if m: result['area'] = float(m.group(1))
        elif 'Санузел' in label: result['bathroom'] = value.lower()
        elif 'Балкон' in label: result['balcony'] = value.lower()
        elif 'Состояние' in label: result['state'] = value.lower()
    
    if result.get('rooms') and result.get('street'):
        house = result.get('house_number') or ''
        result['title_generated'] = f"пр {result['rooms']}к {result['street']} {house}".strip()
    else:
        result['title_generated'] = result.get('title', 'Новая сделка Krisha')
    
    result['link'] = krisha_url
    return result

def build_bitrix_fields(parsed: dict) -> dict:
    fields = {}
    if parsed.get('title_generated'): fields[FIELD_MAP['TITLE']] = parsed['title_generated']
    if parsed.get('price'): fields[FIELD_MAP['PRICE']] = parsed['price']
    if parsed.get('street'): fields[FIELD_MAP['STREET']] = parsed['street']
    if parsed.get('house_number'): fields[FIELD_MAP['HOUSE_NUMBER']] = parsed['house_number']
    if parsed.get('area'): fields[FIELD_MAP['AREA']] = parsed['area']
    if parsed.get('floor') and parsed.get('floors_total'):
        fields[FIELD_MAP['FLOOR']] = f"{parsed['floor']}/{parsed['floors_total']}"
    elif parsed.get('floor'): fields[FIELD_MAP['FLOOR']] = str(parsed['floor'])
    
    if parsed.get('house_type'):
        for k, v in HOUSE_TYPE_MAP.items():
            if k in parsed['house_type']: fields[FIELD_MAP['HOUSE_TYPE']] = v; break
    if parsed.get('year'): fields[FIELD_MAP['YEAR']] = parsed['year']
    if parsed.get('bathroom'):
        for k, v in BATHROOM_MAP.items():
            if k in parsed['bathroom']: fields[FIELD_MAP['BATHROOM']] = v; break
    if parsed.get('balcony'):
        for k, v in BALCONY_MAP.items():
            if k in parsed['balcony']: fields[FIELD_MAP['BALCONY']] = v; break
    if parsed.get('state'):
        for k, v in STATE_MAP.items():
            if k in parsed['state']: fields[FIELD_MAP['STATE']] = v; break
            
    if parsed.get('rooms'): fields[FIELD_MAP['ROOMS']] = parsed['rooms']
    if parsed.get('description'): fields[FIELD_MAP['DESCRIPTION']] = parsed['description'][:1000]
    if parsed.get('link'): fields[FIELD_MAP['KRISHA_LINK']] = parsed['link']
    
    # Жесткие настройки маршрутизации сделки
    if ASSIGNED_USER_ID: fields["ASSIGNED_BY_ID"] = ASSIGNED_USER_ID
    fields["CATEGORY_ID"] = 7
    fields["STAGE_ID"] = "C7:NEW"
    
    return fields

def update_bitrix_deal(deal_id: int, krisha_url: str) -> bool:
    parsed = parse_krisha_page(krisha_url)
    if not parsed: return False
    fields = build_bitrix_fields(parsed)
    url = urllib.parse.urljoin(BITRIX_WEBHOOK_URL, 'crm.deal.update')
    try:
        r = requests.post(url, json={"id": deal_id, "fields": fields}, timeout=15)
        if r.ok and r.json().get('result'):
            print(f"✅ Сделка {deal_id} обновлена!")
            return True
        print(f"❌ Ошибка Битрикс: {r.text}")
        return False
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

# ==========================================
# БЛОК: АВТО-ПАРСЕР (ТОЛЬКО ПЕРВАЯ СТРАНИЦА)
# ==========================================

def load_db() -> dict:
    if not os.path.exists(DB_FILE): return {}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return {}

def save_db(db_data: dict):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(db_data, f, indent=4)

def add_new_bitrix_deal(krisha_url: str) -> bool:
    parsed = parse_krisha_page(krisha_url)
    if not parsed: return False
    
    fields = build_bitrix_fields(parsed)
    if DEFAULT_PHONE and FIELD_MAP.get("PHONE"):
        fields[FIELD_MAP["PHONE"]] = DEFAULT_PHONE
        
    url = urllib.parse.urljoin(BITRIX_WEBHOOK_URL, 'crm.deal.add')
    try:
        r = requests.post(url, json={"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}, timeout=15)
        if r.ok and r.json().get('result'):
            print(f"✅ Создана сделка ID: {r.json().get('result')}")
            return True
        print(f"❌ Ошибка Битрикс при создании: {r.text}")
        return False
    except Exception as e:
        print(f"❌ Ошибка соединения: {e}")
        return False

def auto_parser_worker():
    print(f"auto parse {CHECK_INTERVAL_SEC} sec.")
    
    while True:
        try:
            db = load_db()
            current_time = time.time()
            
            # Проверка старых сделок (24 часа)
            '''for ad_id, info in list(db.items()):
                if current_time - info['timestamp'] >= 86400:
                    print(f"\n⏳ Прошло 24 часа! Пересоздаю: {ad_id}")
                    if add_new_bitrix_deal(info['url']):
                        db[ad_id]['timestamp'] = current_time
                        save_db(db)
                    time.sleep(3)'''
            
            # Парсинг первой страницы
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
            resp = requests.get(CATALOG_URL, headers=headers, timeout=15)
            
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                cards = soup.find_all('div', class_='a-card')
                
                new_ads = 0
                for card in cards:
                    title_link = card.find('a', class_='a-card__title')
                    if title_link:
                        link = title_link.get('href')
                        if link and '/a/show/' in link:
                            ad_id = link.split('/')[-1].split('?')[0]
                            full_url = "https://krisha.kz" + link
                            
                            if ad_id not in db:
                                print(f"\n🌟 НАЙДЕНО НОВОЕ: {ad_id}")
                                if add_new_bitrix_deal(full_url):
                                    db[ad_id] = {"url": full_url, "timestamp": time.time()}
                                    save_db(db)
                                    new_ads += 1
                                time.sleep(3)
                
                print(f"⏱️ Проверка завершена (Новых: {new_ads}). Жду {CHECK_INTERVAL_SEC} сек...")
            else:
                print(f"⚠️ Ошибка доступа к Крыше. Код: {resp.status_code}.")
                
        except Exception as e:
            print(f"❌ Сбой в авто-парсере: {e}")
            
        time.sleep(CHECK_INTERVAL_SEC)

# ==========================================
# ВЕБХУК (РУЧНОЕ ОБНОВЛЕНИЕ)
# ==========================================

@app.route('/bitrix/krisha_update', methods=['POST', 'GET'])
def handle_bitrix_webhook():
    print("\n" + "="*70)
    print("📩 НОВЫЙ ЗАПРОС ОТ БИТРИКС24")
    print("="*70)
    
    data = request.values.to_dict()
    if request.is_json: data.update(request.get_json(silent=True) or {})
    if not data:
        raw_body = request.data.decode('utf-8', errors='ignore').strip()
        if "=" in raw_body:
            raw_body = raw_body.replace('&', '&')
            raw_body = re.sub(r'\s*\[.*\]$', '', raw_body).strip()
            data.update(dict(urllib.parse.parse_qsl(raw_body)))

    print(f"📦 ВСЕ ПОЛУЧЕННЫЕ ДАННЫЕ: {data}")
    
    # Достаем и очищаем номер телефона (оставляем только цифры)
    raw_phone = str(data.get("phone", "")).strip()
    clean_phone = re.sub(r'\D', '', raw_phone)
    
    if ALLOWED_PHONES and clean_phone not in ALLOWED_PHONES:
        print(f"🚫 Доступ запрещён для номера '{raw_phone}' (очищенный: '{clean_phone}')")
        return jsonify({"error": "forbidden"}), 403
    else:
        print(f"✅ Номер '{clean_phone}' имеет доступ")
    
    deal_id_str = data.get('deal_id') or data.get('deal')
    krisha_link = data.get('link') or data.get('url')
    
    if not deal_id_str or not krisha_link:
        print("❌ Отсутствует deal_id или link")
        return jsonify({'status': 'error', 'message': 'Missing deal_id or link'}), 400
    
    try: deal_id = int(deal_id_str)
    except: return jsonify({'status': 'error', 'message': 'Invalid deal_id'}), 400
    
    start_time = time.time()
    success = update_bitrix_deal(deal_id, krisha_link)
    duration = round(time.time() - start_time, 2)
    
    print(f"\n⏱️ Время выполнения: {duration} сек")
    print("="*70 + "\n")
    
    return jsonify({'status': 'ok' if success else 'failed'}), 200

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
    
    if AUTO_PARSER_ENABLE and CATALOG_URL:
        threading.Thread(target=auto_parser_worker, daemon=True).start()
    else:
        print("⏸️  Авто-парсер ОТКЛЮЧЕН в config.ini")
        
    print("="*70 + "\n")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False)