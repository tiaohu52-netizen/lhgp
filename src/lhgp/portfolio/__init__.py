"""P6: multi-contract management view (portfolio).

A single lhgp installation typically has many contracts running in
parallel. The CLI/MCP need a single surface that summarises the
state of all of them at once, surfaces risk, and traces one contract
end-to-end.

  - :func:`portfolio_summary` aggregates per-contract rows from
    ``contracts`` into a flat list with lifecycle/deadline/acceptance
    snapshot.
  - :func:`trace_contract` produces a per-contract timeline: events
    joined with the latest evaluation + diff, for debugging "why did
    this contract close this way?".
"""

from lhgp.portfolio.summary import portfolio_summary
from lhgp.portfolio.tracking import trace_contract
from lhgp.portfolio.types import ContractSummary, PortfolioSnapshot

__all__ = [
    "ContractSummary",
    "PortfolioSnapshot",
    "portfolio_summary",
    "trace_contract",
]
