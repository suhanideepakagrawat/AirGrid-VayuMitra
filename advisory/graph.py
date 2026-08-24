"""A typed knowledge graph over the entities this system already reasons about.

Why this exists. The product's central promise is that every number carries an evidence
chain: this ward, that dominant source, this pollutant signature, therefore that team and
that action, and separately that health guidance for that persona. Until now the chain
was implicit, spread across a CSV, a scoring script and a rendering component. Nobody
could ask the system to *show* the chain.

This module makes it explicit. Nodes and typed edges are built from data the pipeline
already produces - ward forecasts, source attribution, the enforcement queue, the CPCB
band table, the persona rules, the GRAP ladder - and `explain_ward()` walks the graph to
return the chain end to end.

It is deliberately a plain in-memory property graph, not a triple store or a graph
database. The graph has a few thousand nodes, is rebuilt from source data in
milliseconds, and is read far more than it is written. A database here would add an
operational dependency and answer the same questions no faster.

Nothing else imports this module. It is additive: if it fails to build, every existing
surface behaves exactly as before.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from advisory import data as data_mod
from advisory import sources as sources_mod
from advisory.health_bands import band_for_aqi
from advisory.personas import PERSONAS

# ---------------------------------------------------------------------------
# Domain relations. These are the edges the product's own explanations rely on,
# written down once instead of being reimplemented per surface.
# ---------------------------------------------------------------------------

#: pollutant -> the source category its elevation indicates. This is the
#: fingerprint rule, and the direction matters: a pollutant INDICATES a source,
#: a source EMITS a pollutant.
POLLUTANT_INDICATES: dict[str, str] = {
    "no2": "traffic",
    "so2": "industry",
    "pm10": "construction",
    "pm25": "burning",
}

SOURCE_EMITS: dict[str, list[str]] = {
    "traffic": ["no2", "pm25"],
    "industry": ["so2", "pm25"],
    "construction": ["pm10"],
    "burning": ["pm25", "pm10"],
}

SOURCE_LABEL = {
    "traffic": "Traffic / roads",
    "industry": "Industry",
    "construction": "Construction dust",
    "burning": "Regional biomass burning",
}

#: CPCB band -> GRAP stage invoked at that band, per CAQM.
BAND_TRIGGERS_GRAP: dict[str, str] = {
    "poor": "Stage I",
    "very_poor": "Stage II",
    "severe": "Stage III",
}

POLLUTANT_LABEL = {
    "pm25": "PM2.5", "pm10": "PM10", "no2": "NO₂",
    "so2": "SO₂", "o3": "O₃",
}


@dataclass
class Node:
    id: str
    kind: str
    label: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    rel: str
    dst: str
    props: dict[str, Any] = field(default_factory=dict)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._out: dict[str, list[Edge]] = {}

    def add_node(self, node_id: str, kind: str, label: str, **props: Any) -> str:
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id, kind, label, dict(props))
        elif props:
            self.nodes[node_id].props.update(props)
        return node_id

    def add_edge(self, src: str, rel: str, dst: str, **props: Any) -> None:
        # Both endpoints must exist. Silently creating a node from an edge is how
        # graphs quietly fill with typos.
        if src not in self.nodes or dst not in self.nodes:
            return
        e = Edge(src, rel, dst, dict(props))
        self.edges.append(e)
        self._out.setdefault(src, []).append(e)

    def out(self, node_id: str, rel: str | None = None) -> list[Edge]:
        got = self._out.get(node_id, [])
        return [e for e in got if rel is None or e.rel == rel] if got else []

    def neighbours(self, node_id: str, rel: str) -> list[Node]:
        return [self.nodes[e.dst] for e in self.out(node_id, rel) if e.dst in self.nodes]

    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        rels: dict[str, int] = {}
        for n in self.nodes.values():
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        for e in self.edges:
            rels[e.rel] = rels.get(e.rel, 0) + 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "node_kinds": dict(sorted(kinds.items(), key=lambda x: -x[1])),
            "relations": dict(sorted(rels.items(), key=lambda x: -x[1])),
        }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _build() -> Graph:
    g = Graph()

    # -- the fixed vocabulary: pollutants, sources, bands, personas, GRAP -----
    for key, label in POLLUTANT_LABEL.items():
        g.add_node(f"pol:{key}", "Pollutant", label)

    for key, label in SOURCE_LABEL.items():
        g.add_node(f"src:{key}", "SourceCategory", label)

    for src, pols in SOURCE_EMITS.items():
        for pol in pols:
            g.add_edge(f"src:{src}", "EMITS", f"pol:{pol}")
    for pol, src in POLLUTANT_INDICATES.items():
        g.add_edge(f"pol:{pol}", "INDICATES", f"src:{src}")

    for aqi in (25, 75, 150, 250, 350, 450):
        band = band_for_aqi(aqi)
        g.add_node(f"band:{band.key}", "Band", band.label_en,
                   lower=band.lower, upper=band.upper, note=band.note_en)

    for stage in ("Stage I", "Stage II", "Stage III", "Stage IV"):
        g.add_node(f"grap:{stage}", "GrapStage", f"GRAP {stage}")
    for band_key, stage in BAND_TRIGGERS_GRAP.items():
        g.add_edge(f"band:{band_key}", "TRIGGERS", f"grap:{stage}")

    for key, persona in PERSONAS.items():
        g.add_node(f"persona:{key}", "Persona", persona.label_en,
                   extra_sensitivity=persona.extra_sensitivity)
        # A persona is escalated relative to the general public: the edge carries
        # how many bands, which is the whole rule in one number.
        if persona.extra_sensitivity:
            g.add_edge(f"persona:{key}", "ESCALATED_BY",
                       f"persona:general", bands=persona.extra_sensitivity)

    for s in sources_mod.all_sources():
        g.add_node(f"auth:{s['id']}", "Authority", s.get("title", s["id"]),
                   publisher=s.get("publisher"), year=s.get("year"), url=s.get("url"))

    # -- wards, their band, and their attributed sources ---------------------
    try:
        zones = data_mod.load_zones()
    except Exception:
        zones = ()

    for z in zones:
        zid = f"ward:{z['zone_id']}"
        # load_zones() names this current_aqi; there is no "aqi" key and reading one
        # silently produced a graph where every ward sat in the Good band.
        aqi = float(z.get("current_aqi") or 0)
        band = band_for_aqi(aqi)
        g.add_node(zid, "Ward", z.get("name", z["zone_id"]),
                   aqi=round(aqi), band=band.key,
                   lat=z.get("lat"), lon=z.get("lon"),
                   confidence=z.get("confidence"))
        g.add_edge(zid, "IN_BAND", f"band:{band.key}", aqi=round(aqi))

        srcs = z.get("sources") or {}
        for key, share in srcs.items():
            node = f"src:{key}"
            if node in g.nodes and share:
                g.add_edge(zid, "ATTRIBUTED_TO", node, share_pct=round(float(share), 1))

    # -- the dispatch queue: targets, teams, actions --------------------------
    targets = _load_targets()
    for t in targets:
        tid = f"target:{t.get('source_id') or t.get('rank')}"
        g.add_node(tid, "EnforcementTarget", t.get("source_name") or tid,
                   rank=t.get("rank"), priority=t.get("priority"),
                   peak_aqi=t.get("peak_aqi"), confidence=t.get("confidence"),
                   basis=t.get("basis"), lat=t.get("latitude"), lon=t.get("longitude"))

        team = (t.get("recommended_team") or "").strip()
        if team:
            g.add_node(f"team:{team}", "Team", team)
            g.add_edge(tid, "HANDLED_BY", f"team:{team}")

        action = (t.get("action") or "").strip()
        if action:
            g.add_node(f"action:{action[:60]}", "Action", action)
            g.add_edge(f"team:{team}" if team else tid, "PERFORMS",
                       f"action:{action[:60]}")

        # Map the target's declared type onto our source vocabulary.
        stype = (t.get("source_type") or "").lower()
        cat = ("construction" if "construct" in stype or "dust" in stype
               else "industry" if "indust" in stype
               else "traffic" if ("road" in stype or "inter" in stype or "junction" in stype)
               else None)
        if cat:
            g.add_edge(tid, "OF_CATEGORY", f"src:{cat}")

        ward_no = str(t.get("Ward_No") or "").replace(".0", "").strip()
        if ward_no:
            wid = f"ward:W{ward_no}"
            if wid in g.nodes:
                g.add_edge(wid, "HOSTS", tid, rank=t.get("rank"))

    return g


def _load_targets() -> list[dict]:
    """The enforcement queue, read the same way the API reads it."""
    import csv
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = root / "data" / "enforcement_targets_v2.csv"
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


_lock = threading.Lock()
_graph: Graph | None = None


def graph() -> Graph:
    global _graph
    with _lock:
        if _graph is None:
            _graph = _build()
        return _graph


def rebuild() -> Graph:
    """Called after a forecast refresh, so the graph never lags the data."""
    global _graph
    with _lock:
        _graph = _build()
        return _graph


def stats() -> dict:
    s = graph().stats()
    s["method"] = ("in-memory typed property graph, rebuilt from pipeline output on "
                   "every forecast refresh")
    return s


# ---------------------------------------------------------------------------
# Traversal: the evidence chain
# ---------------------------------------------------------------------------

def explain_ward(zone_id: str, persona_key: str = "general") -> dict | None:
    """Walk the graph from a ward to everything that follows from it.

    Two chains leave a ward and they answer different questions:
      health      ward -> band -> GRAP stage, and band + persona -> guidance
      enforcement ward -> dominant source -> pollutant evidence, and
                  ward -> hosted target -> team -> action
    """
    g = graph()
    wid = f"ward:{zone_id}"
    node = g.nodes.get(wid)
    if node is None:
        return None

    band_nodes = g.neighbours(wid, "IN_BAND")
    band = band_nodes[0] if band_nodes else None
    grap = g.neighbours(band.id, "TRIGGERS") if band else []

    attributed = sorted(
        ({"source": g.nodes[e.dst].label,
          "key": e.dst.split(":", 1)[1],
          "share_pct": e.props.get("share_pct", 0)}
         for e in g.out(wid, "ATTRIBUTED_TO")),
        key=lambda x: -x["share_pct"],
    )
    dominant = attributed[0] if attributed else None

    evidence: list[dict] = []
    if dominant:
        skey = f"src:{dominant['key']}"
        for pol in g.neighbours(skey, "EMITS"):
            pkey = pol.id.split(":", 1)[1]
            evidence.append({
                "pollutant": pol.label,
                "relation": ("elevation of this pollutant indicates "
                             f"{SOURCE_LABEL.get(POLLUTANT_INDICATES.get(pkey, ''), '')}".strip())
                if POLLUTANT_INDICATES.get(pkey) == dominant["key"]
                else f"emitted by {dominant['source'].lower()}",
            })

    targets = []
    for e in g.out(wid, "HOSTS"):
        t = g.nodes.get(e.dst)
        if not t:
            continue
        teams = g.neighbours(t.id, "HANDLED_BY")
        team = teams[0] if teams else None
        actions = g.neighbours(team.id, "PERFORMS") if team else []
        targets.append({
            "target": t.label,
            "rank": t.props.get("rank"),
            "priority": t.props.get("priority"),
            "basis": t.props.get("basis"),
            "confidence": t.props.get("confidence"),
            "team": team.label if team else None,
            "action": actions[0].label if actions else None,
        })
    targets.sort(key=lambda x: int(x["rank"] or 999))

    persona = PERSONAS.get(persona_key) or PERSONAS["general"]

    return {
        "ward": node.label,
        "zone_id": zone_id,
        "aqi": node.props.get("aqi"),
        "band": {
            "key": band.id.split(":", 1)[1] if band else None,
            "label": band.label if band else None,
            "range": f"{band.props.get('lower')}-{band.props.get('upper')}" if band else None,
            "note": band.props.get("note") if band else None,
        },
        "grap_stage": grap[0].label if grap else None,
        "persona": {
            "key": persona.key,
            "label": persona.label_en,
            "escalated_bands": persona.extra_sensitivity,
        },
        "attribution": attributed,
        "dominant_source": dominant,
        "pollutant_evidence": evidence,
        "enforcement": targets,
        "chain": _chain_sentence(node, band, grap, dominant, targets, persona),
    }


def _chain_sentence(node, band, grap, dominant, targets, persona) -> str:
    """The traversal, rendered as the sentence a human would say."""
    bits = [f"{node.label} is at AQI {node.props.get('aqi')}"]
    if band:
        bits.append(f"which is the CPCB {band.label} band")
    if persona.extra_sensitivity:
        bits.append(f"and guidance for {persona.label_en.lower()} is taken from a band "
                    f"{persona.extra_sensitivity} step(s) more severe")
    if grap:
        bits.append(f"this band invokes {grap[0].label}")
    if dominant:
        bits.append(f"its dominant source is {dominant['source'].lower()} "
                    f"at {dominant['share_pct']}%")
    if targets:
        t = targets[0]
        bits.append(f"and the queue puts {t['target']} in it at rank {t['rank']}, "
                    f"for {t['team']}")
    return "; ".join(bits) + "."


def subgraph(zone_id: str) -> dict:
    """Nodes and edges within one hop of a ward, for rendering or inspection."""
    g = graph()
    wid = f"ward:{zone_id}"
    if wid not in g.nodes:
        return {"nodes": [], "edges": []}
    keep = {wid}
    edges = []
    for e in g.out(wid):
        keep.add(e.dst)
        edges.append(e)
        for e2 in g.out(e.dst):
            keep.add(e2.dst)
            edges.append(e2)
    return {
        "nodes": [{"id": n, "kind": g.nodes[n].kind, "label": g.nodes[n].label}
                  for n in keep if n in g.nodes],
        "edges": [{"src": e.src, "rel": e.rel, "dst": e.dst, **e.props} for e in edges],
    }
