import json

from api_helpers import post_json


def main() -> None:
    text = "kexit cjplfq jnxtn gj ekexitybzv"
    payload = {
        "text": text,
        "force_llm": False,
        "agent_id": "cline",
    }

    result = post_json("layout/fix", json_payload=payload, auth=True)

    if "error" in result:
        print(f"Error: {result['error']}")
        return

    if result.get("was_fixed"):
        print("Fix applied:")
        print(f"  Original:  {result['original']}")
        print(f"  Corrected: {result['corrected']}")
        print(f"  Method:    {result['method']}")
        print(f"  Confidence:{result['confidence']}")
    else:
        print("No fix required.")
        print(f"  Text: {result.get('original', text)}")


if __name__ == "__main__":
    main()
