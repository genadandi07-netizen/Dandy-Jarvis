"""Declarative description of the acyclic pipeline graph (documentation helper)."""
from __future__ import annotations

NODES = (
    "ingress_guard",
    "contract_validator",
    "state_store.record",
    "egress_dispatcher",
    "state_store.update",
    "n8n_dlq",
)

EDGES = (
    ("ingress_guard", "contract_validator"),
    ("contract_validator", "state_store.record"),
    ("state_store.record", "egress_dispatcher"),
    ("egress_dispatcher", "state_store.update"),
    ("egress_dispatcher", "n8n_dlq"),
)


def build_graph() -> dict:
    return {"nodes": list(NODES), "edges": [list(edge) for edge in EDGES], "acyclic": True}