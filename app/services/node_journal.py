"""
Node Journal — generates structured session journal entries per task node.

Each entry captures one working session's contribution to a task:
  - What was done
  - Problems encountered & how they were solved
  - What works well
  - Current state / what's next

Entries accumulate chronologically on the node, forming a searchable
task history that surfaces relevant solutions in future sessions.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.project_tree_store import ProjectTreeStore

logger = logging.getLogger(__name__)

_ENTRY_PROMPT = """\
You are a technical writer creating a concise task journal entry from a working session transcript.
Write in {language}. Be precise and concrete — avoid vague summaries.

Task node: {title}
Topic path: {topic_path}
Node status: {status}
Previous journal entries (most recent first, for context):
{prev_entries}

Session transcript (last part of conversation):
{transcript}

Write a journal entry in this exact Markdown structure:

### {date} · {title}

**Выполнено:**
- <bullet per completed action, be specific: what file/function/endpoint was changed and why>

**Проблемы и решения:**
| Проблема | Решение |
|---|---|
| <problem> | <how it was fixed> |

_(omit table if no problems encountered)_

**Что сработало хорошо:**
- <what worked, patterns worth repeating>

**Текущее состояние:**
<1-2 sentences: current working state, what still needs attention>

Rules:
- Use the same language as the node's existing journal (default: {language})
- Be specific: mention file names, function names, error messages where relevant
- Omit sections that have nothing meaningful to say (e.g. no problems → omit that table)
- Max 300 words total
- Output ONLY the markdown entry, no preamble
"""


async def generate_journal_entry(
    node: dict,
    transcript: str,
    prev_entries: list[dict],
    language: str = "Russian",
) -> str:
    """Generate a journal entry for a working session on this node."""
    from app.services.cloud_llm import cloud_available, cloud_complete

    import datetime
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    prev_text = ""
    if prev_entries:
        # Show last 2 entries for context (newest first)
        parts = []
        for e in prev_entries[:2]:
            ts = time.strftime("%Y-%m-%d", time.localtime(e["created_at"]))
            parts.append(f"--- {ts} ---\n{e['content'][:400]}")
        prev_text = "\n\n".join(parts)
    else:
        prev_text = "(first entry)"

    # Trim transcript to fit context
    transcript = transcript[-3000:] if len(transcript) > 3000 else transcript

    if not cloud_available():
        # Minimal fallback without LLM
        return (
            f"### {date_str} · {node.get('title', '')}\n\n"
            f"**Выполнено:**\n- _(LLM недоступен — заполните вручную)_\n\n"
            f"**Текущее состояние:**\n{node.get('status', '?')}\n"
        )

    prompt = _ENTRY_PROMPT.format(
        language=language,
        title=node.get("title", ""),
        topic_path=node.get("topic_path", ""),
        status=node.get("status", "?"),
        prev_entries=prev_text,
        transcript=transcript,
        date=date_str,
    )

    try:
        from app.services.llm_gateway import get_cloud_gateway

        entry = await get_cloud_gateway().generate(
            prompt,
            system="You are a precise technical writer. Write concise, actionable journal entries.",
            task_type="text_summarization",
            mode="economy",
            max_tokens=420,
            temperature=0.2,
            allow_local_fallback=True,
            prefer_local=True,
        )
        return entry.strip()
    except Exception as e:
        logger.warning("Journal entry generation failed for node %s: %s", node.get("id"), e)
        return (
            f"### {date_str} · {node.get('title', '')}\n\n"
            f"_Генерация не удалась: {e}_\n"
        )


async def add_session_journal_entry(
    topic_path: str,
    transcript: str,
    session_id: str,
    store: "ProjectTreeStore",
    language: str = "Russian",
) -> str | None:
    """
    Find node by topic_path, generate a journal entry from the transcript,
    save it to the store. Returns the entry text or None if node not found.
    """
    node = store.get_by_topic_path(topic_path)
    if not node:
        logger.debug("Journal: no node found for topic_path=%s", topic_path)
        return None

    prev_entries = store.get_journal(node["id"], limit=3)
    entry = await generate_journal_entry(
        node=node,
        transcript=transcript,
        prev_entries=prev_entries,
        language=language,
    )
    store.add_journal_entry(node["id"], entry, session_id=session_id)
    logger.info("Journal entry added for node %s (%s)", node["id"], topic_path)
    return entry
