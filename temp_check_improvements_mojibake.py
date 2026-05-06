import asyncio
from app.services.improvements_store import get_improvements_store
from app.services.text_localization import looks_like_mojibake, repair_mojibake

async def check():
    store = get_improvements_store()
    rows = await store.list(project='mnemoforge', status='open', limit=50)

    mojibake_count = 0
    for row in rows:
        title = row['title']
        desc = row['description']

        title_mojibake = looks_like_mojibake(title)
        desc_mojibake = looks_like_mojibake(desc)

        if title_mojibake or desc_mojibake:
            print(f'Mojibake found in improvement {row["id"]}:')
            if title_mojibake:
                print(f'  Title: {title[:100]}...')
                repaired_title = repair_mojibake(title)
                if repaired_title != title:
                    print(f'  Repaired title: {repaired_title[:100]}...')
            if desc_mojibake:
                print(f'  Description: {desc[:100]}...')
                repaired_desc = repair_mojibake(desc)
                if repaired_desc != desc:
                    print(f'  Repaired description: {repaired_desc[:100]}...')
            mojibake_count += 1

    print(f'\nTotal mojibake records in improvements: {mojibake_count}')

if __name__ == "__main__":
    asyncio.run(check())
