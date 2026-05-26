from datetime import datetime

from api_helpers import get_json


def check_health() -> dict:
    return get_json("health")


def check_stats() -> dict:
    return get_json("stats", auth=True)


def check_improvements() -> dict:
    params = {
        "project": "sloplesscode",
        "status": "all",
        "limit": 200,
    }
    return get_json("improvements", params=params, auth=True)


def print_section(title: str) -> None:
    print(title)
    print('-' * len(title))


def main() -> None:
    print("============================================================")
    print("SLOPSTOP STATUS REPORT")
    print("============================================================")
    print(f"Report time: {datetime.now():%Y-%m-%d %H:%M:%S}\n")

    print_section("HEALTH")
    health = check_health()
    if "error" in health:
        print(f"Health error: {health['error']}")
    else:
        print(f"Server: {health.get('server', 'OK')}")
        print(f"Qdrant: {health.get('qdrant', 'OK')}")
        print(f"Ollama: {health.get('ollama', 'OK')}")
    print()

    print_section("STATS")
    stats = check_stats()
    if "error" in stats:
        print(f"Stats error: {stats['error']}")
    else:
        print(f"Points count: {stats.get('points_count', 0)}")
        print(f"Vector count: {stats.get('vectors_count', 0)}")
        print(f"Indexed vectors: {stats.get('indexed_vectors_count', 0)}")
        print(f"Status: {stats.get('status', 'unknown')}")
    print()

    print_section("IMPROVEMENTS")
    improvements = check_improvements()
    if "error" in improvements:
        print(f"Improvements error: {improvements['error']}")
    else:
        total = len(improvements)
        resolved = sum(1 for imp in improvements if imp.get("status") == "resolved")
        open_count = total - resolved
        print(f"Total improvements: {total}")
        if total:
            print(f"Resolved: {resolved} ({resolved/total*100:.1f}% of total)")
            print(f"Open: {open_count} ({open_count/total*100:.1f}% of total)")
        else:
            print("Resolved: 0 (0%)")
            print("Open: 0 (0%)")
        if open_count:
            print("\nOpen improvements:")
            for imp in improvements:
                if imp.get("status") != "resolved":
                    title = imp.get("title", "missing title")
                    print(f"- {title}\n  Importance: {imp.get('importance_score', 0):.2f} | ID: {imp.get('id')}")
        print("\nLatest 5 improvements:")
        sorted_imps = sorted(improvements, key=lambda imp: imp.get("timestamp", ""), reverse=True)[:5]
        for imp in sorted_imps:
            status_icon = "✓" if imp.get("status") == "resolved" else "!"
            title = imp.get("title", "missing title")
            timestamp = imp.get("timestamp", "")[:10]
            importance = imp.get("importance_score", 0)
            print(f"  {status_icon} {title[:60]} | {timestamp} | Importance {importance:.2f}")

    print("\n============================================================")
    print("END OF REPORT")
    print("============================================================")


if __name__ == "__main__":
    main()
