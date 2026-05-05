import argparse
import json
import sys
from pathlib import Path

import httpx

# Добавляем корень проекта в пути импорта
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from app.config import settings

def main():
    parser = argparse.ArgumentParser(description="MnemoForge API CLI (UTF-8 Safe)")
    parser.add_argument("method", choices=["GET", "POST", "DELETE", "PATCH"], help="HTTP Method")
    parser.add_argument("endpoint", help="API Endpoint (e.g., /knowledge-tree/slice)")
    parser.add_argument("-d", "--data", help="JSON data string", default=None)
    
    args = parser.parse_args()
    
    # Формируем URL
    port = getattr(settings, "server_port", 8000)
    prefix = getattr(settings, "api_prefix", "/api/v1")
    url = f"http://localhost:{port}{prefix}{args.endpoint}"

    # Читаем ключ напрямую из настроек
    headers = {}
    if settings.api_key:
        headers["X-Api-Key"] = settings.api_key

    # Безопасный парсинг JSON с поддержкой кириллицы
    payload = None
    if args.data:
        try:
            payload = json.loads(args.data)
        except json.JSONDecodeError as e:
            print(f"❌ Ошибка парсинга JSON: {e}")
            sys.exit(1)
            
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method=args.method, url=url, headers=headers, json=payload)
            print(f"📥 Статус: {response.status_code}")
            try:
                # ensure_ascii=False гарантирует, что мы увидим русский текст, а не \u043a\u0430\u043a
                print(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except Exception:
                print(response.text)
    except Exception as e:
        print(f"❌ Ошибка соединения: {e}")

if __name__ == "__main__":
    main()
