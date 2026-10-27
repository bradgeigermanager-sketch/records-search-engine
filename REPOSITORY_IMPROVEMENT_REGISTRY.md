# Owner Repository Improvement Registry

**Registry owner:** `bradgeigermanager-sketch`  
**Coordinator:** [records-search-engine](https://github.com/bradgeigermanager-sketch/records-search-engine)  
**Last registry update:** 2026-10-26  
**Purpose:** Maintain a durable queue and audit trail for reviewing, organizing, completing, and verifying repository improvements across the owner account.

## Status definitions

| Status | Meaning |
|---|---|
| `NOT_STARTED` | Identified but not yet reviewed in this work program |
| `IN_REVIEW` | Contents are being inventoried or assessed |
| `PLANNED` | Scope and next action have been identified |
| `IN_PROGRESS` | Work is actively being changed |
| `BLOCKED` | A missing dependency, access, decision, or technical issue prevents progress |
| `DONE` | Change or review was completed; verification may still be pending |
| `VERIFIED` | Relevant automated checks or explicit review were completed and evidence is recorded |
| `DEFERRED` | Intentionally postponed with a reason |

## Program-level work queue

| ID | Work item | Status | Evidence / location | Next action |
|---|---|---|---|---|
| `PORT-001` | Establish central registry and running log | `DONE` | This file and [IMPROVEMENT_LOG.md](IMPROVEMENT_LOG.md); commit [d465edc](https://github.com/bradgeigermanager-sketch/records-search-engine/commit/d465edc155fd10ebccd6a6b57a4228f4ea632011) establishes registry link in README | Keep these files updated with each material repository action |
| `PORT-002` | Improve README/entry points for first selected repositories | `DONE` | See [Improvement Log](IMPROVEMENT_LOG.md) | Inspect actual contents, then add tests and structure where appropriate |
| `PORT-003` | Create a verified owner-wide repository inventory | `IN_PROGRESS` | Initial account listing and selected repository inspections; inventory is not yet exhaustive | Paginate repository listing and record each repository's default branch, purpose, size, files, and status |
| `PORT-004` | Establish shared naming, schema, provenance, and versioning conventions | `PLANNED` | Cross-repository candidate links below | Compare existing schemas before defining a shared contract |
| `PORT-005` | Ingest selected repositories into the Records Search Engine and test actual engine behavior | `BLOCKED` | Engine supports `POST /github/ingest-repository` and `POST /github/ingest-owner`; no reachable running API URL has been confirmed in this workflow | Start a local/deployed instance, then record ingestion counts and query test evidence |
| `PORT-006` | Create cross-repository dependency/interface map | `PLANNED` | Candidate map below | Confirm relationships from repository content and actual interfaces, not names alone |
| `PORT-007` | Define quality gates for completed repository improvements | `PLANNED` | See [review template](docs/repository-review-template.md) | Add per-language tests, link checks, schema validation, and CI expectations |

## Repository registry

Repository statuses below describe the **current improvement-program review state**, not the maturity of the underlying project. A README update alone does not imply code, schemas, or tests are complete.

| Repository | Initial role / observed entry point | Review status | Initial completed work | Open work |
|---|---|---|---|---|
| [records-search-engine](https://github.com/bradgeigermanager-sketch/records-search-engine) | FastAPI/SQLite FTS5 search, document versioning, provenance, GitHub ingestion, relationship review | `IN_PROGRESS` | Added registry/log/review-template navigation to README | Complete inventory; live end-to-end ingestion/search test; expand search facets and operational safeguards |
| [Records-Deduplication-Suite](https://github.com/bradgeigermanager-sketch/Records-Deduplication-Suite) | Conceptual/procedural deduplication framework and Record Relationship Determination | `DONE` | Organized README, repository contents, processing pipeline, safeguards, and phased roadmap; [commit](https://github.com/bradgeigermanager-sketch/Records-Deduplication-Suite/commit/746b72c0092bd9c0854bfcc7469cc773ff29a357) | Validate actual filenames/links; reconcile outcome vocabulary; define and test versioned schemas |
| [Data-Types-and-Templates-Library](https://github.com/bradgeigermanager-sketch/Data-Types-and-Templates-Library) | Browser template/fillable-form interface in `index.html` | `DONE` | Added role, conventions, status limits, and roadmap to README; [commit](https://github.com/bradgeigermanager-sketch/Data-Types-and-Templates-Library/commit/3b83e67c57d8944836b0f2389f9b9a1b5d6f9611) | Inventory UI behavior; define schemas; test import/export and persistence |
| [Procedures-Playbooks-Library](https://github.com/bradgeigermanager-sketch/Procedures-Playbooks-Library) | Browser playbook UI, JSON templates, generator scripts | `DONE` | Added repository map, graph-integrity rules, roadmap, and limitations; [commit](https://github.com/bradgeigermanager-sketch/Procedures-Playbooks-Library/commit/46b3afce5276565d42f78398d31c985be5cc71ac) | Validate JSON schemas; test graph links, generator behavior, and round trips |
| [Meta-Search-Resolver](https://github.com/bradgeigermanager-sketch/Meta-Search-Resolver) | Numbered design notes `1.txt`–`22.txt` | `DONE` | Added consolidation architecture, identity-resolution safeguards, and note-review plan; [commit](https://github.com/bradgeigermanager-sketch/Meta-Search-Resolver/commit/330b48cb897d3c085462428d08cd345df54bd773) | Classify all notes; select canonical design; validate executable components/connectors |
| [Data-Source-Mapping-And-Overlap-Comparison-Suite](https://github.com/bradgeigermanager-sketch/Data-Source-Mapping-And-Overlap-Comparison-Suite) | Candidate source comparison/overlap domain | `NOT_STARTED` | Identified as likely adjacent project | Inspect files and record actual purpose, entry points, and maturity |
| [Dimensional-Mapping-Coordinate-Tree](https://github.com/bradgeigermanager-sketch/Dimensional-Mapping-Coordinate-Tree) | Candidate coordinate/dimensional mapping domain | `NOT_STARTED` | Identified as likely adjacent project | Inspect files and confirm scope |
| [Multi-Temporal-Lineage-Comparison-And-Originality-Derivation-Engine](https://github.com/bradgeigermanager-sketch/Multi-Temporal-Lineage-Comparison-And-Originality-Derivation-Engine) | Candidate temporal lineage/originality analysis domain | `NOT_STARTED` | Identified as likely adjacent project | Inspect files and verify actual implementation |
| [Universal-Cataloging-System](https://github.com/bradgeigermanager-sketch/Universal-Cataloging-System) | Candidate cataloging/identity metadata domain | `NOT_STARTED` | Identified as likely adjacent project | Inspect files and identify overlaps with records/search |
| [Forms-and-Templates-Library](https://github.com/bradgeigermanager-sketch/Forms-and-Templates-Library) | Candidate form/schema library | `NOT_STARTED` | Identified as likely adjacent project | Inspect schema directory and validation strategy |
| [Policies-and-Procedures-Library](https://github.com/bradgeigermanager-sketch/Policies-and-Procedures-Library) | Candidate governance and policy source library | `NOT_STARTED` | Identified as likely adjacent project | Inspect actual contents; distinguish policy text from operational playbooks |
| [Continuous-Quality-Improvement-Knowledge-Portal](https://github.com/bradgeigermanager-sketch/Continuous-Quality-Improvement-Knowledge-Portal) | Candidate quality/review workflow domain | `NOT_STARTED` | Identified as likely adjacent project | Inspect files and define its relation to review/verification workflow |

This is an initial working register, **not yet a complete inventory of every repository under the owner**. Add every remaining repository after paginating the owner listing. If a repository is private/inaccessible or a name is not confirmed, record that explicitly rather than infer its contents.

## Candidate cross-repository map

These are hypotheses to investigate, not confirmed dependencies:

| Producer / domain | Possible consumer / related domain | Relationship to verify |
|---|---|---|
| Data-Types-and-Templates-Library | Forms-and-Templates-Library | Shared primitives versus duplicate template catalogs |
| Data-Types-and-Templates-Library | Procedures-Playbooks-Library | Typed inputs/outputs for playbooks |
| Records-Deduplication-Suite | records-search-engine | Search retrieves candidate records; deduplication assesses record identity/relationships |
| Data-Source-Mapping-And-Overlap-Comparison-Suite | Records-Deduplication-Suite | Source-level overlap evidence versus record-level duplicate assessment |
| Multi-Temporal-Lineage-Comparison-And-Originality-Derivation-Engine | Records-Deduplication-Suite | Version lineage and originality as evidence, not automatic merge decisions |
| Meta-Search-Resolver | records-search-engine | Search connectors and retrieval results, subject to privacy, access, and provider rules |
| Universal-Cataloging-System | Data-Types-and-Templates-Library | Shared entity/identifier definitions |
| Continuous-Quality-Improvement-Knowledge-Portal | all repositories | Review findings, corrective actions, and quality tracking |

## Repository review checklist

For each repository, record:
- [ ] Purpose, intended users, scope, non-goals, and maturity.
- [ ] Default branch, inventory of files, entry point, and runnable instructions.
- [ ] Content classes: policy, specification, procedure, schema, code, test, example, or historical note.
- [ ] Missing or duplicate documentation; stale links; naming inconsistency.
- [ ] Existing schemas, APIs, dependency declarations, and versioning conventions.
- [ ] Provenance, source citations, data sensitivity, security and failure modes.
- [ ] Tests/validation actually run and exact results.
- [ ] Cross-repository relationships and the evidence supporting them.
- [ ] Prioritized actions with status, commit, verification, and next step.

## Update protocol

After every material change:
1. Update the relevant repository row and work-item status.
2. Append a dated entry to [IMPROVEMENT_LOG.md](IMPROVEMENT_LOG.md) with repository, change, commit, checks, result, and follow-up.
3. Link evidence such as commits, workflow runs, tests, or inspected file paths.
4. Keep unresolved items open. Never infer that a workflow passed from the existence of a commit.
