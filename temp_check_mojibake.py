import asyncio
from app.services.improvements_store import get_improvements_store
from app.services.text_localization import looks_like_mojibake

async def check():
    store = get_improvements_store()
    rows = await store.list(project='supermemory', status='open', limit=50)
    mojibake_count = 0
    for row in rows:
        title = row['title']
        desc = row['description']
        if looks_like_mojibake(title) or looks_like_mojibake(desc):
            print(f'Mojibake found in {row["id"]}:')
            print(f'  Title: {title[:80]}...')
            print(f'  Desc: {desc[:80]}...')
            mojibake_count += 1
    print(f'Total mojibake records: {mojibake_count}')

if __name__ == "__main__":
    asyncio.run(check())
