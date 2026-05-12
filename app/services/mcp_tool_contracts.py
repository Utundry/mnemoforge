from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import quote


_SHARED_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "load_instruction_layer": {
        "name": "load_instruction_layer",
        "description": (
            "Load a specific instruction layer on demand. "
            "Use L3 for detailed API reference, L4 for advanced/experimental features."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["layer"],
            "properties": {
                "layer": {
                    "type": "string",
                    "enum": ["L3", "L4"],
                    "description": "Layer to load (L3 for detailed reference, L4 for advanced)",
                },
                "category": {
                    "type": "string",
                    "description": "Category for L3 layer (e.g., 'memory_operations', 'skills')",
                },
                "section": {
                    "type": "string",
                    "description": "Section to load for L3 (api_reference, examples, troubleshooting)",
                },
            },
        },
    },
    "list_instruction_layers": {
        "name": "list_instruction_layers",
        "description": (
            "List available instruction layers. "
            "Use this to discover what layers and categories are available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {
                    "type": "string",
                    "enum": ["L2", "L3", "L4"],
                    "description": "Filter by layer (optional)",
                },
            },
        },
    },
    "list_learning_candidates": {
        "name": "list_learning_candidates",
        "description": (
            "List learning candidates awaiting user review. "
            "Use this before approving, deferring, or rejecting a candidate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                "artifact_type": {"type": "string"},
                "agent_id": {"type": "string"},
            },
        },
    },
    "approve_learning_candidate": {
        "name": "approve_learning_candidate",
        "description": (
            "Explicitly approve a pending learning candidate. "
            "Promotes it from candidate to active runtime_hint and records user approval metadata."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["artifact_id"],
            "properties": {
                "artifact_id": {"type": "string"},
                "approved_by": {"type": "string"},
                "approval_source": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    "defer_learning_candidate": {
        "name": "defer_learning_candidate",
        "description": (
            "Defer review of a pending learning candidate. "
            "Raises the resurface threshold and records explicit user deferral metadata."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["artifact_id"],
            "properties": {
                "artifact_id": {"type": "string"},
                "defer_days": {"type": "integer", "minimum": 1, "maximum": 90},
                "deferred_by": {"type": "string"},
                "defer_source": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    "reject_learning_candidate": {
        "name": "reject_learning_candidate",
        "description": (
            "Reject a pending learning candidate. "
            "Archives it and records explicit user rejection metadata."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["artifact_id"],
            "properties": {
                "artifact_id": {"type": "string"},
                "rejected_by": {"type": "string"},
                "rejection_source": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    "review_improvement": {
        "name": "review_improvement",
        "description": (
            "Set stage/verdict for an improvement without changing lifecycle status. "
            "Use this when you want to mark an improvement as beta_test/stable or effective/ineffective."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["improvement_id"],
            "properties": {
                "improvement_id": {"type": "string", "description": "UUID of the improvement"},
                "stage": {
                    "type": "string",
                    "enum": ["proposal", "beta_test", "experimental", "stable", "deprecated"],
                    "description": "Stage of the improvement",
                },
                "verdict": {
                    "type": "string",
                    "enum": ["effective", "ineffective"],
                    "description": "Quality verdict for the improvement",
                },
                "reviewed_by": {"type": "string", "default": "user"},
                "review_source": {"type": "string", "default": "manual_review"},
                "reason": {"type": "string", "default": ""},
            },
        },
    },
    "list_project_aliases": {
        "name": "list_project_aliases",
        "description": (
            "List active project identity aliases. Use this to see which historical project names "
            "resolve to a canonical project id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project id or alias to scope the alias list.",
                },
            },
        },
    },
    "rename_project": {
        "name": "rename_project",
        "description": (
            "Rename a project identity as a normal lifecycle operation. Defaults to dry-run; "
            "set apply=true to rewrite structured SQLite project references and create aliases."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["old_project_id", "new_project_id"],
            "properties": {
                "old_project_id": {"type": "string", "description": "Historical project id or working name."},
                "new_project_id": {"type": "string", "description": "Canonical release/current project id."},
                "apply": {"type": "boolean", "default": False, "description": "Apply changes. False returns a dry-run report."},
                "include_text": {"type": "boolean", "default": False, "description": "Also rewrite free-text history fields."},
                "ensure_alias": {"type": "boolean", "default": True, "description": "Create canonical and legacy alias rows."},
                "reason": {"type": "string", "default": "", "description": "Reason stored with the alias/update report."},
            },
        },
    },
    "normalize_mcp_intent": {
        "name": "normalize_mcp_intent",
        "description": (
            "Normalize an agent intent into a canonical MCP request form. "
            "Use this as the first stage when the agent is unsure which tool family or endpoint to use."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "intent": {"type": "string", "description": "Natural-language intent, task, or request"},
                "project_id": {"type": "string", "description": "Optional project identifier for project-aware routing"},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            },
        },
    },
    "project_workflow": {
        "name": "project_workflow",
        "description": (
            "Start a thematic project lifecycle workflow from a natural-language intent. "
            "Returns step-by-step guidance and a structured form for the agent to fill, "
            "instead of requiring the agent to choose several narrow lifecycle tools."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "intent": {"type": "string", "description": "Natural-language project lifecycle intent"},
                "workflow": {
                    "type": "string",
                    "enum": ["task_completion"],
                    "default": "task_completion",
                    "description": "Optional explicit workflow id. Defaults to the first supported project lifecycle workflow.",
                },
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string", "description": "Optional task id when already known"},
                "artifact_key": {"type": "string", "description": "Optional artifact key when already known"},
            },
        },
    },
    "project_workflow_submit": {
        "name": "project_workflow_submit",
        "description": (
            "Submit a filled project workflow form. "
            "The system routes the structured report to task changes, artifact lifecycle, "
            "linked improvements, and evidence records as appropriate."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workflow", "form"],
            "properties": {
                "workflow": {
                    "type": "string",
                    "enum": ["task_completion"],
                    "description": "Workflow identifier returned by project_workflow",
                },
                "form": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Filled form returned by project_workflow",
                },
                "acted_by": {"type": "string", "default": "user"},
                "source": {"type": "string", "default": "project_workflow"},
            },
        },
    },
    "reopen_task": {
        "name": "reopen_task",
        "description": (
            "Reopen an existing project task. "
            "Project is optional when task_id is globally unique; the server resolves it automatically when possible."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "description": "Task identifier"},
                "project": {
                    "type": "string",
                    "description": "Optional project name; omit to auto-resolve by task_id when possible",
                },
                "status": {
                    "type": "string",
                    "enum": ["planning", "active"],
                    "default": "active",
                    "description": "Target status for the task",
                },
                "reason": {"type": "string", "default": "reopen_task"},
                "acted_by": {"type": "string", "default": "user"},
                "source": {"type": "string", "default": "mcp"},
            },
        },
    },
    "enrich_task_with_context": {
        "name": "enrich_task_with_context",
        "description": (
            "Enrich a task description with relevant project context, including active project laws, "
            "indexed components, open improvements, active runtime hints, recent decision memoirs, "
            "effective documentation sections, operational instincts, and a short list of recommended next MCP calls. "
            "Call this at task start instead of manually reconstructing context."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "task"],
            "properties": {
                "project_id": {"type": "string"},
                "task": {"type": "string"},
                "max_components": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                "context_profile": {
                    "type": "string",
                    "enum": ["default", "handoff_compact", "hot_path"],
                    "default": "default",
                },
                "detail": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "Layer detail. For handoff_compact, compact returns immediate context plus available layer index; full returns complete context text.",
                },
                "model_context_window": {
                    "type": "integer",
                    "minimum": 1000,
                    "default": 32000,
                    "description": "Target main model context window used to compute enrichment token budget.",
                },
                "resume_budget_ratio": {
                    "type": "number",
                    "minimum": 0.001,
                    "maximum": 0.5,
                    "description": "Optional override for enrichment budget as a ratio of model_context_window.",
                },
                "resume_budget_profile": {
                    "type": "string",
                    "enum": ["normal", "complex", "handoff", "emergency"],
                    "default": "handoff",
                    "description": "Budget profile used when resume_budget_ratio is omitted.",
                },
            },
        },
    },
    "get_task_execution_context": {
        "name": "get_task_execution_context",
        "description": (
            "Return a compact state-aware execution packet for a task: recommended tool families/tools, "
            "required and recommended laws, risk controls, expected outputs, and allowed next transitions. "
            "Use this before acting in a specific task state instead of loading the full tool catalog or all rules."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task", "state"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string", "description": "Optional project task identifier for traceability."},
                "task": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": [
                        "planning",
                        "implementation",
                        "verification",
                        "live_validation",
                        "documentation",
                        "checkpointing",
                        "handoff",
                        "operator_review",
                    ],
                },
                "intent": {"type": "string"},
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "prior_stage_recorded": {
                    "type": "boolean",
                    "description": "Set true only when the previous task stage is already recorded in project memory.",
                },
                "stage_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Memory/checkpoint/law refs proving the previous stage is recorded.",
                },
                "include_tools": {"type": "boolean", "default": True},
                "include_rules": {"type": "boolean", "default": True},
                "max_required_rules": {"type": "integer", "minimum": 0, "maximum": 20, "default": 8},
                "max_recommended_rules": {"type": "integer", "minimum": 0, "maximum": 20, "default": 8},
            },
        },
    },
    "operational_tray": {
        "name": "operational_tray",
        "description": (
            "State-aware MCP facade for project work. Use action=inspect to get the current Operation Tray, "
            "or action=execute to run a small whitelisted tray action without loading the full MCP catalog."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task", "state", "action"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string"},
                "task": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": [
                        "planning",
                        "implementation",
                        "verification",
                        "live_validation",
                        "documentation",
                        "checkpointing",
                        "handoff",
                        "operator_review",
                    ],
                },
                "action": {"type": "string", "enum": ["inspect", "execute"], "default": "inspect"},
                "tray_action": {
                    "type": "string",
                    "enum": [
                        "record_stage_evidence",
                        "record_checkpoint",
                        "draft_checkpoint",
                        "review_rule_candidates",
                        "list_rule_candidates",
                    ],
                    "description": "Whitelisted high-level action. Required when action=execute.",
                },
                "tool": {
                    "type": "string",
                    "description": "Alias for tray_action, accepted for compact Operational Tray calls.",
                },
                "args": {"type": "object", "default": {}, "description": "Arguments for the whitelisted tray action."},
                "arguments": {
                    "type": "object",
                    "default": {},
                    "description": "Alias for args, accepted for compact Operational Tray calls.",
                },
                "intent": {"type": "string"},
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "stage_evidence": {"type": "array", "items": {"type": "string"}, "default": []},
                "prior_stage_recorded": {"type": "boolean"},
                "dry_run": {"type": "boolean", "default": False},
                "include_rules": {"type": "boolean", "default": True},
            },
        },
    },
    "upsert_knowledge_tree_node": {
        "name": "upsert_knowledge_tree_node",
        "description": (
            "Create or update a structured project knowledge-tree node by topic_path. "
            "Use this for architecture/documentation knowledge before editing Markdown projections."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["topic_path", "title"],
            "properties": {
                "topic_path": {"type": "string"},
                "title": {"type": "string"},
                "type": {"type": "string", "enum": ["idea", "project", "area", "task", "leaf"], "default": "area"},
                "status": {
                    "type": "string",
                    "enum": ["inbox", "planning", "active", "in-progress", "done", "paused", "archived"],
                    "default": "active",
                },
                "parent_topic_path": {"type": "string"},
                "description": {"type": "string"},
                "goal": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "doc": {"type": "string", "description": "Optional human-facing projection text; structured fields remain primary."},
                "responsibility": {"type": "string"},
                "source_of_truth": {"type": "string"},
                "runtime_entrypoints": {"type": "array", "items": {"type": "string"}, "default": []},
                "tests": {"type": "array", "items": {"type": "string"}, "default": []},
                "current_debt": {"type": "array", "items": {"type": "string"}, "default": []},
                "target_state": {"type": "string"},
                "projection_targets": {"type": "array", "items": {"type": "string"}, "default": []},
                "structured_fields": {"type": "object", "default": {}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}, "default": []},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "mcp_upsert_knowledge_tree_node"},
                "reason": {"type": "string"},
            },
        },
    },
    "get_project_readiness": {
        "name": "get_project_readiness",
        "description": (
            "Assess whether a project is ready for an external pilot workflow. "
            "Reports knowledge coverage, blockers, and next actions for bootstrap."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string"},
            },
        },
    },
    "get_project_bootstrap_checklist": {
        "name": "get_project_bootstrap_checklist",
        "description": (
            "Return an ordered bootstrap checklist for a project. "
            "Turns readiness findings into concrete operator steps for external-project setup."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string"},
            },
        },
    },
    "get_project_reconstruction_bundle": {
        "name": "get_project_reconstruction_bundle",
        "description": (
            "Assemble a read-only source-loss reconstruction bundle for any SuperMemory-backed project. "
            "Uses governed project memory layers such as components, tasks, improvements, laws, docs, memoirs, and runtime hints; "
            "does not require source code and does not mutate project state."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string"},
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
                "max_items_per_layer": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
            },
        },
    },
    "plan_remote_snapshot": {
        "name": "plan_remote_snapshot",
        "description": (
            "Validate and normalize a remote helper snapshot payload before ingest or refresh. "
            "Returns how the server will interpret the snapshot, storage mode, and rebuild policy "
            "without mutating project knowledge."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "snapshot"],
            "properties": {
                "project_id": {"type": "string"},
                "storage_mode": {
                    "type": "string",
                    "enum": ["knowledge_only", "selective_source_cache", "full_mirror"],
                    "default": "knowledge_only",
                },
                "snapshot": {
                    "type": "object",
                    "required": ["source_mode"],
                    "properties": {
                        "source_mode": {
                            "type": "string",
                            "enum": ["workspace", "git_snapshot", "github_pr", "archive_bundle"],
                        },
                        "repo": {"type": "string"},
                        "branch": {"type": "string"},
                        "commit_sha": {"type": "string"},
                        "base_commit_sha": {"type": "string"},
                        "dirty_workspace": {"type": "boolean"},
                        "snapshot_ts": {"type": "string"},
                        "diff_summary": {"type": "string"},
                        "pr_ref": {"type": "string"},
                    },
                },
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "deleted_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "renamed_files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["from_path", "to_path"],
                        "properties": {
                            "from_path": {"type": "string"},
                            "to_path": {"type": "string"},
                        },
                    },
                    "default": [],
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["path", "status"],
                        "properties": {
                            "path": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["added", "modified", "deleted", "renamed"],
                            },
                            "content": {"type": "string"},
                            "content_hash": {"type": "string"},
                            "language": {"type": "string"},
                            "component_hint": {"type": "string"},
                        },
                    },
                    "default": [],
                },
                "force": {"type": "boolean", "default": False},
            },
        },
    },
    "sync_remote_snapshot": {
        "name": "sync_remote_snapshot",
        "description": (
            "Run the helper-facing remote snapshot workflow for git-first autodocs. "
            "Returns one normalized action for the helper such as skipped, needs_source_payload, "
            "refreshed, bootstrap_needed, or no_changes."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "snapshot"],
            "properties": {
                "project_id": {"type": "string"},
                "storage_mode": {
                    "type": "string",
                    "enum": ["knowledge_only", "selective_source_cache", "full_mirror"],
                    "default": "knowledge_only",
                },
                "snapshot": {
                    "type": "object",
                    "required": ["source_mode"],
                    "properties": {
                        "source_mode": {
                            "type": "string",
                            "enum": ["workspace", "git_snapshot", "github_pr", "archive_bundle"],
                        },
                        "repo": {"type": "string"},
                        "branch": {"type": "string"},
                        "commit_sha": {"type": "string"},
                        "base_commit_sha": {"type": "string"},
                        "dirty_workspace": {"type": "boolean"},
                        "snapshot_ts": {"type": "string"},
                        "diff_summary": {"type": "string"},
                        "pr_ref": {"type": "string"},
                    },
                },
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "deleted_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "renamed_files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["from_path", "to_path"],
                        "properties": {
                            "from_path": {"type": "string"},
                            "to_path": {"type": "string"},
                        },
                    },
                    "default": [],
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["path", "status"],
                        "properties": {
                            "path": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["added", "modified", "deleted", "renamed"],
                            },
                            "content": {"type": "string"},
                            "content_hash": {"type": "string"},
                            "language": {"type": "string"},
                            "component_hint": {"type": "string"},
                        },
                    },
                    "default": [],
                },
                "force": {"type": "boolean", "default": False},
            },
        },
    },
    "get_storage_trust_status": {
        "name": "get_storage_trust_status",
        "description": (
            "Return a unified storage trust report that combines data integrity and data hygiene. "
            "Use this when you need one operator-facing view of storage health, cleanup pressure, and next actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "send_coordination_message": {
        "name": "send_coordination_message",
        "description": (
            "Send a project-scoped coordination message from one agent to another. "
            "Use for questions, action requests, replies, and status updates without turning them into project truth."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project", "from_agent", "to_agent", "content"],
            "properties": {
                "project": {"type": "string"},
                "from_agent": {"type": "string"},
                "to_agent": {"type": "string"},
                "content": {"type": "string"},
                "message_type": {
                    "type": "string",
                    "enum": ["question", "request_action", "response", "status_update", "handoff", "note"],
                    "default": "question",
                },
                "thread_id": {"type": "string"},
                "response_to_message_id": {"type": "string"},
                "requested_action": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                "source": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "pickup_coordination_messages": {
        "name": "pickup_coordination_messages",
        "description": (
            "Pick up new project-scoped coordination messages addressed to an agent. "
            "New messages become acknowledged when picked up."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string"},
                "project": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            },
        },
    },
    "list_coordination_messages": {
        "name": "list_coordination_messages",
        "description": (
            "List project-scoped coordination messages for an inbox, outbox, or thread. "
            "Use this to inspect a conversation thread or check whether an answer has arrived."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "project": {"type": "string"},
                "mailbox": {"type": "string", "enum": ["inbox", "outbox", "thread"], "default": "inbox"},
                "thread_id": {"type": "string"},
                "status": {"type": "string", "enum": ["new", "acknowledged", "in_progress", "answered", "closed"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    "update_coordination_message_status": {
        "name": "update_coordination_message_status",
        "description": (
            "Update the lifecycle status of a coordination message. "
            "Typical flow: new -> acknowledged -> in_progress -> answered/closed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["message_id", "status", "acted_by"],
            "properties": {
                "message_id": {"type": "string"},
                "status": {"type": "string", "enum": ["new", "acknowledged", "in_progress", "answered", "closed"]},
                "acted_by": {"type": "string"},
                "action_source": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    "set_canonical_status": {
        "name": "set_canonical_status",
        "description": "Suppress or reactivate a canonical memory for governance purposes.",
        "inputSchema": {
            "type": "object",
            "required": ["canonical_id", "suppressed"],
            "properties": {
                "canonical_id": {"type": "string"},
                "suppressed": {"type": "boolean"},
                "reason": {"type": "string"},
                "reviewed_by": {"type": "string"},
                "review_source": {"type": "string"},
            },
        },
    },
    "merge_canonicals": {
        "name": "merge_canonicals",
        "description": "Merge one canonical into another canonical of the same scope.",
        "inputSchema": {
            "type": "object",
            "required": ["source_id", "target_id"],
            "properties": {
                "source_id": {"type": "string"},
                "target_id": {"type": "string"},
                "reviewed_by": {"type": "string"},
                "review_source": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    "fix_layout_feedback": {
        "name": "fix_layout_feedback",
        "description": (
            "Confirm or reject a previous layout fix. "
            "Teaches the system - confirmed fixes become few-shot examples for future corrections. "
            "Use correction_id from fix_layout response."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["correction_id", "confirmed"],
            "properties": {
                "correction_id": {"type": "string", "description": "ID from fix_layout response"},
                "confirmed": {"type": "boolean", "description": "True if fix was correct, False if wrong"},
                "correct_text": {
                    "type": "string",
                    "description": "The actual correct text (if confirmed=False)",
                },
                "reviewed_by": {"type": "string"},
                "review_source": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
    "get_artifact": {
        "name": "get_artifact",
        "description": (
            "Get a unified artifact (improvement or task) by artifact_key. "
            "Artifact key format: {type}:{project}:{local_id} "
            "Example: improvement:mnemoforge:2e8fdc03-fc0b-4f77-bbaa-99f570e8894c "
            "Example: task:mnemoforge:6174ad7b-1fd9-4b6b-bb59-4f932b8cfc8c"
        ),
        "inputSchema": {
            "type": "object",
            "required": ["artifact_key"],
            "properties": {
                "artifact_key": {
                    "type": "string",
                    "description": "Artifact key in format: {type}:{project}:{local_id}",
                },
            },
        },
    },
    "list_artifacts": {
        "name": "list_artifacts",
        "description": (
            "List unified artifacts with optional filtering. "
            "Use this as the primary search surface for improvements and tasks together, "
            "because callers often do not know which entity type they need. "
            "Filter by status (open, done, paused, archived), type (improvement, task, or null for both), "
            "and created/updated time interval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "status": {
                    "type": "string",
                    "description": "Filter by status (open, done, paused, archived)",
                },
                "type": {
                    "type": "string",
                    "description": "Filter by type (improvement, task, or null for both)",
                },
                "created_after": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include artifacts with created_at >= this value",
                },
                "created_before": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include artifacts with created_at <= this value",
                },
                "updated_after": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include artifacts with updated_at >= this value",
                },
                "updated_before": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include artifacts with updated_at <= this value",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
        },
    },
    "reconcile_completed_checkpoints": {
        "name": "reconcile_completed_checkpoints",
        "description": (
            "Find open task/improvement artifacts whose task has a strict completed/done checkpoint. "
            "Defaults to report-only mode. Set close=true only after reviewing candidates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "close": {
                    "type": "boolean",
                    "default": False,
                    "description": "When false, report candidates only. When true, close eligible artifacts.",
                },
                "close_policy": {
                    "type": "string",
                    "enum": ["strict", "checkpoint_done"],
                    "default": "strict",
                    "description": "strict skips completed checkpoints that still have blockers or next_step.",
                },
                "acted_by": {"type": "string", "default": "codex"},
                "action_source": {"type": "string", "default": "mcp_reconcile_completed_checkpoints"},
                "reason": {
                    "type": "string",
                    "default": "Completed checkpoint reconciliation requested through MCP.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
        },
    },
    "review_completed_checkpoint_scope": {
        "name": "review_completed_checkpoint_scope",
        "description": (
            "Persist an operator review for a completed checkpoint's next_step scope. "
            "Use after reconcile_completed_checkpoints returns needs_next_step_scope."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_id", "next_step_scope"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string", "description": "Task identifier from the reconciliation candidate"},
                "checkpoint_change_id": {
                    "type": "string",
                    "description": "Optional checkpoint_change_id from the reconciliation candidate",
                },
                "next_step_scope": {
                    "type": "string",
                    "enum": ["none", "follow_up_task", "same_artifact_remaining_work", "operator_review"],
                    "description": "Reviewed meaning of the checkpoint next_step.",
                },
                "reason": {"type": "string", "default": "Review completed checkpoint next_step scope."},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "mcp_checkpoint_scope_review"},
            },
        },
    },
    "review_completed_checkpoint_scopes": {
        "name": "review_completed_checkpoint_scopes",
        "description": (
            "Persist multiple completed-checkpoint next_step scope reviews in one call. "
            "This records review annotations only; it never closes artifacts."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["decisions"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "decisions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "required": ["task_id", "next_step_scope"],
                        "properties": {
                            "task_id": {"type": "string"},
                            "checkpoint_change_id": {"type": "string"},
                            "next_step_scope": {
                                "type": "string",
                                "enum": ["none", "follow_up_task", "same_artifact_remaining_work", "operator_review"],
                            },
                            "reason": {"type": "string"},
                        },
                    },
                },
                "default_reason": {"type": "string", "default": "Batch review completed checkpoint next_step scopes."},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "mcp_checkpoint_scope_review_batch"},
            },
        },
    },
    "list_open_tasks": {
        "name": "list_open_tasks",
        "description": (
            "List open project tasks through the unified artifact surface. "
            "Use this when you want open work items and do not want to remember status/type filters. "
            "This is the preferred MCP surface for open-task inspection."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "created_after": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include tasks with created_at >= this value",
                },
                "created_before": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include tasks with created_at <= this value",
                },
                "updated_after": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include tasks with updated_at >= this value",
                },
                "updated_before": {
                    "type": "string",
                    "description": "ISO 8601 timestamp; include tasks with updated_at <= this value",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
        },
    },
    "list_tool_families": {
        "name": "list_tool_families",
        "description": (
            "List the compact top-level MCP tool families instead of the full flat catalog. "
            "Use this first when you need to choose a tool family without loading every tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_compatibility_note": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include a note that the flat catalog remains available for compatibility/debugging",
                },
            },
        },
    },
    "tool_family_tools": {
        "name": "tool_family_tools",
        "description": (
            "List the tools that belong to one tool family. "
            "Use this after list_tool_families when you need the concrete tool names and arguments for one area."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["family"],
            "properties": {
                "family": {"type": "string", "description": "Tool family slug"},
                "depth": {
                    "type": "string",
                    "enum": ["brief", "full"],
                    "default": "brief",
                    "description": "brief shows compact summaries; full includes input schemas",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
            },
        },
    },
    "tool_explain": {
        "name": "tool_explain",
        "description": (
            "Explain a specific tool in task context. "
            "Use this when you know the tool name but want the shortest useful explanation, required arguments, and common pitfalls."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["tool_name"],
            "properties": {
                "tool_name": {"type": "string", "description": "Tool name to explain"},
                "task_context": {
                    "type": "string",
                    "description": "Optional task context used to tailor the explanation",
                },
            },
        },
    },
    "tool_recommend": {
        "name": "tool_recommend",
        "description": (
            "Recommend the next MCP family and tools for a task. "
            "Use this when you want the system to narrow a large toolset to the few best next calls."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {"type": "string", "description": "Task or query to analyze"},
                "project_id": {
                    "type": "string",
                    "description": "Optional project id for project-aware recommendations",
                },
                "top_n": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            },
        },
    },
    "tool_feedback": {
        "name": "tool_feedback",
        "description": (
            "Record feedback after using a testing-stage MCP tool. "
            "Use this as the standardized evaluation envelope for a tool run: what was tested, what worked, "
            "what friction you hit, what should change, and whether the tool looks ready to promote or needs redesign."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["tool_name", "valence"],
            "properties": {
                "tool_name": {"type": "string", "description": "Name of the MCP tool you used"},
                "tool_stage": {
                    "type": "string",
                    "enum": ["testing", "stable", "deprecated"],
                    "default": "testing",
                    "description": "Stage of the tool when you used it",
                },
                "scope": {
                    "type": "string",
                    "description": "Short scope label for the run, for example 'route selection', 'task resume', or 'catalog discovery'",
                },
                "what_was_tested": {
                    "type": "string",
                    "description": "Short description of the behavior or path under evaluation",
                },
                "expected_behavior": {
                    "type": "string",
                    "description": "What the tool was expected to do",
                },
                "observed_behavior": {
                    "type": "string",
                    "description": "What the tool actually did",
                },
                "valence": {
                    "type": "string",
                    "enum": ["positive", "negative", "mixed"],
                    "description": "Overall usefulness of the tool run",
                },
                "worked": {
                    "type": "boolean",
                    "default": True,
                    "description": "Did the tool complete the requested work?",
                },
                "friction": {
                    "type": "string",
                    "description": "Short note about any friction, confusion, or missing affordance",
                },
                "missing_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Fields or parameters you wished the tool had exposed",
                },
                "suggestion": {
                    "type": "string",
                    "description": "Short improvement suggestion",
                },
                "next_action": {
                    "type": "string",
                    "description": "Short next step, such as retest, widen usage, or redesign",
                },
                "assessment": {
                    "type": "string",
                    "enum": ["promote_candidate", "keep_testing", "needs_redesign", "deprecate", "informational"],
                    "description": "Optional explicit evaluation outcome for the tool run",
                },
                "should_promote": {
                    "type": "boolean",
                    "description": "Optional judgment about promotion readiness",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Optional confidence in the assessment",
                },
                "task_context": {"type": "string", "description": "Short task context for the feedback"},
                "project_id": {"type": "string", "description": "Optional project identifier"},
                "agent_id": {"type": "string", "default": "mcp-agent"},
                "session_id": {"type": "string", "description": "Optional MCP session id"},
            },
        },
    },
    "report_task_checkpoint": {
        "name": "report_task_checkpoint",
        "description": (
            "Record a compact task checkpoint across planning, execution, blockers, interruptions, handoff, and completion. "
            "Use this to keep task progress recoverable across sessions and to leave a durable trail before the agent stops."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project", "task_id", "stage", "summary"],
            "properties": {
                "project": {"type": "string", "description": "Project name"},
                "task_id": {"type": "string", "description": "Task identifier"},
                "stage": {
                    "type": "string",
                    "enum": ["planning", "in_progress", "blocked", "interrupted", "handoff", "completed"],
                    "description": "Checkpoint stage in the task lifecycle",
                },
                "summary": {"type": "string", "description": "Short summary of what changed or what is currently happening"},
                "checkpoint_mode": {
                    "type": "string",
                    "enum": ["lightweight", "standard", "full"],
                    "default": "standard",
                    "description": "Use lightweight for stage-gate evidence, standard for normal progress, and full for handoff/closeout.",
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Short list of blockers or open issues",
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Important decisions made since the previous checkpoint",
                },
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Files or modules touched or owned by this task slice",
                },
                "verification": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Checks, tests, or validation evidence for the current state",
                },
                "remaining_risk": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Residual risks, unknowns, or follow-up concerns",
                },
                "next_step": {"type": "string", "description": "Compact next step or resume point"},
                "stage_evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Short refs from prior stage evidence, such as checkpoint:<change_id>.",
                },
                "next_step_scope": {
                    "type": "string",
                    "enum": ["none", "follow_up_task", "same_artifact_remaining_work", "operator_review", "unknown"],
                    "default": "unknown",
                    "description": "Whether next_step is separate follow-up work or remaining work on the same artifact.",
                },
                "status": {
                    "type": "string",
                    "enum": ["planning", "active", "paused", "done"],
                    "description": "Optional canonical task status for the checkpoint",
                },
                "reason": {"type": "string", "description": "Optional reason for this checkpoint"},
                "scope_confirmation": {
                    "type": "string",
                    "description": (
                        "Explicit human-reviewed override for checkpoint scope guard. "
                        "Use exactly 'current checkpoint belongs to this task' only when the checkpoint intentionally belongs to task_id."
                    ),
                },
                "acted_by": {"type": "string", "default": "user"},
                "source": {"type": "string", "default": "mcp"},
            },
        },
    },
    "record_work_result": {
        "name": "record_work_result",
        "description": (
            "High-level closeout facade for agent work. Use this when you want to save the current result "
            "without choosing between memory_store, task checkpoints, and artifact lifecycle tools. "
            "It always records a compact memory summary, tries to attach to a task when task_id/artifact_key is provided "
            "or a recent open task is available, and can optionally resolve the linked artifact."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge", "description": "Project name"},
                "summary": {"type": "string", "description": "Short result summary to persist"},
                "task_id": {"type": "string", "description": "Optional task identifier. If omitted, the facade tries the newest open task."},
                "artifact_key": {"type": "string", "description": "Optional unified artifact key, e.g. task:mnemoforge:<id> or improvement:mnemoforge:<id>."},
                "title": {"type": "string", "description": "Optional human title for the memory record"},
                "stage": {
                    "type": "string",
                    "enum": ["planning", "in_progress", "blocked", "interrupted", "handoff", "completed"],
                    "default": "completed",
                    "description": "Checkpoint stage when a task checkpoint is recorded",
                },
                "checkpoint_mode": {
                    "type": "string",
                    "enum": ["lightweight", "standard", "full"],
                    "default": "standard",
                },
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "verification": {"type": "array", "items": {"type": "string"}, "default": []},
                "decisions": {"type": "array", "items": {"type": "string"}, "default": []},
                "blockers": {"type": "array", "items": {"type": "string"}, "default": []},
                "remaining_risk": {"type": "array", "items": {"type": "string"}, "default": []},
                "next_step": {"type": "string", "description": "Remaining work or resume point"},
                "next_step_scope": {
                    "type": "string",
                    "enum": ["none", "follow_up_task", "same_artifact_remaining_work", "operator_review", "unknown"],
                    "default": "none",
                },
                "should_resolve_artifact": {
                    "type": "boolean",
                    "default": False,
                    "description": "Resolve the linked artifact after checkpointing. Defaults to false to avoid accidental closure.",
                },
                "use_clerk": {
                    "type": "boolean",
                    "default": True,
                    "description": "When stenographer spans are available, create a review-only clerk draft before direct checkpointing.",
                },
                "force_direct_checkpoint": {
                    "type": "boolean",
                    "default": False,
                    "description": "Bypass clerk draft creation and write the checkpoint directly even when stenographer spans exist.",
                },
                "create_issue_if_unmatched": {
                    "type": "boolean",
                    "default": False,
                    "description": "When no task/artifact can be matched, create a proposal improvement from title/summary instead of memory-only closeout.",
                },
                "agent_id": {"type": "string", "default": "codex"},
                "acted_by": {"type": "string", "default": "codex"},
                "source": {"type": "string", "default": "record_work_result"},
            },
        },
    },
    "project_work": {
        "name": "project_work",
        "description": (
            "Thematic project-work routing facade. Use this as the first-contact surface for common project work "
            "requests such as next priority, continue task, review capture drafts, close tail, save checkpoint, or route verification/restart work. "
            "It classifies intent, returns routing evidence, executes safe read-only routes, and plans guarded mutations unless allow_mutation=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["intent"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge", "description": "Project name"},
                "intent": {"type": "string", "description": "Natural-language project-work request or operator intent"},
                "task_id": {"type": "string", "description": "Optional task id when the request belongs to a known task"},
                "artifact_key": {"type": "string", "description": "Optional unified artifact key"},
                "state": {
                    "type": "string",
                    "enum": ["planning", "implementation", "verification", "live_validation", "documentation", "checkpointing", "handoff", "operator_review"],
                    "description": "Optional current task state used for routing evidence",
                },
                "summary": {"type": "string", "description": "Optional work summary for checkpoint/closeout routes"},
                "raw_notes": {"type": "string", "description": "Optional raw notes for capture/checkpoint routes"},
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "verification": {"type": "array", "items": {"type": "string"}, "default": []},
                "allow_mutation": {
                    "type": "boolean",
                    "default": False,
                    "description": "When false, mutating routes return a plan instead of writing memory or changing lifecycle state.",
                },
                "diagnostic": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return a compact plain-text route diagnostic block for local/weak MCP clients.",
                },
                "answer": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return a final-answer-shaped plain-text block for small local models.",
                },
                "response_format": {
                    "type": "string",
                    "enum": ["json", "diagnostic", "answer"],
                    "default": "json",
                    "description": "Use diagnostic for route/scorer/telemetry fields or answer for a short final-answer-shaped plain-text block instead of nested JSON.",
                },
                "scorer_backend": {
                    "type": "string",
                    "enum": ["lexical", "auto", "llm"],
                    "default": "auto",
                    "description": "Route scorer backend. auto keeps deterministic lexical strong matches and uses cheap LLM disambiguation when no explicit route is found; lexical forces deterministic-only routing.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": "Detail level for read-only route execution",
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                "acted_by": {"type": "string", "default": "codex"},
                "agent_id": {"type": "string", "default": "codex"},
            },
        },
    },
    "record_task_checkpoint": {
        "name": "record_task_checkpoint",
        "description": (
            "Preferred checkpoint tool. Record a compact task checkpoint through task_change storage, "
            "and create a resume-oriented handoff packet for blocked, interrupted, handoff, and completed phases. "
            "This is a thin MCP facade over existing task_change and handoff paths, not a separate checkpoint store."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["project", "task_id", "stage", "summary"],
            "properties": {
                "project": {"type": "string", "description": "Project name"},
                "task_id": {"type": "string", "description": "Task identifier"},
                "stage": {
                    "type": "string",
                    "enum": ["planning", "in_progress", "blocked", "interrupted", "handoff", "completed"],
                    "description": "Checkpoint stage in the task lifecycle",
                },
                "summary": {"type": "string", "description": "Short summary of what changed or what is currently happening"},
                "checkpoint_mode": {
                    "type": "string",
                    "enum": ["lightweight", "standard", "full"],
                    "default": "standard",
                    "description": "Use lightweight for stage-gate evidence, standard for normal progress, and full for handoff/closeout.",
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Short list of blockers or open issues",
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Important decisions made since the previous checkpoint",
                },
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Files or modules touched or owned by this task slice",
                },
                "verification": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Checks, tests, or validation evidence for the current state",
                },
                "remaining_risk": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Residual risks, unknowns, or follow-up concerns",
                },
                "next_step": {"type": "string", "description": "Compact next step or resume point"},
                "stage_evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Short refs from prior stage evidence, such as checkpoint:<change_id>.",
                },
                "next_step_scope": {
                    "type": "string",
                    "enum": ["none", "follow_up_task", "same_artifact_remaining_work", "operator_review", "unknown"],
                    "default": "unknown",
                    "description": "Whether next_step is separate follow-up work or remaining work on the same artifact.",
                },
                "status": {
                    "type": "string",
                    "enum": ["planning", "active", "paused", "done"],
                    "description": "Optional canonical task status for the checkpoint",
                },
                "reason": {"type": "string", "description": "Optional reason for this checkpoint"},
                "scope_confirmation": {
                    "type": "string",
                    "description": (
                        "Explicit human-reviewed override for checkpoint scope guard. "
                        "Use exactly 'current checkpoint belongs to this task' only when the checkpoint intentionally belongs to task_id."
                    ),
                },
                "acted_by": {"type": "string", "default": "user"},
                "source": {"type": "string", "default": "mcp"},
                "to_agent": {
                    "type": "string",
                    "description": "Optional receiving/resuming agent for resume-relevant handoff packets",
                },
                "handoff_label": {
                    "type": "string",
                    "description": "Optional human-readable label for the generated resume packet",
                },
                "write_scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Bounded files/modules/areas owned by this checkpoint",
                },
            },
        },
    },
    "draft_task_checkpoint": {
        "name": "draft_task_checkpoint",
        "description": (
            "Use a low-cost memory scribe to turn raw work notes into reviewable record_task_checkpoint arguments. "
            "This tool does not mutate project memory; it returns a draft, validation report, and the exact next tool payload."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["raw_notes"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string", "description": "Task identifier when known"},
                "task_title": {"type": "string", "description": "Optional task title for fallback summaries"},
                "stage": {
                    "type": "string",
                    "enum": ["planning", "in_progress", "blocked", "interrupted", "handoff", "completed"],
                    "default": "in_progress",
                },
                "status": {
                    "type": "string",
                    "enum": ["planning", "active", "paused", "done"],
                    "default": "active",
                },
                "raw_notes": {
                    "type": "string",
                    "description": "Raw execution notes, changed files, tests, risks, blockers, and next step.",
                },
                "reason": {"type": "string", "default": "draft_task_checkpoint"},
                "acted_by": {"type": "string", "default": "codex"},
                "use_llm": {
                    "type": "boolean",
                    "default": True,
                    "description": "Use cheap local/cloud LLM extraction when available; deterministic fallback remains available.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["economy", "strict_economy", "balanced"],
                    "default": "economy",
                },
                "model_context_window": {"type": "integer", "minimum": 1000, "default": 32000},
                "resume_budget_ratio": {"type": "number", "minimum": 0.001, "maximum": 0.5},
                "resume_budget_profile": {
                    "type": "string",
                    "enum": ["normal", "complex", "handoff", "emergency"],
                    "default": "normal",
                },
            },
        },
    },
    "clerk_draft_report": {
        "name": "clerk_draft_report",
        "description": (
            "First-class clerk/scribe closeout surface. Use this before writing governed memory when a work session "
            "has stenographer spans or when you have raw agent notes. It returns a review-only checkpoint/report draft "
            "and the next approve/revise tool; it does not mutate project memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string", "description": "Task identifier when known"},
                "work_id": {"type": "string", "description": "Preferred when drafting from stenographer spans"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "task_title": {"type": "string", "description": "Optional task title for fallback summaries"},
                "stage": {
                    "type": "string",
                    "enum": ["planning", "in_progress", "blocked", "interrupted", "handoff", "completed"],
                    "default": "completed",
                },
                "status": {
                    "type": "string",
                    "enum": ["planning", "active", "paused", "done"],
                    "default": "done",
                },
                "raw_notes": {
                    "type": "string",
                    "description": "Optional raw notes. If omitted, the clerk drafts from stenographer spans.",
                },
                "reason": {"type": "string", "default": "clerk_draft_report"},
                "use_llm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Use cheap local/cloud LLM extraction when available; deterministic fallback remains available.",
                },
                "preserve_evidence": {
                    "type": "boolean",
                    "default": True,
                    "description": "When drafting from spans, preserve bounded source evidence alongside the draft.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["compact", "preserve_evidence", "no_compression", "project_overview", "economy", "strict_economy", "balanced"],
                    "default": "compact",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
        },
    },
    "get_work_session_state": {
        "name": "get_work_session_state",
        "description": "Return the stenographer/work-session protocol state for the current agent session and the next valid tools.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string", "description": "Agent session id; defaults to agent_id when omitted"},
            },
        },
    },
    "start_work_session": {
        "name": "start_work_session",
        "description": "Start a guarded stenographer work session. Fails if this agent session already has active work.",
        "inputSchema": {
            "type": "object",
            "required": ["project", "task_id"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "role": {"type": "string", "default": "worker"},
                "work_id": {"type": "string", "description": "Optional caller-provided work id; server generates one when omitted"},
                "parent_work_id": {"type": "string"},
                "parent_task_id": {"type": "string"},
                "spawn_reason": {"type": "string"},
                "return_condition": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}, "default": []},
                "summary": {"type": "string"},
            },
        },
    },
    "park_work_session": {
        "name": "park_work_session",
        "description": "Park the active work session before starting focused child work.",
        "inputSchema": {
            "type": "object",
            "required": ["work_id", "reason"],
            "properties": {
                "work_id": {"type": "string"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "reason": {"type": "string"},
                "child_task_id": {"type": "string"},
                "child_work_id": {"type": "string"},
            },
        },
    },
    "resume_work_session": {
        "name": "resume_work_session",
        "description": "Resume a parked parent work session after child work ends or is handed off.",
        "inputSchema": {
            "type": "object",
            "required": ["work_id"],
            "properties": {
                "work_id": {"type": "string"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "child_work_id": {"type": "string"},
                "result": {"type": "string"},
            },
        },
    },
    "end_work_session": {
        "name": "end_work_session",
        "description": "End the active work session. work_id and task_id must match the active state. status=completed requires explicit closeout spans: verification, changed_files, and next_step.",
        "inputSchema": {
            "type": "object",
            "required": ["work_id", "task_id", "status"],
            "properties": {
                "work_id": {"type": "string"},
                "task_id": {"type": "string"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "status": {"type": "string", "enum": ["completed", "blocked", "failed", "interrupted", "cancelled"]},
                "result": {"type": "string"},
            },
        },
    },
    "record_stenographer_span": {
        "name": "record_stenographer_span",
        "description": "Record a bounded review-only stenographer evidence span into the active work session. This never writes canonical memory.",
        "inputSchema": {
            "type": "object",
            "required": ["kind", "content"],
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string"},
                "work_id": {"type": "string", "description": "Optional integrity check; must match active work when provided"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["fact", "decision", "verification", "risk", "blocker", "next_step", "checkpoint_hint", "handoff_hint", "diagnostic", "changed_files", "rule_project_candidate", "rule_canonical_candidate", "rule_revision_hint", "rule_merge_hint"],
                },
                "source": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    },
    "list_stenographer_spans": {
        "name": "list_stenographer_spans",
        "description": "List review-only stenographer evidence spans for a task, work session, or agent session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task_id": {"type": "string"},
                "work_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
        },
    },
    "draft_checkpoint_from_spans": {
        "name": "draft_checkpoint_from_spans",
        "description": (
            "Build an immutable, review-only checkpoint draft from stenographer spans. "
            "Returns draft_id/version for approve-by-reference; does not write canonical memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge"},
                "task_id": {"type": "string"},
                "work_id": {"type": "string"},
                "agent_id": {"type": "string", "default": "codex"},
                "session_id": {"type": "string"},
                "stage": {"type": "string", "default": "in_progress"},
                "status": {"type": "string", "default": "active"},
                "reason": {"type": "string", "default": "draft_checkpoint_from_spans"},
                "use_llm": {"type": "boolean", "default": False},
                "preserve_evidence": {
                    "type": "boolean",
                    "default": False,
                    "description": "Persist a bounded original span snapshot alongside the compact draft for full-view review.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["compact", "preserve_evidence", "no_compression", "project_overview"],
                    "default": "compact",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
        },
    },
    "get_checkpoint_draft": {
        "name": "get_checkpoint_draft",
        "description": "Fetch a checkpoint draft by reference. Use view=preview for low-token review before approval.",
        "inputSchema": {
            "type": "object",
            "required": ["draft_id"],
            "properties": {
                "draft_id": {"type": "string"},
                "version": {"type": "integer"},
                "view": {"type": "string", "enum": ["preview", "full"], "default": "preview"},
            },
        },
    },
    "revise_checkpoint_draft": {
        "name": "revise_checkpoint_draft",
        "description": "Apply a bounded patch to a checkpoint draft, creating a new immutable version.",
        "inputSchema": {
            "type": "object",
            "required": ["draft_id", "patch"],
            "properties": {
                "draft_id": {"type": "string"},
                "patch": {
                    "type": "object",
                    "description": "Allowed fields: summary, blockers, decisions, changed_files, verification, remaining_risk, next_step, stage, status, reason",
                },
                "revised_by": {"type": "string", "default": "codex"},
            },
        },
    },
    "approve_checkpoint_draft": {
        "name": "approve_checkpoint_draft",
        "description": "Approve an immutable checkpoint draft version by reference and save it canonically server-side.",
        "inputSchema": {
            "type": "object",
            "required": ["draft_id", "version"],
            "properties": {
                "draft_id": {"type": "string"},
                "version": {"type": "integer"},
                "approved_by": {"type": "string", "default": "codex"},
            },
        },
    },
    "reject_checkpoint_draft": {
        "name": "reject_checkpoint_draft",
        "description": "Reject the latest checkpoint draft version so it cannot be approved later.",
        "inputSchema": {
            "type": "object",
            "required": ["draft_id", "version"],
            "properties": {
                "draft_id": {"type": "string"},
                "version": {"type": "integer"},
                "rejected_by": {"type": "string", "default": "codex"},
                "reason": {"type": "string"},
            },
        },
    },
    "continue_task": {
        "name": "continue_task",
        "description": (
            "Resume a task from MnemoForge using MCP-accessible state. "
            "Returns a compact layered resume response by default: latest checkpoint, replay/execution status, replay drill decision, available layer index, token-overhead estimate, and the next safe action. "
            "Use detail=full or include_replay_bundle=true to fetch full task history, linked improvement, handoff refs, and context refs. "
            "Use this when the user says to continue a task or when an old agent session is unavailable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": "mnemoforge", "description": "Project name"},
                "task_id": {"type": "string", "description": "Optional task identifier. If omitted, uses the newest open task."},
                "agent_id": {"type": "string", "default": "codex", "description": "Agent identity for handoff lookup"},
                "include_handoffs": {"type": "boolean", "default": True, "description": "Include active/pending handoff packets for this task"},
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact", "description": "Response detail layer. compact omits full replay_bundle and exposes available_layers; full includes detailed replay state."},
                "include_replay_bundle": {"type": "boolean", "default": False, "description": "Force inclusion of replay_bundle even when detail is compact."},
                "model_context_window": {"type": "integer", "minimum": 1000, "default": 32000, "description": "Target main model context window used to compute resume token budget."},
                "resume_budget_ratio": {"type": "number", "minimum": 0.001, "maximum": 0.5, "description": "Optional override for resume budget as a ratio of model_context_window."},
                "resume_budget_profile": {"type": "string", "enum": ["normal", "complex", "handoff", "emergency"], "default": "normal", "description": "Budget profile used when resume_budget_ratio is omitted."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10, "description": "Maximum tasks/handoffs to inspect when task_id is omitted"},
            },
        },
    },
    "resolve_artifact": {
        "name": "resolve_artifact",
        "description": (
            "Resolve a unified artifact (improvement→resolved, task→done). "
            "Automatically syncs status with linked artifact if exists."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["artifact_key"],
            "properties": {
                "artifact_key": {
                    "type": "string",
                    "description": "Artifact key in format: {type}:{project}:{local_id}",
                },
                "acted_by": {"type": "string", "default": "user"},
                "action_source": {"type": "string", "default": "inline_user_approval"},
                "reason": {"type": "string", "default": ""},
            },
        },
    },
    "reopen_artifact": {
        "name": "reopen_artifact",
        "description": (
            "Reopen a unified artifact (improvement→open, task→active). "
            "Automatically syncs status with linked artifact if exists."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["artifact_key", "project"],
            "properties": {
                "artifact_key": {
                    "type": "string",
                    "description": "Artifact key in format: {type}:{project}:{local_id}",
                },
                "project": {"type": "string", "description": "Project name"},
                "status": {"type": "string", "default": "active"},
                "reason": {"type": "string", "default": "reopen_artifact"},
                "acted_by": {"type": "string", "default": "user"},
                "source": {"type": "string", "default": "unified-artifact"},
            },
        },
    },
}


def tool_definition(name: str) -> dict[str, Any]:
    return deepcopy(_SHARED_TOOL_DEFINITIONS[name])


def list_shared_tool_names() -> list[str]:
    return sorted(_SHARED_TOOL_DEFINITIONS)


def list_shared_tool_definitions() -> list[dict[str, Any]]:
    return [deepcopy(_SHARED_TOOL_DEFINITIONS[name]) for name in list_shared_tool_names()]


def sync_tool_definitions(tools: list[dict[str, Any]], *names: str) -> None:
    index_by_name = {tool.get("name"): idx for idx, tool in enumerate(tools)}
    for name in names:
        idx = index_by_name.get(name)
        if idx is not None:
            tools[idx] = tool_definition(name)


def build_enrich_task_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "project_id": args["project_id"],
        "task": args["task"],
        "max_components": args.get("max_components", 3),
    }
    context_profile = str(args.get("context_profile") or "").strip()
    if context_profile:
        payload["context_profile"] = context_profile
    detail = str(args.get("detail") or "").strip()
    if detail:
        payload["detail"] = detail
    for key in ("model_context_window", "resume_budget_ratio", "resume_budget_profile"):
        if args.get(key) not in (None, ""):
            payload[key] = args.get(key)
    return payload


def build_task_execution_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "project": args.get("project", "mnemoforge"),
        "task_id": args.get("task_id", ""),
        "task": args["task"],
        "state": args["state"],
        "intent": args.get("intent", ""),
        "changed_files": args.get("changed_files", []),
        "stage_evidence": args.get("stage_evidence", []),
        "include_tools": bool(args.get("include_tools", True)),
        "include_rules": bool(args.get("include_rules", True)),
    }
    if args.get("prior_stage_recorded") is not None:
        payload["prior_stage_recorded"] = bool(args.get("prior_stage_recorded"))
    for key in ("max_required_rules", "max_recommended_rules"):
        if args.get(key) not in (None, ""):
            payload[key] = args.get(key)
    return payload


def build_operational_tray_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = build_task_execution_context_payload(
        {
            "project": args.get("project", "mnemoforge"),
            "task_id": args.get("task_id", ""),
            "task": args["task"],
            "state": args["state"],
            "intent": args.get("intent", ""),
            "changed_files": args.get("changed_files", []),
            "stage_evidence": args.get("stage_evidence", []),
            "include_tools": True,
            "include_rules": bool(args.get("include_rules", True)),
        }
    )
    if args.get("prior_stage_recorded") is not None:
        payload["prior_stage_recorded"] = bool(args.get("prior_stage_recorded"))
    return payload


def build_upsert_knowledge_tree_node_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "topic_path": str(args["topic_path"]).strip(),
        "title": str(args["title"]).strip(),
        "type": str(args.get("type") or "area").strip(),
        "status": str(args.get("status") or "active").strip(),
        "parent_topic_path": str(args.get("parent_topic_path") or "").strip(),
        "description": str(args.get("description") or "").strip(),
        "goal": str(args.get("goal") or "").strip(),
        "tags": args.get("tags") or [],
        "doc": str(args.get("doc") or "").strip(),
        "responsibility": str(args.get("responsibility") or "").strip(),
        "source_of_truth": str(args.get("source_of_truth") or "").strip(),
        "runtime_entrypoints": args.get("runtime_entrypoints") or [],
        "tests": args.get("tests") or [],
        "current_debt": args.get("current_debt") or [],
        "target_state": str(args.get("target_state") or "").strip(),
        "projection_targets": args.get("projection_targets") or [],
        "structured_fields": args.get("structured_fields") or {},
        "evidence_refs": args.get("evidence_refs") or [],
        "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
        "source": str(args.get("source") or "mcp_upsert_knowledge_tree_node").strip()
        or "mcp_upsert_knowledge_tree_node",
        "reason": str(args.get("reason") or "").strip(),
    }
    return payload


def build_project_readiness_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {"project_id": args["project_id"]}


def build_project_bootstrap_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {"project_id": args["project_id"]}


def build_project_reconstruction_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": args["project_id"],
        "detail": str(args.get("detail") or "compact").strip() or "compact",
        "max_items_per_layer": max(1, min(50, int(args.get("max_items_per_layer") or 5))),
    }


def build_remote_snapshot_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": args["project_id"],
        "storage_mode": args.get("storage_mode", "knowledge_only"),
        "snapshot": args["snapshot"],
        "changed_files": args.get("changed_files", []),
        "deleted_files": args.get("deleted_files", []),
        "renamed_files": args.get("renamed_files", []),
        "files": args.get("files", []),
        "force": bool(args.get("force", False)),
    }


def build_send_coordination_message_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": args["project"],
        "from_agent": args["from_agent"],
        "to_agent": args["to_agent"],
        "content": args["content"],
        "message_type": args.get("message_type", "question"),
        "thread_id": args.get("thread_id"),
        "response_to_message_id": args.get("response_to_message_id"),
        "requested_action": args.get("requested_action"),
        "priority": args.get("priority", "normal"),
        "source": args.get("source", "mcp_coordination"),
        "tags": args.get("tags", []),
    }


def build_pickup_coordination_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {"agent_id": args["agent_id"], "limit": int(args.get("limit", 10))}
    if args.get("project"):
        payload["project"] = args["project"]
    return payload


def build_list_coordination_query(args: dict[str, Any]) -> str:
    params: list[str] = [f"mailbox={args.get('mailbox', 'inbox')}", f"limit={int(args.get('limit', 20))}"]
    if args.get("agent_id"):
        params.append(f"agent_id={args['agent_id']}")
    if args.get("project"):
        params.append(f"project={args['project']}")
    if args.get("thread_id"):
        params.append(f"thread_id={args['thread_id']}")
    if args.get("status"):
        params.append(f"status={args['status']}")
    return "&".join(params)


def build_coordination_status_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": args["status"],
        "acted_by": args["acted_by"],
        "action_source": args.get("action_source", "mcp_coordination"),
        "reason": args.get("reason", ""),
    }


def build_list_learning_candidates_query(args: dict[str, Any]) -> str:
    params: list[str] = [
        "scope=candidate",
        "status=pending_review",
        f"limit={int(args.get('limit', 10))}",
    ]
    if args.get("artifact_type"):
        params.append(f"artifact_type={args['artifact_type']}")
    if args.get("agent_id"):
        params.append(f"agent_id={args['agent_id']}")
    return "&".join(params)


def build_list_artifacts_query(args: dict[str, Any]) -> str:
    params: list[str] = [
        f"project={quote(str(args.get('project', 'mnemoforge')), safe='')}",
        f"limit={int(args.get('limit', 50))}",
    ]
    if args.get("status"):
        params.append(f"artifact_status={quote(str(args['status']), safe='')}")
    if args.get("type"):
        params.append(f"type={quote(str(args['type']), safe='')}")
    if args.get("created_after"):
        params.append(f"created_after={quote(str(args['created_after']), safe='')}")
    if args.get("created_before"):
        params.append(f"created_before={quote(str(args['created_before']), safe='')}")
    if args.get("updated_after"):
        params.append(f"updated_after={quote(str(args['updated_after']), safe='')}")
    if args.get("updated_before"):
        params.append(f"updated_before={quote(str(args['updated_before']), safe='')}")
    return "&".join(params)


def build_reconcile_completed_checkpoints_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": str(args.get("project") or "mnemoforge").strip() or "mnemoforge",
        "close": bool(args.get("close", False)),
        "close_policy": str(args.get("close_policy") or "strict").strip() or "strict",
        "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
        "action_source": str(args.get("action_source") or "mcp_reconcile_completed_checkpoints").strip()
        or "mcp_reconcile_completed_checkpoints",
        "reason": str(args.get("reason") or "Completed checkpoint reconciliation requested through MCP.").strip(),
        "limit": int(args.get("limit") or 100),
    }


def build_review_completed_checkpoint_scope_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": str(args.get("project") or "mnemoforge").strip() or "mnemoforge",
        "task_id": str(args["task_id"]).strip(),
        "checkpoint_change_id": str(args.get("checkpoint_change_id") or "").strip(),
        "next_step_scope": str(args["next_step_scope"]).strip(),
        "reason": str(args.get("reason") or "Review completed checkpoint next_step scope.").strip(),
        "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
        "source": str(args.get("source") or "mcp_checkpoint_scope_review").strip() or "mcp_checkpoint_scope_review",
    }


def build_review_completed_checkpoint_scopes_payload(args: dict[str, Any]) -> dict[str, Any]:
    decisions = args.get("decisions") or []
    if not isinstance(decisions, list):
        decisions = []
    normalized: list[dict[str, str]] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "task_id": str(item.get("task_id") or "").strip(),
                "checkpoint_change_id": str(item.get("checkpoint_change_id") or "").strip(),
                "next_step_scope": str(item.get("next_step_scope") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return {
        "project": str(args.get("project") or "mnemoforge").strip() or "mnemoforge",
        "decisions": normalized,
        "default_reason": str(args.get("default_reason") or "Batch review completed checkpoint next_step scopes.").strip(),
        "acted_by": str(args.get("acted_by") or "codex").strip() or "codex",
        "source": str(args.get("source") or "mcp_checkpoint_scope_review_batch").strip() or "mcp_checkpoint_scope_review_batch",
    }


def build_list_open_tasks_query(args: dict[str, Any]) -> str:
    params: list[str] = [
        f"project={quote(str(args.get('project', 'mnemoforge')), safe='')}",
        "status=open",
        "type=task",
        f"limit={int(args.get('limit', 50))}",
    ]
    if args.get("created_after"):
        params.append(f"created_after={quote(str(args['created_after']), safe='')}")
    if args.get("created_before"):
        params.append(f"created_before={quote(str(args['created_before']), safe='')}")
    if args.get("updated_after"):
        params.append(f"updated_after={quote(str(args['updated_after']), safe='')}")
    if args.get("updated_before"):
        params.append(f"updated_before={quote(str(args['updated_before']), safe='')}")
    return "&".join(params)


def format_list_learning_candidates_response(data: dict[str, Any]) -> str:
    items = data.get("artifacts", [])
    if not items:
        return "No pending learning candidates."
    lines = []
    for i, item in enumerate(items, 1):
        meta = item.get("meta") or {}
        signal_type = meta.get("signal_type") or item.get("action_type") or item.get("artifact_type") or "candidate"
        lines.append(
            f"{i}. [{signal_type}] {item.get('content','')[:180]}\n"
            f"   id={item.get('id')} evidence={item.get('evidence_count', 0)} "
            f"confidence={item.get('confidence', 0):.2f} risk={item.get('risk_level', 'low')}"
        )
    return "Pending learning candidates:\n\n" + "\n\n".join(lines)


def format_list_open_tasks_response(data: dict[str, Any]) -> str:
    items = data.get("items", [])
    if not items:
        return "No open tasks found."
    lines = [f"Open tasks: {len(items)}"]
    for i, item in enumerate(items, 1):
        linked = item.get("linked_artifact_key") or item.get("linked_status") or ""
        suffix = f" linked={linked}" if linked else ""
        lines.append(f"{i}. [{item.get('status', 'open')}] {item.get('title', '')} ({item.get('artifact_key')}){suffix}")
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"   {description[:240]}")
    return "\n".join(lines)


def format_list_tool_families_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_tool_family_tools_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_tool_explain_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_tool_recommend_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_tool_feedback_response(data: dict[str, Any]) -> str:
    payload = dict(data)
    payload.setdefault("summary", f"Recorded tool feedback for {payload.get('tool_name', '?')}")
    return json.dumps(payload, indent=2, ensure_ascii=False)


def build_report_task_checkpoint_payload(args: dict[str, Any]) -> dict[str, Any]:
    from app.services.project_tasks_content import build_task_checkpoint_content

    def _string_list(name: str) -> list[str]:
        value = args.get(name) or []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item).strip()]

    stage = str(args["stage"]).strip().lower()
    status = str(args.get("status") or "").strip().lower()
    if not status:
        status = {
            "planning": "planning",
            "in_progress": "active",
            "blocked": "paused",
            "interrupted": "paused",
            "handoff": "paused",
            "completed": "done",
        }.get(stage, "active")

    clean_blockers = _string_list("blockers")
    clean_decisions = _string_list("decisions")
    clean_changed_files = _string_list("changed_files")
    clean_verification = _string_list("verification")
    clean_remaining_risk = _string_list("remaining_risk")
    clean_stage_evidence_refs = _string_list("stage_evidence_refs")
    checkpoint_mode = str(args.get("checkpoint_mode") or "standard").strip().lower()
    if checkpoint_mode not in {"lightweight", "standard", "full"}:
        checkpoint_mode = "standard"
    if checkpoint_mode == "lightweight":
        clean_blockers = clean_blockers[:3]
        clean_decisions = clean_decisions[:3]
        clean_changed_files = clean_changed_files[:8]
        clean_verification = clean_verification[:3]
        clean_remaining_risk = clean_remaining_risk[:3]
    return {
        "project": args["project"],
        "change_type": "note",
        "content": build_task_checkpoint_content(
            stage=stage,
            status=status,
            summary=str(args["summary"]).strip(),
            blockers=clean_blockers,
            decisions=clean_decisions,
            changed_files=clean_changed_files,
            verification=clean_verification,
            remaining_risk=clean_remaining_risk,
            next_step=str(args.get("next_step") or "").strip(),
            next_step_scope=str(args.get("next_step_scope") or "").strip(),
            stage_evidence_refs=clean_stage_evidence_refs,
            reason=str(args.get("reason") or "").strip(),
        ),
        "why": str(args.get("reason") or "").strip() or f"Task checkpoint recorded at stage={stage}.",
        "agent_id": str(args.get("acted_by") or "user").strip() or "user",
        "source": str(args.get("source") or "mcp").strip() or "mcp",
        "tags": ["task_checkpoint", f"task_stage:{stage}", f"task_status:{status}", f"checkpoint_mode:{checkpoint_mode}"],
    }


def build_reopen_task_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "status": args.get("status", "active"),
        "reason": args.get("reason", "reopen_task"),
        "acted_by": args.get("acted_by", "user"),
        "source": args.get("source", "mcp"),
    }
    project = str(args.get("project") or "").strip()
    if project:
        payload["project"] = project
    return payload


def build_normalize_mcp_intent_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent": args["intent"],
        "project_id": args.get("project_id", ""),
        "top_n": int(args.get("top_n", 3)),
    }


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def build_project_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    intent = str(args["intent"]).strip()
    project = str(args.get("project") or args.get("project_id") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(args.get("task_id") or "").strip()
    artifact_key = str(args.get("artifact_key") or "").strip()
    workflow = str(args.get("workflow") or "task_completion").strip() or "task_completion"
    if not artifact_key and task_id:
        artifact_key = f"task:{project}:{task_id}"
    return {
        "workflow": workflow,
        "project": project,
        "intent": intent,
        "status": "form_required",
        "steps": [
            "Fill completion_report with the task id, summary, changed files, verification, and residual risks.",
            "Set verdict to completed, partial, or blocked.",
            "Set should_resolve_artifact=true only when the implementation is complete and verified.",
            "Submit the filled form with project_workflow_submit.",
        ],
        "form": {
            "project": project,
            "task_id": task_id,
            "artifact_key": artifact_key,
            "completion_summary": "",
            "changed_files": [],
            "tests_run": [],
            "test_result": "passed|failed|not_run",
            "verdict": "completed|partial|blocked",
            "residual_risks": [],
            "should_resolve_artifact": True,
        },
        "submit_tool": "project_workflow_submit",
    }


def build_project_workflow_submit_payload(args: dict[str, Any]) -> dict[str, Any]:
    form = dict(args.get("form") or {})
    workflow = str(args.get("workflow") or form.get("workflow") or "").strip()
    project = str(form.get("project") or args.get("project") or "mnemoforge").strip() or "mnemoforge"
    task_id = str(form.get("task_id") or "").strip()
    artifact_key = str(form.get("artifact_key") or "").strip()
    if not task_id and artifact_key.startswith("task:"):
        task_id = artifact_key.split(":", 2)[-1]
    if not artifact_key and task_id:
        artifact_key = f"task:{project}:{task_id}"
    verdict = str(form.get("verdict") or "completed").strip().lower()
    should_resolve = bool(form.get("should_resolve_artifact", verdict in {"completed", "effective", "done"}))
    return {
        "workflow": workflow,
        "project": project,
        "task_id": task_id,
        "artifact_key": artifact_key,
        "completion_summary": str(form.get("completion_summary") or form.get("summary") or "").strip(),
        "changed_files": _normalize_string_list(form.get("changed_files")),
        "tests_run": _normalize_string_list(form.get("tests_run")),
        "test_result": str(form.get("test_result") or "").strip(),
        "verdict": verdict,
        "residual_risks": _normalize_string_list(form.get("residual_risks")),
        "should_resolve_artifact": should_resolve,
        "acted_by": str(args.get("acted_by") or form.get("acted_by") or "user").strip() or "user",
        "source": str(args.get("source") or form.get("source") or "project_workflow").strip() or "project_workflow",
    }


def build_project_workflow_submit_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if payload["workflow"] != "task_completion":
        return {
            "status": "unsupported_workflow",
            "workflow": payload["workflow"],
            "supported_workflows": ["task_completion"],
            "operations": [],
        }
    missing_fields = [
        field
        for field in ("task_id", "completion_summary")
        if not str(payload.get(field) or "").strip()
    ]
    if missing_fields:
        return {
            "status": "incomplete_form",
            "missing_fields": missing_fields,
            "message": "Task completion workflow requires task_id and completion_summary.",
            "operations": [],
        }

    evidence_sections = [
        ("summary", [payload["completion_summary"]]),
        ("changed_files", payload["changed_files"]),
        ("tests_run", payload["tests_run"]),
        ("test_result", [payload["test_result"]] if payload["test_result"] else []),
        ("residual_risks", payload["residual_risks"]),
    ]
    evidence_lines = [
        {
            "summary": lambda values: values[0],
            "changed_files": lambda values: "Changed files: " + ", ".join(values),
            "tests_run": lambda values: "Tests run: " + ", ".join(values),
            "test_result": lambda values: f"Test result: {values[0]}",
            "residual_risks": lambda values: "Residual risks: " + "; ".join(values),
        }[name](values)
        for name, values in evidence_sections
        if values
    ]
    checkpoint_args = {
        "project": payload["project"],
        "task_id": payload["task_id"],
        "stage": "completed" if payload["verdict"] in {"completed", "done", "effective"} else "blocked",
        "status": "done" if payload["should_resolve_artifact"] else "active",
        "summary": "\n".join(evidence_lines),
        "blockers": payload["residual_risks"] if payload["verdict"] == "blocked" else [],
        "next_step": "Artifact resolved." if payload["should_resolve_artifact"] else "Review remaining work before resolving.",
        "reason": f"project_workflow_submit verdict={payload['verdict']}",
        "acted_by": payload["acted_by"],
        "source": payload["source"],
    }
    operations = [
        {
            "type": "record_task_change",
            "result_key": "checkpoint",
            "task_id": payload["task_id"],
            "payload": build_report_task_checkpoint_payload(checkpoint_args),
            "route_label": "task_change",
        }
    ]
    if payload["should_resolve_artifact"]:
        operations.append(
            {
                "type": "resolve_artifact",
                "result_key": "resolved_artifact",
                "artifact_key": payload["artifact_key"],
                "payload": {
                    "acted_by": payload["acted_by"],
                    "action_source": payload["source"],
                    "reason": payload["completion_summary"],
                },
                "route_label": "resolve_artifact",
            }
        )
    return {
        "status": "ready",
        "operations": operations,
        "receipt": {
            "status": "completed" if payload["should_resolve_artifact"] else "recorded",
            "workflow": "task_completion",
            "project": payload["project"],
            "task_id": payload["task_id"],
            "artifact_key": payload["artifact_key"],
            "verdict": payload["verdict"],
            "routed_to": [],
        },
    }


def format_project_workflow_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_project_workflow_submit_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_task_checkpoint_response(data: dict[str, Any]) -> str:
    base = (
        f"Checkpoint recorded for task {data.get('task_id', '?')}\n"
        f"stage={data.get('stage', 'planning')} status={data.get('status', 'planning')}\n"
        f"change_id={data.get('id', '?')}"
    )
    if "handoff_packet_created" in data:
        base += f"\nhandoff_packet_created={data.get('handoff_packet_created')}"
    if data.get("handoff_memory_id"):
        base += f"\nhandoff_memory_id={data['handoff_memory_id']}"
    if data.get("handoff_label"):
        base += f"\nhandoff_label={data['handoff_label']}"
    if data.get("handoff_error"):
        base += f"\nhandoff_error={data['handoff_error']}"
    if data.get("stage_evidence"):
        base += f"\nstage_evidence={data['stage_evidence']}"
    return base


def format_continue_task_response(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_approve_learning_candidate_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "approved_by": args.get("approved_by", "user"),
        "approval_source": args.get("approval_source", "inline_user_approval"),
        "reason": args.get("reason", ""),
    }


def build_defer_learning_candidate_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "deferred_by": args.get("deferred_by", "user"),
        "defer_source": args.get("defer_source", "inline_user_approval"),
        "reason": args.get("reason", ""),
    }
    if "defer_days" in args and args.get("defer_days") is not None:
        payload["defer_days"] = args["defer_days"]
    return payload


def build_reject_learning_candidate_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "rejected_by": args.get("rejected_by", "user"),
        "rejection_source": args.get("rejection_source", "inline_user_approval"),
        "reason": args.get("reason", ""),
    }


def format_learning_candidate_transition(data: dict[str, Any], *, action: str) -> str:
    meta = data.get("meta") or {}
    lines = [
        f"{action} learning candidate {data['id']}",
        f"status={data.get('status')} scope={data.get('artifact_scope')}",
    ]
    if action == "approved":
        lines.append(f"approved_by={meta.get('approved_by', '-')}")
        lines.append(f"approval_source={meta.get('approval_source', '-')}")
    elif action == "deferred":
        lines.append(f"deferred_by={meta.get('last_deferred_by', '-')}")
        lines.append(f"defer_source={meta.get('last_defer_source', '-')}")
        lines.append(f"defer_count={data.get('defer_count', 0)}")
    elif action == "rejected":
        lines.append(f"rejected_by={meta.get('rejected_by', '-')}")
        lines.append(f"rejection_source={meta.get('rejection_source', '-')}")
    reason = (
        meta.get("approval_reason")
        or meta.get("last_defer_reason")
        or meta.get("rejection_reason")
        or ""
    )
    if reason:
        lines.append(f"reason={reason}")
    return "\n".join(lines)


def format_enrich_task_response(data: dict[str, Any]) -> str:
    if not any(
        data.get(key)
        for key in (
            "components",
            "laws",
            "improvements",
            "runtime_hints",
            "memoirs",
            "tasks",
            "docs_sections",
            "operational_instincts",
            "recommended_mcp_calls",
            "deferred_sources",
        )
    ):
        return data.get("message", "No relevant project context found.")
    suffix: list[str] = []
    available_layers = data.get("available_layers") or {}
    if isinstance(available_layers, dict):
        layer_bits = []
        for name, meta in available_layers.items():
            if not isinstance(meta, dict) or not meta.get("available"):
                continue
            count = meta.get("count")
            bit = str(name)
            if isinstance(count, int):
                bit += f"={count}"
            layer_bits.append(bit)
        if layer_bits:
            suffix.append("Available layers: " + ", ".join(layer_bits) + ". Use detail=full to expand.")
    token_budget = data.get("token_budget") or {}
    if isinstance(token_budget, dict) and token_budget:
        suffix.append(
            "Token budget: "
            f"estimated={token_budget.get('estimated_tokens', '-')}, "
            f"budget={token_budget.get('budget_tokens', '-')}, "
            f"within_soft_limit={token_budget.get('within_soft_limit', '-')}"
        )
    if data.get("missing_sources"):
        suffix.append("Missing sources: " + ", ".join(data["missing_sources"]))
    if data.get("deferred_sources"):
        suffix.append("Deferred background synthesis: " + ", ".join(data["deferred_sources"]))
    if data.get("code_inspection_recommended"):
        suffix.append("Code inspection is recommended as fallback.")
    recommended_calls = data.get("recommended_mcp_calls") or []
    if recommended_calls:
        lines = ["Recommended MCP calls:"]
        for idx, call in enumerate(recommended_calls, 1):
            tool = str(call.get("tool") or "unknown").strip()
            args = call.get("args") or {}
            arg_bits: list[str] = []
            if isinstance(args, dict):
                for key in ("project", "project_id", "status", "type", "limit", "improvement_id", "stage"):
                    value = args.get(key)
                    if value not in (None, ""):
                        arg_bits.append(f"{key}={value}")
            reason = str(call.get("reason") or "").strip()
            line = f"{idx}. `{tool}`"
            if arg_bits:
                line += f" ({', '.join(arg_bits)})"
            lines.append(line)
            if reason:
                lines.append(f"  {reason}")
        suffix.append("\n".join(lines))
    if not suffix:
        return data.get("context", "")
    context = str(data.get("context", "") or "").strip()
    if not context:
        return "\n".join(suffix)
    return context + "\n\n" + "\n".join(suffix)


def format_project_readiness_response(data: dict[str, Any]) -> str:
    lines = [
        f"Project readiness for {data['project_id']}: {data['readiness_level']} ({data['readiness_score']}/100)",
        data.get("summary", ""),
    ]
    coverage = data.get("coverage") or {}
    if coverage:
        lines.append(
            "Coverage: "
            + ", ".join(f"{key}={value}" for key, value in coverage.items())
        )
    snapshot = data.get("snapshot") or {}
    if snapshot:
        pieces = [
            f"mode={snapshot.get('source_mode', '-')}",
            f"repo={snapshot.get('repo', '-') or '-'}",
            f"branch={snapshot.get('branch', '-') or '-'}",
            f"commit={snapshot.get('commit_sha', '-') or '-'}",
        ]
        lines.append("Snapshot: " + ", ".join(pieces))
    strengths = data.get("strengths") or []
    if strengths:
        lines.append("Strengths:")
        lines.extend(f"- {item}" for item in strengths)
    blockers = data.get("blocking_gaps") or []
    if blockers:
        lines.append("Blocking gaps:")
        lines.extend(f"- {item}" for item in blockers)
    actions = data.get("recommended_actions") or []
    if actions:
        lines.append("Recommended actions:")
        lines.extend(f"- {item}" for item in actions[:6])
    instincts = data.get("operational_instincts") or []
    if instincts:
        lines.append("Operational instincts:")
        lines.extend(f"- [{item.get('rank','?')}] {item.get('instinct_id')}: {item.get('action')}" for item in instincts[:5])
    if data.get("code_inspection_recommended"):
        lines.append("Code inspection is recommended as fallback until project knowledge improves.")
    return "\n".join(line for line in lines if line)


def format_project_bootstrap_response(data: dict[str, Any]) -> str:
    lines = [
        f"Bootstrap checklist for {data['project_id']}: {data['readiness_level']}",
        data.get("summary", ""),
        f"next_step={data.get('next_step', '-')}",
    ]
    steps = data.get("steps") or []
    if steps:
        lines.append("Steps:")
        for idx, step in enumerate(steps, 1):
            marker = "required" if step.get("required") else "optional"
            lines.append(
                f"{idx}. [{step.get('status')}] {step.get('title')} ({marker})\n"
                f"   action={step.get('action')}\n"
                f"   tool_hint={step.get('tool_hint')}"
            )
    instincts = data.get("operational_instincts") or []
    if instincts:
        lines.append("Operational instincts:")
        lines.extend(f"- [{item.get('rank','?')}] {item.get('instinct_id')}: {item.get('action')}" for item in instincts[:5])
    return "\n".join(lines)


def format_project_reconstruction_response(data: dict[str, Any]) -> str:
    readiness = data.get("reconstruction_readiness") or {}
    lines = [
        f"Project reconstruction bundle for {data['project_id']}: {readiness.get('status', 'unknown')}",
        "source_policy="
        + ", ".join(f"{key}={value}" for key, value in (data.get("source_policy") or {}).items()),
    ]
    coverage = data.get("coverage") or {}
    if coverage:
        lines.append("Coverage: " + ", ".join(f"{key}={value}" for key, value in coverage.items()))
    missing = readiness.get("missing_layers") or []
    warnings = readiness.get("warning_layers") or []
    if missing:
        lines.append("Missing layers: " + ", ".join(str(item) for item in missing))
    if warnings:
        lines.append("Warning layers: " + ", ".join(str(item) for item in warnings))
    layers = data.get("layers") or []
    if layers:
        lines.append("Reconstruction layers:")
        for layer in layers:
            lines.append(f"- {layer.get('layer')}: count={layer.get('count')} role={layer.get('role')}")
    sequence = data.get("reconstruction_sequence") or []
    if sequence:
        lines.append("Sequence:")
        lines.extend(f"{idx}. {item}" for idx, item in enumerate(sequence, 1))
    actions = data.get("next_actions") or []
    if actions:
        lines.append("Next actions:")
        lines.extend(f"- {item}" for item in actions[:6])
    return "\n".join(line for line in lines if line)


def format_remote_snapshot_plan_response(data: dict[str, Any]) -> str:
    snapshot = data.get("snapshot") or {}
    counts = data.get("counts") or {}
    plan = data.get("plan") or {}
    contract = data.get("contract") or {}
    lines = [
        f"Remote snapshot plan for {data.get('project_id')}: rebuild_mode={plan.get('rebuild_mode', '-')}, projection_target_state={plan.get('projection_target_state', '-')}",
        "Snapshot: "
        + ", ".join(
            [
                f"mode={snapshot.get('source_mode') or '-'}",
                f"repo={snapshot.get('repo') or '-'}",
                f"branch={snapshot.get('branch') or '-'}",
                f"commit={snapshot.get('commit_sha') or '-'}",
                f"base={snapshot.get('base_commit_sha') or '-'}",
                f"dirty={bool(snapshot.get('dirty_workspace'))}",
            ]
        ),
        "Counts: "
        + ", ".join(
            [
                f"changed={counts.get('changed_files', 0)}",
                f"deleted={counts.get('deleted_files', 0)}",
                f"renamed={counts.get('renamed_files', 0)}",
                f"files_with_content={counts.get('files_with_content', 0)}",
            ]
        ),
        "Contract: "
        + ", ".join(
            [
                f"storage_mode={data.get('storage_mode') or '-'}",
                f"requires_selective_source_payload={bool(plan.get('requires_selective_source_payload'))}",
                f"can_skip_when_unchanged={bool(plan.get('can_skip_when_unchanged'))}",
                f"stores_selective_source_cache={bool(contract.get('stores_selective_source_cache'))}",
                f"full_mirror_enabled={bool(contract.get('full_mirror_enabled'))}",
            ]
        ),
    ]
    touched_paths = plan.get("touched_paths") or []
    if touched_paths:
        preview = ", ".join(str(item) for item in touched_paths[:8])
        if len(touched_paths) > 8:
            preview += ", ..."
        lines.append(f"Touched paths: {preview}")
    return "\n".join(lines)


def format_remote_snapshot_sync_response(data: dict[str, Any]) -> str:
    plan = data.get("plan") or {}
    refresh = data.get("refresh") or {}
    inner_plan = plan.get("plan") or {}
    lines = [
        f"Remote snapshot sync for {data.get('project_id')}: action={data.get('action', '-')}",
        f"Plan: rebuild_mode={inner_plan.get('rebuild_mode', '-')}, projection_target_state={inner_plan.get('projection_target_state', '-')}, requires_selective_source_payload={bool(inner_plan.get('requires_selective_source_payload'))}",
    ]
    if refresh.get("message"):
        lines.append(f"Refresh: {refresh['message']}")
    else:
        lines.append(
            "Refresh: "
            + ", ".join(
                [
                    f"updated={len(refresh.get('updated') or [])}",
                    f"up_to_date={len(refresh.get('up_to_date') or [])}",
                    f"requires_source_payload={len(refresh.get('requires_source_payload') or [])}",
                    f"used_remote_file_payload={bool(refresh.get('used_remote_file_payload'))}",
                ]
            )
        )
    missing = refresh.get("requires_source_payload") or []
    if missing:
        lines.append("Components needing source payload: " + ", ".join(str(item) for item in missing[:8]))
    return "\n".join(lines)


def format_storage_trust_response(data: dict[str, Any]) -> str:
    lines = [f"Storage trust: {data.get('status', 'unknown')}"]
    if data.get("summary"):
        lines.append(data["summary"])
    signals = data.get("signals") or {}
    degraded_slices = signals.get("degraded_slices") or []
    if degraded_slices:
        lines.append(f"Degraded integrity slices: {', '.join(degraded_slices)}")
    lines.append(f"Active hygiene findings: {signals.get('active_hygiene_findings', 0)}")
    if signals.get("manual_review_pending"):
        lines.append(f"Manual review pending: {signals['manual_review_pending']}")
    if signals.get("quarantine_candidates"):
        lines.append(f"Quarantine candidates: {signals['quarantine_candidates']}")
    if signals.get("delete_ready"):
        lines.append(f"Delete ready: {signals['delete_ready']}")
    next_actions = data.get("next_actions") or []
    if next_actions:
        lines.append("Next actions:")
        for action in next_actions[:5]:
            lines.append(f"- {action}")
    return "\n".join(lines)


def format_coordination_message(data: dict[str, Any], *, prefix: str = "Coordination message") -> str:
    lines = [
        f"{prefix} {data['memory_id']}",
        f"project={data.get('project', '-')} thread={data.get('thread_id', '-')}",
        f"from={data.get('from_agent', '-')} to={data.get('to_agent', '-')}",
        f"type={data.get('message_type', 'note')} status={data.get('status', 'new')} priority={data.get('priority', 'normal')}",
    ]
    if data.get("requested_action"):
        lines.append(f"requested_action={data['requested_action']}")
    if data.get("response_to_message_id"):
        lines.append(f"response_to={data['response_to_message_id']}")
    lines.append(data.get("content", ""))
    return "\n".join(lines)


def format_coordination_list(data: dict[str, Any], *, empty_text: str) -> str:
    items = data.get("items", [])
    if not items:
        return empty_text
    lines: list[str] = []
    for i, item in enumerate(items, 1):
        lines.append(
            f"{i}. [{item.get('status', 'new')}] {item.get('message_type', 'note')} "
            f"{item.get('from_agent', '?')} -> {item.get('to_agent', '?')}"
        )
        lines.append(
            f"   id={item.get('memory_id')} thread={item.get('thread_id')} project={item.get('project')}"
        )
        if item.get("requested_action"):
            lines.append(f"   action={item['requested_action']}")
        lines.append(f"   {str(item.get('content', ''))[:200]}")
    return "\n".join(lines)


def build_mnemoforge_initialize_hint(agent_id: str) -> dict[str, Any]:
    from app.services.instruction_layers import build_l0_policy
    
    return {
        "agent_id": agent_id,
        "tip": (
            "Call get_onboarding at the start of a session or when you are lost. "
            "If you collaborate on a project, call pickup_coordination_messages for your agent_id and project. "
            "The default tools/list response is compact; start with ask_project for human project questions or project_work for explicit project work, and only request mode=full for deep/debug access. "
            "If you need to choose a MCP path, call normalize_mcp_intent before you guess at tools. "
            "If you need to resume a task, call reopen_task before you do anything else. "
            "If you are working on a task, record a checkpoint at planning and after every meaningful stage transition with report_task_checkpoint. "
            "If storage health may affect retrieval, call get_storage_trust_status."
        ),
        "semantic_defaults": [
            "Prefer project-scoped operations and keep project_id consistent.",
            "Use coordination messages for requests, replies, and handoff status; they do not become project truth automatically.",
            "Prefer semantic routes such as /api/v1/coordination/... over internal module topology.",
            "Treat degraded storage trust as an operational constraint: affected retrieval or learning paths may require caution or operator review.",
        ],
        "tool_catalog": {
            "preferred_mode": "compact",
            "compact_request": {"method": "tools/list", "params": {"mode": "compact"}},
            "full_request": {"method": "tools/list", "params": {"mode": "full"}},
            "recommended_first_tool": "ask_project",
            "reason": "Default tools/list is the compact thematic public surface; request the full flat catalog only for deeper/debug access.",
        },
        "l0_policy": build_l0_policy(),
        "instruction_layers": {
            "L0": "always_present",
            "L1": "from_handoff_or_enrichment",
            "L2": "auto_loaded",
            "L3": "on_demand",
            "L4": "opt_in",
        },
    }


def build_mnemoforge_onboarding_basics() -> str:
    return (
        "MNEMOFORGE BASICS:\n"
        "  - Call get_onboarding at session start or when you lose context.\n"
        "  - If onboarding warns that storage trust is degraded, call get_storage_trust_status before trusting affected retrieval or learning paths.\n"
        "  - If another agent may have contacted you, call pickup_coordination_messages with your agent_id and project.\n"
        "  - If the right MCP path is unclear, call normalize_mcp_intent first.\n"
        "  - If you need to resume a task, call reopen_task instead of bypassing MCP.\n"
        "  - If you are working on a task, call report_task_checkpoint at planning, blockers, interruptions, handoff, and completion.\n"
        "  - Use send_coordination_message for requests, replies, and handoffs; coordination is operational, not project truth.\n"
        "  - Keep project_id explicit and consistent across retrieval, bootstrap, and coordination."
    )


# Compatibility for older internal imports during the project rename window.
build_supermemory_initialize_hint = build_mnemoforge_initialize_hint
build_supermemory_onboarding_basics = build_mnemoforge_onboarding_basics


def build_set_canonical_status_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "suppressed": args["suppressed"],
        "reason": args.get("reason"),
        "reviewed_by": args.get("reviewed_by", "user"),
        "review_source": args.get("review_source", "inline_user_approval"),
    }


def format_set_canonical_status_response(data: dict[str, Any]) -> str:
    return (
        f"Canonical {data['id']} status={data['canonical_status']} "
        f"suppressed={bool(data.get('suppressed'))}"
    )


def build_merge_canonicals_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_id": args["target_id"],
        "reviewed_by": args.get("reviewed_by", "user"),
        "review_source": args.get("review_source", "inline_user_approval"),
        "reason": args.get("reason"),
    }


def format_merge_canonicals_response(data: dict[str, Any]) -> str:
    return (
        f"Merged canonical {data['source_id']} -> {data['target_id']}\n"
        f"topic_path={data['topic_path']} supports={data['merged_support_count']}"
    )


def build_layout_feedback_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "correction_id": args["correction_id"],
        "confirmed": args["confirmed"],
        "reviewed_by": args.get("reviewed_by", "user"),
        "review_source": args.get("review_source", "inline_user_approval"),
        "reason": args.get("reason"),
    }
    if "correct_text" in args:
        payload["correct_text"] = args.get("correct_text")
    return payload


def format_layout_feedback_response(data: dict[str, Any]) -> str:
    status = "confirmed" if data["confirmed"] else "rejected"
    return f"Feedback recorded ({status}) for correction {data['correction_id']}"


def build_load_instruction_layer_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {"layer": args["layer"]}
    if args.get("category"):
        payload["category"] = args["category"]
    if args.get("section"):
        payload["section"] = args["section"]
    return payload


def format_load_instruction_layer_response(content: str) -> str:
    return content


def build_list_instruction_layers_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = {}
    if args.get("layer"):
        payload["layer"] = args["layer"]
    return payload


def format_list_instruction_layers_response(data: dict[str, Any]) -> str:
    lines = ["Available Instruction Layers:"]
    
    for layer_name, items in data.items():
        lines.append(f"\n## {layer_name}")
        
        if layer_name == "L2":
            for item in items:
                lines.append(f"  - {item['category']}: {item['description']}")
        elif layer_name == "L3":
            for item in items:
                sections = ", ".join(item.get("sections", []))
                lines.append(f"  - {item['category']}: {sections}")
        elif layer_name == "L4":
            for item in items:
                lines.append(f"  - {item}")
    
    return "\n".join(lines)
