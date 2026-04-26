import requests
import json

url = 'http://localhost:6333/collections/agent_memories/points/scroll'
payload = {
    'filter': {
        'must': [
            {'key': 'category', 'match': {'value': 'skill'}},
            {'key': 'domain_tags', 'match': {'any': ['python']}}
        ]
    },
    'limit': 5,
    'with_payload': True,
    'with_vectors': False
}

try:
    resp = requests.post(url, json=payload, timeout=30)
    print('Status:', resp.status_code)
    print('Response:', resp.text[:1000])
except Exception as e:
    print('Error:', str(e))
