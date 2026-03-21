import urllib.request
import json

text = "kexit cjplfq jnxtn gj ekexitybzv"
data = json.dumps({
    'text': text,
    'force_llm': False,
    'agent_id': 'cline'
}).encode('utf-8')

req = urllib.request.Request(
    'http://localhost:8000/api/v1/layout/fix',
    data=data,
    headers={'Content-Type': 'application/json'}
)

try:
    response = urllib.request.urlopen(req)
    result = json.loads(response.read().decode())
    
    if result.get('was_fixed'):
        print(f"Оригинал: {result['original']}")
        print(f"Исправлено: {result['corrected']}")
        print(f"Метод: {result['method']}")
        print(f"Уверенность: {result['confidence']}")
    else:
        print(f"Исправление не нужно: {result['original']}")
except Exception as e:
    print(f"Ошибка: {e}")