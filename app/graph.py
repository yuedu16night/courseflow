"""Graph utilities for the prerequisite DAG (semantic dimension).

The course catalog forms a directed acyclic graph over course codes.
These functions are pure and unit-testable: cycle detection on catalog
mutation and Kahn's topological sort used by the curriculum analysis
endpoint.
"""
from __future__ import annotations

from collections import defaultdict, deque


def has_path(graph: dict[str, set[str]], start: str, target: str) -> bool:
    """DFS: is there a directed path start -> ... -> target?"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def creates_cycle(graph: dict[str, set[str]], edge_from: str, edge_to: str) -> bool:
    """Would adding edge edge_from -> edge_to create a cycle?

    A cycle appears iff edge_to already reaches edge_from.
    """
    return has_path(graph, edge_to, edge_from)


def topological_order(graph: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm. Raises ValueError on cycles.

    Edges are prerequisite -> course (you must take the prerequisite
    first). The returned list is a valid curriculum study order.
    """
    indegree: dict[str, int] = defaultdict(int)
    nodes: set[str] = set(graph)
    for src, dsts in graph.items():
        nodes.update(dsts)
        for dst in dsts:
            indegree[dst] += 1
    for n in nodes:
        indegree.setdefault(n, 0)

    queue = deque(sorted(n for n, d in indegree.items() if d == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for dst in sorted(graph.get(node, ())):
            indegree[dst] -= 1
            if indegree[dst] == 0:
                queue.append(dst)
    if len(order) != len(nodes):
        cyclic = sorted(n for n, d in indegree.items() if d > 0)
        raise ValueError(f"Prerequisite graph contains a cycle involving: {cyclic}")
    return order
