"""
Auto-documentation for Project Tree nodes.

Always generates in English regardless of GLM_RESPONSE_LANGUAGE.
Uses semantic search on topic_path + linked artifacts/improvements.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from app.services.governed_artifact import stage_buffered_revision

if TYPE_CHECKING:
    from app.services.project_tree_store import ProjectTreeStore

logger = logging.getLogger(__name__)

_DOC_PROMPT = """\
You are a technical documentation writer for an AI memory system called Supermemory.
Write concise documentation for a project tree node in Markdown.
Always write in English. Be precise and LLM-friendly (avoid fluff).

Node type: {type}
Title: {title}
Description: {description}
Goal: {goal}
Status: {status}
Topic path: {topic_path}

Related memories ({mem_count}):
{memories_text}

Linked canonicals ({canonical_count}):
{canonicals_text}

Related artifacts ({art_count}):
{artifacts_text}

Write the documentation following this structure:
## {title}
**Status:** {status} | **Type:** {type} | **Path:** {topic_path}

### Purpose
<1-2 sentences: what this node represents and why it exists>

### Goal
<what success looks like Ð²Ð‚â€ skip if empty>

### Context
<key facts from related memories and artifacts Ð²Ð‚â€ bullet points, max 5>

### Canonical Links
<reusable canonicals linked to this topic_path Ð²Ð‚â€ bullet points, skip if none>

### Notes
<implementation details, constraints, open questions Ð²Ð‚â€ skip if none>
"""

_TRANSLATE_PROMPT = """\
Translate the following technical documentation to {language}.
Keep all code blocks, paths, and technical terms in English.
Keep the same Markdown structure.

{doc}
"""


async def generate_doc(node: dict, qdrant=None, ollama=None) -> str:
    """Generate English markdown doc for a tree node using semantic search + LLM."""
    from app.services.cloud_llm import cloud_available
    from app.services.llm_gateway import get_cloud_gateway

    topic_path = node.get("topic_path", "")
    memories_text = ""
    canonicals_text = ""
    artifacts_text = ""
    mem_count = 0
    canonical_count = 0
    art_count = 0

    # Pull related memories via topic_path search Ð²Ð‚â€ strictly filtered by project
    project_name = topic_path.split("/")[0] if topic_path else ""
    if qdrant and ollama and topic_path:
        try:
            vec = await ollama.embed(f"{node.get('title','')} {node.get('description','')}")
            results = await qdrant.search(vector=vec, limit=20)
            relevant = []
            for r, score in results:
                if score < 0.55:
                    continue
                is_canonical = r.scope in {"domain", "principle", "meta"}
                if not is_canonical and r.project != project_name:
                    continue
                if r.topic_path and not r.topic_path.startswith(topic_path):
                    continue
                relevant.append(r)
                if len(relevant) >= 6:
                    break
            mem_count = len(relevant)
            memories_text = "\n".join(
                f"- [{r.memory_type}] {r.content[:120]}" for r in relevant if r.scope not in {"domain", "principle", "meta"}
            ) or "none"
            canonicals = [r for r in relevant if r.scope in {"domain", "principle", "meta"}]
            canonical_count = len(canonicals)
            canonicals_text = "\n".join(
                f"- [{r.scope}] {r.topic_path or 'unknown'} Ð²Ð‚â€ {r.content[:120]}" for r in canonicals
            ) or "none"
        except Exception as e:
            logger.debug("Memory search for doc failed: %s", e)
            memories_text = "none"
            canonicals_text = "none"

    try:
        from app.services.learning_store import get_learning_store

        ls = get_learning_store()
        project = topic_path.split("/")[0] if topic_path else ""
        arts = await ls.get_artifacts(project=project, status="active", limit=5)
        art_count = len(arts)
        artifacts_text = "\n".join(
            f"- [{a.get('artifact_type')}] {a.get('content','')[:100]}" for a in arts[:5]
        ) or "none"
    except Exception:
        artifacts_text = "none"

    if not cloud_available():
        return (
            f"## {node.get('title','')}\n\n"
            f"**Status:** {node.get('status','?')} | **Type:** {node.get('type','?')} "
            f"| **Path:** {topic_path}\n\n"
            f"### Purpose\n{node.get('description','') or '_No description yet._'}\n\n"
            f"### Goal\n{node.get('goal','') or '_Not specified._'}\n"
            f"\n### Canonical Links\n{canonicals_text or '_None._'}\n"
        )

    prompt = _DOC_PROMPT.format(
        type=node.get("type", "?"),
        title=node.get("title", ""),
        description=node.get("description", "") or "not set",
        goal=node.get("goal", "") or "not set",
        status=node.get("status", "?"),
        topic_path=topic_path or "not set",
        mem_count=mem_count,
        memories_text=memories_text,
        canonical_count=canonical_count,
        canonicals_text=canonicals_text or "none",
        art_count=art_count,
        artifacts_text=artifacts_text,
    )

    try:
        doc = await get_cloud_gateway().generate(
            prompt,
            system="You are a precise technical writer. Always write in English. Be concise.",
            task_type="text_summarization",
            mode="economy",
            max_tokens=450,
            temperature=0.2,
            allow_local_fallback=True,
        )
        return doc.strip()
    except Exception as e:
        logger.warning("Doc generation failed for node %s: %s", node.get("id"), e)
        return f"## {node.get('title','')}\n\n_Doc generation failed: {e}_"


async def translate_doc(doc: str, language: str) -> str:
    """Translate doc to target language, keeping code/paths in English."""
    from app.services.cloud_llm import cloud_available, describe_cloud_error
    from app.services.llm_gateway import get_cloud_gateway

    if not cloud_available():
        raise RuntimeError("Translation unavailable Ð²Ð‚â€ no cloud LLM configured.")
    prompt = _TRANSLATE_PROMPT.format(language=language, doc=doc)
    try:
        return await get_cloud_gateway().generate(
            prompt,
            system=f"You are a translator. Translate to {language}. Keep technical terms and code in English.",
            task_type="text_summarization",
            mode="economy",
            max_tokens=500,
            temperature=0.1,
            allow_local_fallback=False,
        )
    except Exception as e:
        detail = describe_cloud_error(e)
        logger.warning("Translation failed: %s", detail)
        raise RuntimeError(detail) from e


async def regenerate_node_doc(node_id: str, store: "ProjectTreeStore", qdrant=None, ollama=None, force: bool = False) -> str:
    """Regenerate doc for a node.

    If the node's doc is locked by manual edit and force=False, the generated
    text is saved to doc_candidate instead of doc, preserving the user's edit.
    If force=True (explicit user action), doc is overwritten and lock is cleared.
    """
    node = store.get_node(node_id)
    if not node:
        return ""
    doc = await generate_doc(node, qdrant=qdrant, ollama=ollama)
    locked = (node.get("meta_json") or {}).get("doc_locked", False)
    generated_at = time.time()
    effective_doc, effective_generated_at, candidate_doc, candidate_generated_at = stage_buffered_revision(
        effective_value=node.get("doc", ""),
        effective_updated_at=node.get("doc_generated_at"),
        replacement_value=doc,
        replacement_updated_at=generated_at,
        preserve_effective=bool(locked and not force),
        empty_factory=str,
    )
    store.update_node(
        node_id,
        doc=effective_doc,
        doc_candidate=candidate_doc,
        doc_generated_at=effective_generated_at,
        doc_candidate_generated_at=candidate_generated_at,
    )
    if locked and not force:
        logger.debug("Node %s is locked; saved to doc_candidate", node_id)
    return doc
