"""EU S8 reusable enclosure: analytic S7 geometry with a closed floor and no labels."""

from pathlib import Path
import json
import hashlib
import cadquery as cq
import front as f
from section_profiles import wire

R = Path(__file__).resolve().parents[1]
D = json.loads((R / "rear-sketches.json").read_text())
OUT = R / "exports"
OUT.mkdir(exist_ok=True)


def extrude(key, z0, z1):
    return cq.Solid.extrudeLinear(
        wire(D[key]["primitives"]), [], cq.Vector(0, 0, z1 - z0)
    ).translate((0, 0, z0))


def port(w, h, r, x, z):
    # Keep the straight throat separate so spline interpolation cannot overshoot it.
    import math

    def profile_at(y, d):
        if abs(w - h) < 1e-8 and abs(2 * r - w) < 1e-8:
            return cq.Wire.makeCircle(r + d, cq.Vector(x, y, z), cq.Vector(0, 1, 0))
        return f.section_wire(
            w + 2 * d, h + 2 * d, r + d, y, z - (h + 2 * d) / 2
        ).translate((x, 0, 0))

    throat = cq.Solid.extrudeLinear(profile_at(54, 0), [], cq.Vector(0, 4, 0))
    wires = []
    for i in range(21):
        angle = math.pi / 2 * i / 20
        y = 58 + 2 * math.sin(angle)
        d = 2 - 2 * math.cos(angle)
        wires.append(profile_at(y, d))
    rounding = cq.Solid.makeLoft(wires, ruled=False)
    outside = cq.Solid.extrudeLinear(profile_at(60, 2), [], cq.Vector(0, 1, 0))
    return f.fuse(throat, rounding, outside)


def rootpost(x, y):
    # R10 concave root blend around a 6 mm post, matching the legacy support.
    t = (
        cq.Workplane("XZ")
        .moveTo(0, 2.5)
        .lineTo(13, 2.5)
        .threePointArc((5.928932188, 5.428932188), (3, 12.5))
        .lineTo(0, 12.5)
        .close()
        .revolve()
        .val()
        .translate((x, y, 0))
    )
    drill = f.cylinder(x, y, 7.5, 12.6, 1.2).fuse(
        cq.Solid.makeCone(0, 1.2, 0.7210331, cq.Vector(x, y, 6.7789669))
    )
    return t.cut(drill)


def rear():
    outer = (
        cq.Workplane("XY")
        .rect(197, 120)
        .extrude(82)
        .edges("|Z")
        .fillet(8)
        .faces(">Z or <Z")
        .edges()
        .fillet(3)
        .val()
    )
    crop = f.box([-110, -13, -1], [110, 65, 90])
    base = f.fuse(
        outer.intersect(crop).intersect(f.box([-110, -65, 0], [110, 65, 2.5])),
        extrude("base-wall", 2.5, 15.5),
        extrude("base-skirt", 15.5, 20.5),
    )
    lid = f.fuse(
        extrude("lid-lower", 15.5, 20.5),
        extrude("lid-wall", 20.5, 79.5),
        outer.intersect(crop).intersect(f.box([-110, -65, 79.5], [110, 65, 83])),
    )
    base = base.intersect(outer)
    lid = lid.intersect(outer)
    for x in [8.5, 66.5]:
        for y in [2.2, 51.2]:
            base = f.fuse(base, rootpost(x, y).intersect(outer))
    for x in [-90.739311218, 90.739311218]:
        y = 52.239311218
        base = base.cut(f.cylinder(x, y, -1, 7, 3), f.cylinder(x, y, 7, 20.6, 1.4))
        lid = lid.cut(
            f.cylinder(x, y, 20.49, 26, 1.2),
            cq.Solid.makeCone(1.2, 0, 0.721033096, cq.Vector(x, y, 26)),
        )
    power = port(14.94, 7, 2, 58.814, 15.5)
    auxiliary = port(7.7, 7.7, 3.85, 16, 16.7)
    # Interior horizontal recess: R1.25 roundovers in YZ, flat ends in X.
    blind = (
        cq.Workplane("YZ")
        .moveTo(55.4, 13.1)
        .lineTo(55.5, 13.1)
        .threePointArc((56.383883476, 13.466116524), (56.75, 14.35))
        .lineTo(56.75, 16.85)
        .threePointArc((56.383883476, 17.733883476), (55.5, 18.1))
        .lineTo(55.4, 18.1)
        .close()
        .extrude(21.5)
        .val()
        .translate((26.5, 0, 0))
    )
    base = base.cut(
        power,
        auxiliary,
        blind,
        f.box([51.344, 54, 14], [66.284, 57.41, 21]),
        f.box([12.15, 54, 16.7], [19.85, 57.41, 21]),
    )
    lid = lid.cut(power, auxiliary, blind)
    lid = lid.cut(
        f.cylinder(-43, 0, 79.49, 83, 43.5), f.cylinder(-43, 43.5, 79.49, 83, 3)
    )
    groove_path = f.rounded_wire(54.5, 70, 4.75).translate((52.75, -3.25, 82))
    edge = groove_path.Edges()[0]
    groove = cq.Solid.sweep(
        cq.Wire.makeCircle(0.25, edge.startPoint(), edge.tangentAt(0)),
        [],
        groove_path,
        True,
        False,
    )
    lid = lid.cut(groove)
    skin = f.scale(lid, x=1 + 0.25 / 98.5).cut(f.scale(outer, x=1 - 0.02 / 98.5))
    lid = f.fuse(lid, skin)
    return base.clean(), lid.clean()


def volume(shape):
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, p, 1e-8, True, False)
    return p.Mass()


def export(name, shape):
    inf = f.info(shape)
    print(name, inf, flush=True)
    assert inf["valid"] and inf["solids"] == 1, (name, inf)
    cq.exporters.export(shape, str(OUT / (name + ".step")))
    rt = cq.importers.importStep(str(OUT / (name + ".step"))).val()
    assert rt.isValid() and len(rt.Solids()) == 1
    inf["precise_volume_mm3"] = volume(shape)
    inf["step_roundtrip_volume_mm3"] = volume(rt)
    print(
        "roundtrip",
        name,
        inf["precise_volume_mm3"],
        inf["step_roundtrip_volume_mm3"],
        flush=True,
    )
    assert abs(inf["step_roundtrip_volume_mm3"] - inf["precise_volume_mm3"]) < 0.01
    if not name.endswith("-print"):
        return inf
    shape.exportStl(
        str(OUT / (name + ".stl")),
        tolerance=0.001,
        angularTolerance=0.03,
        relative=False,
    )
    import trimesh

    mesh = trimesh.load(OUT / (name + ".stl"))
    before = len(mesh.faces)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    assert (
        mesh.is_watertight
        and mesh.is_winding_consistent
        and len(mesh.split(only_watertight=False)) == 1
    ), (name, "mesh")
    mesh.export(OUT / (name + ".stl"))
    inf["zero_area_triangles_removed"] = before - len(mesh.faces)
    return inf


def build_parts():
    rearbase, rearlid = rear()
    front, _ = f.build()
    return {
        "base": f.fuse(front["base"], rearbase),
        "lid": f.fuse(front["lid"], rearlid),
        "speaker": front["speaker-reference"],
    }


def main():
    parts = build_parts()
    report = {
        "revision": "EU S8",
        "units": "mm",
        "engraving": False,
        "parts": {},
        "intersections_mm3": {},
    }
    for role in ["base", "lid"]:
        report["parts"][role] = export(role, parts[role])
        printable = parts[role]
        if role == "lid":
            printable = printable.rotate((0, 0, 0), (1, 0, 0), 180).translate(
                (0, 0, 82)
            )
        report["parts"][role + "-print"] = export(role + "-print", printable)
    for lift in [0, 0.5, 2, 5, 10, 20, 40, 80]:
        for role in ["base", "speaker"]:
            report["intersections_mm3"][f"lid_lift_{lift}/{role}"] = max(
                0, volume(parts["lid"].translate((0, 0, lift)).intersect(parts[role]))
            )
    report["intersections_mm3"]["base/speaker"] = max(
        0, volume(parts["base"].intersect(parts["speaker"]))
    )
    assert all(v < 0.005 for v in report["intersections_mm3"].values()), (
        "Inspect interference"
    )
    assy = cq.Assembly(name="EU-S8-enclosure")
    assy.add(parts["base"], name="Base", color=cq.Color(0.64, 0.76, 0.79))
    assy.add(parts["lid"], name="Lid", color=cq.Color(0.94, 0.93, 0.89))
    assy.export(str(OUT / "assembly.step"))
    report["files"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(OUT.iterdir())
        if p.suffix in [".step", ".stl"]
    }
    report["source_files"] = {
        str(p.relative_to(R)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            *sorted((R / "source").glob("*.py")),
            R / "parameters.json",
            R / "rear-sketches.json",
        ]
    }
    (R / "verification/geometry.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["intersections_mm3"], flush=True)


if __name__ == "__main__":
    main()
