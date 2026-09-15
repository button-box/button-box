"""Analytic speaker-facing features for the European ADELGO EL001 enclosure."""

from pathlib import Path
import json
import cadquery as cq

ROOT = Path(__file__).resolve().parents[1]
P = json.loads((ROOT / "parameters.json").read_text())
S, F, H, R, M = (P[k] for k in ["speaker", "fit", "shell", "retainers", "mic"])


def box(lo, hi):
    return cq.Solid.makeBox(*[b - a for a, b in zip(lo, hi)], cq.Vector(*lo))


def fuse(*shapes):
    return shapes[0].fuse(*shapes[1:]).clean()


def rounded_wire(w, h, r):
    wire = cq.Workplane("XY").rect(w, h).val()
    return wire.fillet2D(r, wire.Vertices())


def section_wire(w, h, r, y, bottom):
    return (
        rounded_wire(w, h, r)
        .rotate((0, 0, 0), (1, 0, 0), 90)
        .translate((0, y, bottom + h / 2))
    )


def profile(w, h, r, y0, y1, bottom):
    return cq.Solid.extrudeLinear(
        section_wire(w, h, r, y1, bottom), [], cq.Vector(0, y0 - y1, 0)
    )


def xy_prism(points, z0, z1):
    w = cq.Workplane("XY").polyline(points).close().val()
    return cq.Solid.extrudeLinear(w, [], cq.Vector(0, 0, z1 - z0)).translate((0, 0, z0))


def x_prism(points, x0, x1):
    w = cq.Wire.makePolygon([cq.Vector(x0, y, z) for y, z in points], close=True)
    return cq.Solid.extrudeLinear(w, [], cq.Vector(x1 - x0, 0, 0))


def scale(shape, x=1, y=1, z=1, oz=0):
    return shape.transformGeometry(
        cq.Matrix([[x, 0, 0, 0], [0, y, 0, 0], [0, 0, z, oz], [0, 0, 0, 1]])
    )


def cylinder(x, y, z0, z1, r):
    return cq.Solid.makeCylinder(r, z1 - z0, cq.Vector(x, y, z0))


def build():
    outer = (
        cq.Workplane("XY")
        .rect(H["base_width"], H["depth"])
        .extrude(H["height"])
        .edges("|Z")
        .fillet(H["outer_corner_radius"])
        .faces(">Z or <Z")
        .edges()
        .fillet(H["edge_radius"])
        .val()
    )
    clip = box([-110, -65, -1], [110, H["crop_rear_y"], 90])
    # The existing mating skirt follows a rounded inner envelope, not the outer wall.
    inner_wire = rounded_wire(192, 115, 4)
    joint = cq.Solid.extrudeLinear(inner_wire, [], cq.Vector(0, 0, 90))
    xf = H["legacy_side_front_x"]
    yt = H["legacy_side_transition_y"]
    xr = H["legacy_side_rear_x"]
    cavity_points = [
        (-xf, H["inner_front_y"]),
        (xf, H["inner_front_y"]),
        (xf, -56.2),
        (xr, yt),
        (96, yt),
        (96, 65),
        (-96, 65),
        (-96, yt),
        (-xr, yt),
        (-xf, -56.2),
    ]
    cavity = xy_prism(cavity_points, 0, H["roof_under_z"])
    cap = outer.intersect(box([-110, -65, H["seam_z"]], [110, 65, 90])).cut(cavity)
    skirt = joint.intersect(
        box([-110, -56.2, H["skirt_bottom_z"]], [110, yt, H["seam_z"] + 0.01])
    ).cut(cavity)
    cap = fuse(cap, skirt)
    # Base cavity preserves the R4 joint and flat 2.5 mm speaker seat.
    base_cavity = fuse(joint, box([-xf, -57.1, 0], [xf, 65, 90]))
    base_cavity = base_cavity.intersect(box([-110, -65, S["floor_z"]], [110, 65, 90]))
    base = outer.intersect(box([-110, -65, 0], [110, 65, H["seam_z"]])).cut(base_cavity)
    inner_front = S["front_y"] - F["front_clearance"]
    aperture = profile(
        S["front_width"] - 2 * F["front_overlap"],
        S["front_height"] - 2 * F["front_overlap"],
        S["corner_radius_trial"] - F["front_overlap"],
        -62,
        -55,
        S["floor_z"] + F["front_overlap"],
    )
    # Remove the original opening before constructing the integrated retaining face.
    cap = cap.cut(aperture)
    front = outer.intersect(box([-110, -61, 0], [110, inner_front, 62]))
    cap = fuse(
        cap, front.cut(aperture).intersect(box([-110, -65, H["seam_z"]], [110, 65, 90]))
    )
    if P.get("printable_front_transition", False):
        # Removing the long holder exposes this 0.8mm step in roof-down printing.
        # Add a 45-degree transition above the speaker-contact area.
        d = inner_front - H["inner_front_y"]
        transition = x_prism(
            [(H["inner_front_y"], 62), (inner_front, 62), (H["inner_front_y"], 62 + d)],
            -xf,
            xf,
        ).intersect(outer)
        cap = fuse(cap, transition)
    seam = H["seam_z"]
    old = S["floor_z"] + F["front_overlap"]
    new = S["floor_z"] + F["bottom_overlap"]
    factor = (seam - new) / (seam - old)
    lower_aperture = scale(aperture, z=factor, oz=seam * (1 - factor))
    # Only the lower profile is scaled; the upper half keeps the tested aperture.
    base = base.cut(aperture)
    base = fuse(
        base, front.cut(lower_aperture).intersect(box([-110, -65, 0], [110, 65, seam]))
    )
    for a, b in [(-98.5, -F["end_span"] / 2), (F["end_span"] / 2, 98.5)]:
        cap = fuse(
            cap, outer.intersect(box([a, -60, 21], [b, -18.4, H["roof_under_z"]]))
        )
    z = S["floor_z"] + S["front_height"] + F["top_clearance"]
    cap = fuse(
        cap,
        box(
            [-R["ledge_half_width"], R["ledge_front_y"], z],
            [R["ledge_half_width"], R["ledge_rear_y"], z + R["ledge_thickness"]],
        ),
        x_prism(
            [
                (R["ledge_front_y"], z + 3),
                (R["ledge_rear_y"], z + 3),
                (R["ledge_front_y"], z + 3 + R["ledge_ramp_height"]),
            ],
            -R["ledge_half_width"],
            R["ledge_half_width"],
        ),
    )
    rear_face = inner_front + F["pocket_depth"]
    for a, b in R["rear_x_ranges"]:
        base = fuse(
            base,
            box(
                [a, rear_face, 2.3],
                [b, rear_face + R["rear_thickness"], R["rear_top_z"]],
            ),
            x_prism(
                [
                    (rear_face + 2.9, 2.3),
                    (H["crop_rear_y"], 2.3),
                    (rear_face + 2.9, 20.5),
                ],
                a,
                b,
            ),
        )
    # Roof details retained from the current section: partial button opening and mic mount.
    cap = cap.cut(cylinder(-43, 0, 79.49, 83, 43.5), cylinder(-43, -43.5, 79.49, 83, 3))
    for x, d in zip(M["post_x"], M["post_diameters"]):
        post = cylinder(x, M["post_y"], M["post_z"], 79.55, d / 2)
        collar = cq.Solid.makeCone(0, 4.5, 2.5, cq.Vector(x, M["post_y"], 77.05))
        cap = fuse(cap, post, collar)
        cap = cap.cut(
            cylinder(
                x,
                M["post_y"],
                M["post_z"] - 0.01,
                M["post_z"] + M["pilot_depth"],
                M["pilot_diameter"] / 2,
            )
        )
    for y, length in zip(M["slot_y"], M["slot_lengths"]):
        slot = (
            cq.Workplane("XY")
            .slot2D(length, M["slot_width"])
            .extrude(4)
            .val()
            .translate((M["slot_center_x"], y, 79))
        )
        cap = cap.cut(slot)
    # Shallow roof outline: 0.25 mm round groove, with front corner radius 4.75.
    groove_path = rounded_wire(54.5, 70, 4.75).translate((52.75, -3.25, 82))
    # Sweep starts on the first edge to avoid a guessed workplane orientation.
    edge = groove_path.Edges()[0]
    start = edge.startPoint()
    tangent = edge.tangentAt(0)
    circle = cq.Wire.makeCircle(0.25, start, tangent)
    groove = cq.Solid.sweep(circle, [], groove_path, True, False)
    cap = cap.cut(groove)
    cap = cap.intersect(clip)
    base = base.intersect(clip)
    # Preserve V7 exterior-only compensation; never scale the speaker pocket.
    factor = 1 + H["lid_extra_each_side"] / (H["base_width"] / 2)
    skin = scale(cap, x=factor).cut(scale(outer, x=1 - 0.02 / (H["base_width"] / 2)))
    skin = skin.intersect(box([-110, -65, seam], [110, H["crop_rear_y"], 90]))
    cap = fuse(cap, skin)
    # Reference housing has a measured front/rear size and an assumed centred taper.
    front_rim = profile(
        S["front_width"],
        S["front_height"],
        S["corner_radius_trial"],
        S["front_y"],
        S["front_y"] + S["front_rim_depth"],
        S["floor_z"],
    )
    w1 = section_wire(
        S["front_width"],
        S["front_height"],
        S["corner_radius_trial"],
        S["front_y"] + S["front_rim_depth"],
        S["floor_z"],
    )
    w2 = section_wire(
        S["rear_width"],
        S["rear_height"],
        S["corner_radius_trial"],
        S["front_y"] + S["body_depth"],
        S["floor_z"] + 1,
    )
    body = fuse(front_rim, cq.Solid.makeLoft([w1, w2], ruled=True))
    gap = F["saddle_clearance"]
    clearance = cq.Solid.makeLoft(
        [
            section_wire(
                S["front_width"] + 2 * gap,
                S["front_height"] + 2 * gap,
                S["corner_radius_trial"] + gap,
                S["front_y"] + 1,
                S["floor_z"] - gap,
            ),
            section_wire(
                S["rear_width"] + 2 * gap,
                S["rear_height"] + 2 * gap,
                S["corner_radius_trial"] + gap,
                S["front_y"] + S["body_depth"],
                S["floor_z"] + 1 - gap,
            ),
        ],
        ruled=True,
    )
    saddles = []
    for a, b in R["saddle_x_ranges"]:
        for y in [S["front_y"] + 5, S["front_y"] + S["body_depth"] - 9]:
            saddles.append(
                box([a, y, 2.3], [b, y + R["saddle_depth"], R["saddle_top_z"]])
                .cut(clearance)
                .intersect(outer)
            )
    base = fuse(base, *saddles)
    cable = box(
        [R["cable_slot_x"][0], S["front_y"] + S["body_depth"] - 5, -1],
        [R["cable_slot_x"][1], -12, R["cable_slot_top_z"]],
    )
    grille = profile(
        183,
        52.2,
        S["corner_radius_trial"] - 1.8,
        S["front_y"] - S["grille_projection"],
        S["front_y"] + 0.1,
        S["floor_z"] + 2,
    )
    grille_clear = profile(
        183.4,
        52.6,
        S["corner_radius_trial"] - 1.6,
        S["front_y"] - 1.7,
        S["front_y"] + 0.2,
        S["floor_z"] + 1.8,
    )
    # Optional floor closure keeps the existing cable space above the seat.
    if P.get("close_floor_opening", False):
        cable = cable.intersect(box([-110, -65, S["floor_z"]], [110, 65, 90]))
    base = base.cut(cable, grille_clear).clean()
    speaker = fuse(body, grille)
    return {
        "base": base,
        "lid": cap.clean(),
        "speaker-reference": speaker,
        "grille-reference": grille,
    }, {"cable": cable, "saddles": fuse(*saddles)}


def info(shape):
    b = shape.BoundingBox()
    return {
        "valid": shape.isValid(),
        "solids": len(shape.Solids()),
        "volume_mm3": shape.Volume(),
        "bounds_mm": [[b.xmin, b.ymin, b.zmin], [b.xmax, b.ymax, b.zmax]],
        "faces": len(shape.Faces()),
        "surface_types": dict(
            __import__("collections").Counter(f.geomType() for f in shape.Faces())
        ),
    }
