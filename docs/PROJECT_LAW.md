# SuperMemory Project Law

This document is a law of the current project: `supermemory`.

It is not a universal law for all projects.
Other projects may and should have their own laws.

`supermemory` exists to help agents work with any project, in any subject area, while preserving project-specific memory, tasks, documentation, and reusable experience.

## Law 1: Every Project Has Its Own Laws

Each project must be able to define its own laws for agents.

Those laws are part of project knowledge.
They are not global defaults.
They are not hidden prompt assumptions.
They are not hardcoded subject-matter rules.

For `supermemory` itself, this means the system must support project-specific laws as a first-class capability.

## Law 2: Laws Live in Memory, Not in Code

Code defines mechanisms.
Memory defines project knowledge.
Project laws are project knowledge.

Therefore:
- laws must be represented as project-scoped knowledge artifacts
- laws must be discoverable through memory and MCP surfaces
- laws must be documented, versioned, and reviewable
- laws must not be embedded as private assumptions in implementation logic

If a behavior only works because the current project's subject matter was hardcoded into the mechanism, that is a violation of this law.

## Law 3: Project Work Is Local First

Every project has its own:
- improvements
- self-documentation
- decisions
- runbooks
- operational memory
- laws

All of these live in one shared system, but they remain scoped to their source project unless promoted by evidence.

Nothing starts as universal truth.

## Law 4: Generalization Must Be Earned

Knowledge may move upward only after repeated evidence and review:

`project -> family -> domain -> principle -> meta`

This applies to laws too.

A project law may later become:
- a family-level law
- a domain law
- a more general engineering principle

But promotion must be evidence-based.
No single session, no single project, and no single agent response may define a universal rule.

## Law 5: Self-Documentation Must Be Grounded

Each project must document itself from evidence:
- source code
- conversations
- work events
- outcomes
- improvements
- accepted artifacts

LLM-generated documentation is allowed only as a synthesis layer over project evidence.

## Law 6: RepRap Is a Normal Case

When `supermemory` works on itself, it is still just a project operating under project laws.

Self-reference does not grant special permission to hardcode project-specific knowledge into mechanisms.

`supermemory` must improve itself by using the same project-memory, improvement, documentation, and law-evolution loops that it provides to other projects.

## Law 7: Self-Improving Project Laws Are A Core Goal

One of the main goals of `supermemory` is to let project laws evolve safely over time.

That means the system must support:
- creating project laws
- storing them in project memory
- applying them in agent workflows
- documenting why they exist
- attaching evidence and counter-evidence
- reviewing and revising them
- promoting them when they generalize across projects
- suppressing or rolling them back when they prove wrong

## Engineering Consequence

When choosing between:
- changing the mechanism to fit the current project's subject matter
- keeping the mechanism general and letting the project carry its own laws and knowledge

the second option is the default.

If an implementation makes `supermemory` better only for the current project by embedding project-specific domain knowledge into code, it is almost certainly wrong.
