"""Build analytic sketch wires from the stored line and arc primitives."""

import cadquery as cq


def wire(data):
    edges = []
    for s in data:
        a = cq.Vector(*s["start"], 0)
        b = cq.Vector(*s["end"], 0)
        if s["type"] == "line":
            edges.append(cq.Edge.makeLine(a, b))
        else:
            edges.append(cq.Edge.makeThreePointArc(a, cq.Vector(*s["mid"], 0), b))
    return cq.Wire.assembleEdges(edges)
