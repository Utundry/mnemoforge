"""Проверка фикса: finish_task_session resolve unified artifact.
Использует /api/v1 prefix (см. main.py: prefix = settings.api_prefix).
"""
import httpx
import json
import uuid
import time

API_BASE = "http://localhost:8000"
API_KEY = "supermemory-local"
PREFIX = "/api/v1"

def _headers():
    return {"X-Api-Key": API_KEY, "Content-Type": "application/json"}

# 1. Создать improvement (как это делает record_work_result с create_issue_if_unmatched=True)
print("=== 1. Создание improvement ===")
improvement_payload = {
    "project": "mnemoforge",
    "title": "Test task for resolve fix",
    "description": "Testing that finish_task_session resolves unified artifacts properly",
    "agent_id": "codex",
    "importance_score": 0.65,
    "stage": "proposal",
    "tags": ["work-result", "mcp-facade", "project:mnemoforge", "test-fix"],
}
r = httpx.post(f"{API_BASE}{PREFIX}/improvements", json=improvement_payload, headers=_headers(), timeout=10)
print(f"  Status: {r.status_code}")
if r.status_code not in (200, 201):
    print(f"  Response: {r.text[:300]}")
    exit(1)
improvement = r.json()
improvement_id = improvement.get("memory_id") or improvement.get("id", "")
print(f"  Improvement ID: {improvement_id}")
print(f"  Improvement: {json.dumps(improvement, indent=2, ensure_ascii=False)[:300]}")

# 2. Создать task record в tasks_store (как это делает create_or_update_project_task)
print("\n=== 2. Создание project task ===")
task_id = str(uuid.uuid4())
task_payload = {
    "project": "mnemoforge",
    "task_id": task_id,
    "title": "Test project task",
    "description": "Testing resolve fix",
    "agent_id": "codex",
    "status": "active",
    "source": "test",
    "tags": ["test"],
    "linked_improvement_id": improvement_id,
}
r = httpx.post(f"{API_BASE}{PREFIX}/project/tasks", json=task_payload, headers=_headers(), timeout=10)
print(f"  Status: {r.status_code}")
if r.status_code not in (200, 201):
    print(f"  Response: {r.text[:200]}")
else:
    print(f"  Task ID: {task_id}")

# 3. Список open artifacts ДО resolve
print("\n=== 3. List open artifacts BEFORE resolve ===")
r = httpx.get(f"{API_BASE}{PREFIX}/artifacts?project=mnemoforge&status=open&limit=50", headers=_headers(), timeout=10)
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    items = r.json().get("items", [])
    for item in items:
        aid = item.get("artifact_key", item.get("id", "?"))
        atype = item.get("type", "?")
        astatus = item.get("status", "?")
        atitle = item.get("title", "?")[:60]
        print(f"    - {aid} [{atype}] status={astatus} title={atitle}")
    print(f"  Total open artifacts: {len(items)}")
else:
    print(f"  Response: {r.text[:300]}")

# 4. Симулировать finish_task_session: resolve improvement
print("\n=== 4. Resolve improvement (как делает finish_task_session) ===")
artifact_key_improvement = f"improvement:mnemoforge:{improvement_id}"
r = httpx.get(f"{API_BASE}{PREFIX}/artifacts/{artifact_key_improvement}", headers=_headers(), timeout=10)
print(f"  GET artifact (improvement): status={r.status_code}")
if r.status_code == 200:
    print(f"    Status before resolve: {r.json().get('status')}")
else:
    print(f"    Response: {r.text[:200]}")

r = httpx.post(
    f"{API_BASE}{PREFIX}/artifacts/{artifact_key_improvement}/resolve",
    json={"acted_by": "codex", "action_source": "finish_task_session", "reason": "Task session finished."},
    headers=_headers(),
    timeout=10,
)
print(f"  Resolve improvement: status={r.status_code}")
if r.status_code == 200:
    result = r.json()
    print(f"    Resolved status: {result.get('status')}")
    print(f"    Linked: {result.get('linked_artifact_key')} status={result.get('linked_status')}")
else:
    print(f"    Response: {r.text[:300]}")

# 5. Resolve task 
print("\n=== 5. Resolve task (как делает finish_task_session) ===")
artifact_key_task = f"task:mnemoforge:{task_id}"
r = httpx.get(f"{API_BASE}{PREFIX}/artifacts/{artifact_key_task}", headers=_headers(), timeout=10)
print(f"  GET artifact (task): status={r.status_code}")
if r.status_code == 200:
    print(f"    Status before resolve: {r.json().get('status')}")
else:
    print(f"    Response: {r.text[:200]}")

r = httpx.post(
    f"{API_BASE}{PREFIX}/artifacts/{artifact_key_task}/resolve",
    json={"acted_by": "codex", "action_source": "finish_task_session", "reason": "Task session finished."},
    headers=_headers(),
    timeout=10,
)
print(f"  Resolve task: status={r.status_code}")
if r.status_code == 200:
    result = r.json()
    print(f"    Resolved status: {result.get('status')}")
    print(f"    Linked: {result.get('linked_artifact_key')} status={result.get('linked_status')}")
else:
    print(f"    Response: {r.text[:300]}")

# 6. Список open artifacts ПОСЛЕ resolve
print("\n=== 6. List open artifacts AFTER resolve ===")
time.sleep(0.5)
r = httpx.get(f"{API_BASE}{PREFIX}/artifacts?project=mnemoforge&status=open&limit=50", headers=_headers(), timeout=10)
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    items = r.json().get("items", [])
    for item in items:
        aid = item.get("artifact_key", item.get("id", "?"))
        atype = item.get("type", "?")
        astatus = item.get("status", "?")
        atitle = item.get("title", "?")[:60]
        print(f"    - {aid} [{atype}] status={astatus} title={atitle}")
    print(f"  Total open artifacts: {len(items)}")
else:
    print(f"  Response: {r.text[:300]}")

# 7. Проверить, что оба resolve'нуты
print("\n=== 7. Verify artifacts are resolved ===")
for label, akey in [("improvement", artifact_key_improvement), ("task", artifact_key_task)]:
    r = httpx.get(f"{API_BASE}{PREFIX}/artifacts/{akey}", headers=_headers(), timeout=10)
    if r.status_code == 200:
        art = r.json()
        status = art.get("status", "?")
        linked_key = art.get("linked_artifact_key", "")
        linked_status = art.get("linked_status", "")
        print(f"  {label}: {akey}")
        print(f"         status={status} linked_key={linked_key} linked_status={linked_status}")
        print(f"         PASS={status in ('done', 'resolved')}")
    else:
        print(f"  {label}: {akey} GET failed: {r.status_code} {r.text[:100]}")

print("\n=== DONE ===")
