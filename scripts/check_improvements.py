import urllib.request
import json

url = "http://localhost:8000/api/v1/improvements"
params = {
    'project': 'supermemory',
    'status': 'all',
    'limit': 200
}

# Build URL with params
query_string = '&'.join(f"{k}={v}" for k, v in params.items())
full_url = f"{url}?{query_string}"

try:
    with urllib.request.urlopen(full_url) as response:
        data = json.loads(response.read().decode())
        print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"Error: {e}")