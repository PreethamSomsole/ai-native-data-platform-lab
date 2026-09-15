"""Canonical text documents derived from validated semantic registry records."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from ai_data_platform.models import Registry


class SemanticDocumentKind(str, Enum):
    CONCEPT = "concept"
    ENTITY = "entity"
    METRIC = "metric"
    DATASET = "dataset"


class SemanticDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    kind: SemanticDocumentKind
    record_id: str
    text: str


def _field(label: str, value: str | list[str]) -> str:
    rendered = ", ".join(value) if isinstance(value, list) else value
    return f"{label}: {rendered}" if rendered else ""


def _document(kind: SemanticDocumentKind, record_id: str, fields: list[str]) -> SemanticDocument:
    return SemanticDocument(
        document_id=f"{kind.value}:{record_id}",
        kind=kind,
        record_id=record_id,
        text="\n".join(field for field in fields if field),
    )


def build_semantic_documents(registry: Registry) -> list[SemanticDocument]:
    """Render every registry record into deterministic, provider-neutral text."""
    documents: list[SemanticDocument] = []
    for concept in sorted(registry.concepts.values(), key=lambda item: item.id):
        documents.append(
            _document(
                SemanticDocumentKind.CONCEPT,
                concept.id,
                [
                    _field("id", concept.id),
                    _field("name", concept.name),
                    _field("description", concept.description),
                    _field("synonyms", concept.synonyms),
                ],
            )
        )
    for entity in sorted(registry.entities.values(), key=lambda item: item.id):
        concept_text = [
            f"{registry.concepts[item].name}: {registry.concepts[item].description}"
            for item in entity.related_concept_ids
        ]
        documents.append(
            _document(
                SemanticDocumentKind.ENTITY,
                entity.id,
                [
                    _field("id", entity.id),
                    _field("name", entity.name),
                    _field("description", entity.description),
                    _field("related concepts", concept_text),
                ],
            )
        )
    for metric in sorted(registry.metrics.values(), key=lambda item: item.id):
        concept_text = [
            f"{registry.concepts[item].name}: {registry.concepts[item].description}"
            for item in metric.related_concept_ids
        ]
        documents.append(
            _document(
                SemanticDocumentKind.METRIC,
                metric.id,
                [
                    _field("id", metric.id),
                    _field("name", metric.name),
                    _field("domain", metric.domain),
                    _field("owner", metric.owner),
                    _field("definition", metric.definition),
                    _field("aliases", metric.aliases),
                    _field("target users", metric.target_users),
                    _field("intended use cases", metric.intended_use_cases),
                    _field("prohibited use cases", metric.prohibited_use_cases),
                    _field("related concepts", concept_text),
                ],
            )
        )
    for dataset in sorted(registry.datasets.values(), key=lambda item: item.id):
        metrics = [
            f"{registry.metrics[item].name}: {registry.metrics[item].definition}"
            for item in dataset.metric_ids
        ]
        entities = [
            f"{registry.entities[item].name}: {registry.entities[item].description}"
            for item in dataset.entity_ids
        ]
        concepts = [
            f"{registry.concepts[item].name}: {registry.concepts[item].description}"
            for item in dataset.dimension_concept_ids
        ]
        documents.append(
            _document(
                SemanticDocumentKind.DATASET,
                dataset.id,
                [
                    _field("id", dataset.id),
                    _field("name", dataset.name),
                    _field("domain", dataset.domain),
                    _field("layer", dataset.layer),
                    _field("description", dataset.description),
                    _field("grain", dataset.grain),
                    _field("owner", dataset.owner),
                    _field("target users", dataset.target_users),
                    _field("supported use cases", dataset.supported_use_cases),
                    _field("prohibited use cases", dataset.prohibited_use_cases),
                    _field("freshness SLA", dataset.freshness_sla),
                    _field("refresh cadence", dataset.refresh_cadence),
                    _field("trust signals", dataset.trust_signals),
                    _field("metrics", metrics),
                    _field("entities", entities),
                    _field("dimensions", concepts),
                ],
            )
        )
    return documents
