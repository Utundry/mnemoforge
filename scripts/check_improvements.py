import json

from api_helpers import get_json


def main() -> None:
    params = {
        "project": "supermemory",
        "status": "all",
        "limit": 200,
    }
    data = get_json("improvements", params=params, auth=True)
    if "error" in data:
        print(f"Error: {data['error']}")
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
