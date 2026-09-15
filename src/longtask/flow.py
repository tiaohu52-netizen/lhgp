"""Legacy facade: re-exports from canonical lhgp.flow namespace."""

from lhgp.flow import *  # noqa: F403
from lhgp.flow import (  # noqa: F401
    Flow,
    FlowEdge,
    FlowNode,
    FlowSection,
    extract_flow_section,
    list_flow_pages,
    render_excalidraw,
    render_mermaid,
    walk_source,
)
