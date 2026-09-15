"""P6+1 / memory-and-wiki Phase 3: flowgen — call graphs from code or wiki.

The flow module produces diagrams that explain *structure* of a system
(function/class/call topology). It is distinct from memory (which
explains *learned* knowledge) and wiki (which explains *human-curated*
knowledge). Together they form the third leg of the long-term knowledge
stool: structure, learnings, narrative.

Three entry points:

  - :func:`lhgp.flow.ast_walker.walk_source` — build a Flow from a Python
    source string
  - :func:`lhgp.flow.render_mermaid.render_mermaid` — Flow → Mermaid block
  - :func:`lhgp.flow.render_excalidraw.render_excalidraw` — Flow →
    Excalidraw JSON scene

For hand-written diagrams, use :func:`lhgp.flow.wiki_parser.extract_flow_section`
which pulls a ``## flow`` section out of a wiki page (the result is a
raw Mermaid block — re-rendering is a no-op).
"""

from lhgp.flow.ast_walker import Flow, FlowEdge, FlowNode, walk_source
from lhgp.flow.render_excalidraw import render_excalidraw
from lhgp.flow.render_mermaid import render_mermaid
from lhgp.flow.wiki_parser import FlowSection, extract_flow_section, list_flow_pages

__all__ = [
    "Flow",
    "FlowEdge",
    "FlowNode",
    "FlowSection",
    "extract_flow_section",
    "list_flow_pages",
    "render_excalidraw",
    "render_mermaid",
    "walk_source",
]
