from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models.mcp_workflow import (
    ClerkCaptureRegistry,
    MailboxFormPolicySpec,
    MailboxFormSpec,
    MailboxProtocolSpec,
    McpRouteCatalogSpec,
    McpToolContractCatalogSpec,
    McpToolFamilyRegistry,
    McpToolSurfaceSpec,
    RuntimeProfileSpec,
    ResponseEnvelopeSpec,
    TaskLeaseWorkflowSpec,
    WorkflowFeatureToggleRegistry,
    WorkflowStateName,
    WorkflowStateSpec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC_ROOT = PROJECT_ROOT / "app" / "mcp_specs"


class WorkflowSpecError(ValueError):
    """Raised when declarative MCP workflow specs are missing or invalid."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowSpecError(f"Workflow spec not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowSpecError(f"Workflow spec is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowSpecError(f"Workflow spec must be a JSON object: {path}")
    return data


def load_state_spec(state: WorkflowStateName | str, *, spec_root: Path = DEFAULT_SPEC_ROOT) -> WorkflowStateSpec:
    state_name = str(state or "").strip()
    path = spec_root / "states" / f"{state_name}.json"
    return WorkflowStateSpec.model_validate(_load_json(path))


def list_state_specs(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> list[WorkflowStateSpec]:
    states_dir = spec_root / "states"
    if not states_dir.exists():
        raise WorkflowSpecError(f"Workflow states directory not found: {states_dir}")
    specs = [WorkflowStateSpec.model_validate(_load_json(path)) for path in sorted(states_dir.glob("*.json"))]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            duplicates.add(spec.id)
        seen.add(spec.id)
    if duplicates:
        raise WorkflowSpecError(f"Duplicate workflow state spec ids: {', '.join(sorted(duplicates))}")
    return specs


def load_packet_template(template: str, *, spec_root: Path = DEFAULT_SPEC_ROOT) -> str:
    relative = Path(str(template or "").strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkflowSpecError(f"Packet template path must stay inside spec root: {template}")
    path = spec_root / relative
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkflowSpecError(f"Packet template not found: {path}") from exc


def load_clerk_capture_registry(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> ClerkCaptureRegistry:
    path = spec_root / "clerk" / "capture_types.json"
    return ClerkCaptureRegistry.model_validate(_load_json(path))


def load_feature_toggle_registry(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> WorkflowFeatureToggleRegistry:
    path = spec_root / "features" / "toggles.json"
    return WorkflowFeatureToggleRegistry.model_validate(_load_json(path))


def load_runtime_profile_spec(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> RuntimeProfileSpec:
    path = spec_root / "identity" / "runtime_profile.json"
    return RuntimeProfileSpec.model_validate(_load_json(path))


def load_task_lease_spec(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> TaskLeaseWorkflowSpec:
    path = spec_root / "leases" / "task_reclaim.json"
    return TaskLeaseWorkflowSpec.model_validate(_load_json(path))


def load_response_envelope_spec(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> ResponseEnvelopeSpec:
    path = spec_root / "responses" / "envelope.json"
    return ResponseEnvelopeSpec.model_validate(_load_json(path))


def load_mailbox_protocol_spec(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> MailboxProtocolSpec:
    path = spec_root / "mailbox" / "protocol.json"
    return MailboxProtocolSpec.model_validate(_load_json(path))


def load_mailbox_form_policy_spec(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> MailboxFormPolicySpec:
    path = spec_root / "mailbox" / "form_policy.json"
    return MailboxFormPolicySpec.model_validate(_load_json(path))


def load_route_catalog_spec(facade: str, *, spec_root: Path = DEFAULT_SPEC_ROOT) -> McpRouteCatalogSpec:
    facade_name = str(facade or "").strip()
    path = spec_root / "routes" / f"{facade_name}.json"
    spec = McpRouteCatalogSpec.model_validate(_load_json(path))
    if spec.facade != facade_name:
        raise WorkflowSpecError(f"Route catalog facade mismatch: expected {facade_name}, got {spec.facade}")
    intent_types = [route.intent_type for route in spec.routes]
    duplicates = {intent_type for intent_type in intent_types if intent_types.count(intent_type) > 1}
    if duplicates:
        raise WorkflowSpecError(f"Duplicate route intent types for {facade_name}: {', '.join(sorted(duplicates))}")
    return spec


def load_tool_family_registry(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> McpToolFamilyRegistry:
    path = spec_root / "discovery" / "tool_families.json"
    spec = McpToolFamilyRegistry.model_validate(_load_json(path))
    family_ids = [family.id for family in spec.families]
    duplicates = {family_id for family_id in family_ids if family_ids.count(family_id) > 1}
    if duplicates:
        raise WorkflowSpecError(f"Duplicate tool family ids: {', '.join(sorted(duplicates))}")
    return spec


def load_tool_surface_spec(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> McpToolSurfaceSpec:
    path = spec_root / "discovery" / "tool_surface.json"
    return McpToolSurfaceSpec.model_validate(_load_json(path))


def load_tool_contract_catalog_spec(
    catalog: str,
    *,
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> McpToolContractCatalogSpec:
    catalog_name = str(catalog or "").strip()
    path = spec_root / "tool_contracts" / f"{catalog_name}.json"
    spec = McpToolContractCatalogSpec.model_validate(_load_json(path))
    if spec.id != catalog_name:
        raise WorkflowSpecError(f"Tool contract catalog id mismatch: expected {catalog_name}, got {spec.id}")
    tool_names = [tool.name for tool in spec.tools]
    duplicates = {tool_name for tool_name in tool_names if tool_names.count(tool_name) > 1}
    if duplicates:
        raise WorkflowSpecError(f"Duplicate tool contract names for {catalog_name}: {', '.join(sorted(duplicates))}")
    return spec


def list_mailbox_form_specs(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> list[MailboxFormSpec]:
    forms_dir = spec_root / "forms"
    if not forms_dir.exists():
        raise WorkflowSpecError(f"Mailbox forms directory not found: {forms_dir}")
    specs = [MailboxFormSpec.model_validate(_load_json(path)) for path in sorted(forms_dir.glob("*.json"))]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            duplicates.add(spec.id)
        seen.add(spec.id)
    if duplicates:
        raise WorkflowSpecError(f"Duplicate mailbox form spec ids: {', '.join(sorted(duplicates))}")
    return specs


def list_mailbox_forms_for_state(
    state: WorkflowStateName | str,
    *,
    spec_root: Path = DEFAULT_SPEC_ROOT,
) -> list[MailboxFormSpec]:
    state_name = str(state or "").strip()
    return [form for form in list_mailbox_form_specs(spec_root=spec_root) if state_name in form.states]


def validate_specs(*, spec_root: Path = DEFAULT_SPEC_ROOT) -> dict[str, Any]:
    state_specs = list_state_specs(spec_root=spec_root)
    missing_templates = []
    for spec in state_specs:
        try:
            load_packet_template(spec.packet.template, spec_root=spec_root)
        except WorkflowSpecError as exc:
            missing_templates.append(str(exc))
    clerk_registry = load_clerk_capture_registry(spec_root=spec_root)
    feature_registry = load_feature_toggle_registry(spec_root=spec_root)
    runtime_profile = load_runtime_profile_spec(spec_root=spec_root)
    task_lease = load_task_lease_spec(spec_root=spec_root)
    response_envelope = load_response_envelope_spec(spec_root=spec_root)
    mailbox_protocol = load_mailbox_protocol_spec(spec_root=spec_root)
    mailbox_form_policy = load_mailbox_form_policy_spec(spec_root=spec_root)
    route_catalogs = [
        load_route_catalog_spec(facade, spec_root=spec_root)
        for facade in ("project_work", "project_rules", "project_context", "project_verify", "project_capture")
    ]
    route_catalogs_by_facade = {catalog.facade: catalog for catalog in route_catalogs}
    tool_family_registry = load_tool_family_registry(spec_root=spec_root)
    tool_surface = load_tool_surface_spec(spec_root=spec_root)
    public_tool_contracts = load_tool_contract_catalog_spec("public_surface", spec_root=spec_root)
    discovery_tool_contracts = load_tool_contract_catalog_spec("discovery_read", spec_root=spec_root)
    mailbox_tool_contracts = load_tool_contract_catalog_spec("mailbox_protocol", spec_root=spec_root)
    instruction_tool_contracts = load_tool_contract_catalog_spec("instruction_layers", spec_root=spec_root)
    learning_review_tool_contracts = load_tool_contract_catalog_spec("learning_review", spec_root=spec_root)
    improvement_review_tool_contracts = load_tool_contract_catalog_spec("improvement_review", spec_root=spec_root)
    project_identity_tool_contracts = load_tool_contract_catalog_spec("project_identity", spec_root=spec_root)
    mailbox_forms = list_mailbox_form_specs(spec_root=spec_root)
    known_state_ids = {spec.id for spec in state_specs}
    known_toggle_ids = {toggle.id for toggle in feature_registry.toggles}
    known_form_ids = {form.id for form in mailbox_forms}
    unknown_toggle_refs = [
        f"{spec.id}:{toggle.id}"
        for spec in state_specs
        for toggle in spec.feature_toggles
        if toggle.id not in known_toggle_ids
    ]
    unknown_form_toggle_refs = [
        f"{form.id}:{toggle_id}"
        for form in mailbox_forms
        for toggle_id in form.feature_toggles
        if toggle_id not in known_toggle_ids
    ]
    unknown_replacement_forms = [
        f"{form.id}:{replacement_id}"
        for form in mailbox_forms
        for replacement_id in form.replacement_form_ids
        if replacement_id not in known_form_ids
    ]
    unknown_policy_states = [
        state_id for state_id in mailbox_form_policy.state_priorities.keys() if state_id not in known_state_ids
    ]
    unknown_policy_forms = [
        f"{state_id}:{form_id}"
        for state_id, form_ids in mailbox_form_policy.state_priorities.items()
        for form_id in form_ids
        if form_id not in known_form_ids
    ]
    unknown_visibility_forms = [
        f"{rule.packet_profile}:{form_id}"
        for rule in mailbox_form_policy.visibility_rules
        for form_id in [*rule.hidden_form_ids, *rule.hide_only_when_form_ids_available]
        if form_id not in known_form_ids
    ]
    if missing_templates:
        raise WorkflowSpecError("; ".join(missing_templates))
    if unknown_toggle_refs:
        raise WorkflowSpecError(f"Unknown feature toggle refs: {', '.join(sorted(unknown_toggle_refs))}")
    if unknown_form_toggle_refs:
        raise WorkflowSpecError(f"Unknown form feature toggle refs: {', '.join(sorted(unknown_form_toggle_refs))}")
    if unknown_replacement_forms:
        raise WorkflowSpecError(f"Unknown replacement form refs: {', '.join(sorted(unknown_replacement_forms))}")
    if unknown_policy_states:
        raise WorkflowSpecError(f"Unknown mailbox form policy states: {', '.join(sorted(unknown_policy_states))}")
    if unknown_policy_forms:
        raise WorkflowSpecError(f"Unknown mailbox form policy form refs: {', '.join(sorted(unknown_policy_forms))}")
    if unknown_visibility_forms:
        raise WorkflowSpecError(f"Unknown mailbox form visibility refs: {', '.join(sorted(unknown_visibility_forms))}")
    return {
        "state_count": len(state_specs),
        "states": [spec.id for spec in state_specs],
        "clerk_capture_type_count": len(clerk_registry.capture_types),
        "clerk_capture_types": [item.id for item in clerk_registry.capture_types],
        "feature_toggle_count": len(feature_registry.toggles),
        "feature_toggles": [item.id for item in feature_registry.toggles],
        "runtime_profile_preset_count": len(runtime_profile.profile_presets),
        "runtime_profile_presets": [item.id for item in runtime_profile.profile_presets],
        "task_reclaim_policy_count": len(task_lease.reclaim_policies),
        "task_reclaim_policies": [item.id for item in task_lease.reclaim_policies],
        "response_public_fields": [item.name for item in response_envelope.public_fields],
        "response_internal_fields": [item.name for item in response_envelope.internal_fields],
        "mailbox_actions": [item.id for item in mailbox_protocol.external_actions],
        "mailbox_forms": [item.id for item in mailbox_forms],
        "mailbox_form_policy_states": list(mailbox_form_policy.state_priorities.keys()),
        "mailbox_form_visibility_profiles": [rule.packet_profile for rule in mailbox_form_policy.visibility_rules],
        "route_catalogs": [catalog.facade for catalog in route_catalogs],
        "project_work_route_intents": [
            route.intent_type for route in route_catalogs_by_facade["project_work"].routes
        ],
        "project_rules_route_intents": [
            route.intent_type for route in route_catalogs_by_facade["project_rules"].routes
        ],
        "tool_families": [family.id for family in tool_family_registry.families],
        "tool_surface_public_entrypoints": tool_surface.public_entrypoints,
        "public_tool_contracts": [tool.name for tool in public_tool_contracts.tools],
        "discovery_tool_contracts": [tool.name for tool in discovery_tool_contracts.tools],
        "mailbox_tool_contracts": [tool.name for tool in mailbox_tool_contracts.tools],
        "instruction_tool_contracts": [tool.name for tool in instruction_tool_contracts.tools],
        "learning_review_tool_contracts": [tool.name for tool in learning_review_tool_contracts.tools],
        "improvement_review_tool_contracts": [tool.name for tool in improvement_review_tool_contracts.tools],
        "project_identity_tool_contracts": [tool.name for tool in project_identity_tool_contracts.tools],
    }


@lru_cache(maxsize=1)
def cached_validate_default_specs() -> dict[str, Any]:
    return validate_specs()
