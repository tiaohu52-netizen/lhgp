"""Legacy facade: re-exports from canonical lhgp.portfolio namespace."""

from lhgp.portfolio import *  # noqa: F403
from lhgp.portfolio import (  # noqa: F401
    ContractSummary,
    PortfolioSnapshot,
    portfolio_summary,
    trace_contract,
)
