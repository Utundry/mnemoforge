import asyncio
from app.services.learning_store import get_learning_store
from app.services.text_localization import looks_like_mojibake, repair_mojibake

async def check():
    store = get_learning_store()
    rows = await store.list_artifacts(scope="runtime_hint", status="active", limit=100)

    mojibake_count = 0
    for row in rows:
        content = str(row.get("content", ""))
        observation = str(row.get("observation", ""))
        why = str(row.get("why_it_matters", ""))

        content_mojibake = looks_like_mojibake(content)
        observation_mojibake = looks_like_mojibake(observation)
        why_mojibake = looks_like_mojibake(why)

        if content_mojibake or observation_mojibake or why_mojibake:
            print(f'Mojibake found in hint {row.get("id")}:')
            if content_mojibake:
                print(f'  Content: {content[:100]}...')
                repaired = repair_mojibake(content)
                if repaired != content:
                    print(f'  Repaired: {repaired[:100]}...')
            if observation_mojibake:
                print(f'  Observation: {observation[:100]}...')
                repaired = repair_mojibake(observation)
                if repaired != observation:
                    print(f'  Repaired: {repaired[:100]}...')
            if why_mojibake:
                print(f'  Why: {why[:100]}...')
                repaired = repair_mojibake(why)
                if repaired != why:
                    print(f'  Repaired: {repaired[:100]}...')
            mojibake_count += 1

    print(f'\nTotal mojibake records in runtime hints: {mojibake_count}')

if __name__ == "__main__":
    asyncio.run(check())
