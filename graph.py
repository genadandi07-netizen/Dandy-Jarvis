"""Pipeline graph for the amocrm-1c-resilient-gateway.

The gateway is a single-agent, acyclic pipeline. This module exposes a
light-weight, dependency-free description of the graph so that orchestrators
and the Documentation Weaver can introspect nodes and edges without importing
the network-facing application.
"""

from __future__ import annotations

from typing import Dict, List, TypedDict


class Node(TypedDict):
    id: str
    module: str
    role: str


class Edge(TypedDict):
    source: str
    target: str
    contract: str


NODES: List[Node] = [
    {"id": "ingress_guard", "module": "gateway.ingress_guard", "role": "Шлюз входа и безопасность"},
    {"id": "contract_validator", "module": "gateway.contract_validator", "role": "Валидатор контрактов"},
    {"id": "state_store", "module": "gateway.state_store", "role": "Хранилище состояния и идемпотентности"},
    {"id": "egress_dispatcher", "module": "gateway.egress_dispatcher", "role": "Диспетчер исходящих вызовов"},
]

EDGES: List[Edge] = [
    {"source": "ingress_guard", "target": "contract_validator", "contract": "WebhookPayload (signed, fresh)"},
    {"source": "contract_validator", "target": "state_store", "contract": "validated domain model + idempotency_key"},
    {"source": "state_store", "target": "egress_dispatcher", "contract": "VALIDATED event (BEGIN IMMEDIATE)"},
    {"source": "egress_dispatcher", "target": "state_store", "contract": "DISPATCHED -> DELIVERED / FAILED"},
]

STATE_MACHINE: Dict[str, List[str]] = {
    "RECEIVED": ["VALIDATED"],
    "VALIDATED": ["DISPATCHED"],
    "DISPATCHED": ["DELIVERED", "FAILED"],
    "DELIVERED": [],
    "FAILED": ["VALIDATED"],
}


def build_graph() -> Dict[str, object]:
    """Return the pipeline graph description."""
    return {
        "entrypoint": "ingress_guard",
        "nodes": NODES,
        "edges": EDGES,
        "state_machine": STATE_MACHINE,
        "acyclic": True,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_graph(), ensure_ascii=False, indent=2))