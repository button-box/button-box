#!/usr/bin/env python3
"""Widen the US Button Box speaker pocket into the eu-el001 model.

Read hardware/models/us/enclosure/{top,bottom}.stl and write
hardware/models/eu-el001/enclosure/{top,bottom}.stl.

The committed eu-el001 STLs already include this change. The script is the
reproducible mesh operation: split each enclosure half at x=±88 mm
(the front-corner centers) and spread the end caps by 2.5 mm so the
front window and inner pocket clear a 187 mm speaker.

Do not run it again on the widened files.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
import trimesh


HINGE_MM = 88.0
DELTA_MM = 2.5
MODELS_DIR = Path(__file__).resolve().parents[2] / "hardware" / "models"
SOURCE_DIR = MODELS_DIR / "us" / "enclosure"
OUTPUT_DIR = MODELS_DIR / "eu-el001" / "enclosure"
PARTS = ("bottom.stl", "top.stl")


def _directed_boundary_edges(faces: np.ndarray) -> np.ndarray:
    edges = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    key = np.sort(edges, axis=1)
    unique, counts = np.unique(key, axis=0, return_counts=True)
    boundary_keys = {tuple(edge) for edge in unique[counts == 1]}
    directed = [edge for edge in edges if tuple(sorted(edge.tolist())) in boundary_keys]
    return np.asarray(directed, dtype=np.int64)


def extrude_cut_boundary(sliced: trimesh.Trimesh, plane_x: float, delta: float) -> trimesh.Trimesh:
    vertices = sliced.vertices
    faces = sliced.faces
    boundary = _directed_boundary_edges(faces)
    on_plane = np.all(np.abs(vertices[boundary][:, :, 0] - plane_x) < 1e-3, axis=1)
    boundary = boundary[on_plane]
    if len(boundary) == 0:
        raise RuntimeError(f"no cut-boundary edges at x={plane_x}")

    offset = np.array([delta, 0.0, 0.0], dtype=np.float64)
    quads = []
    for start, end in boundary:
        p0 = vertices[start]
        p1 = vertices[end]
        quads.append((p0, p1, p1 + offset))
        quads.append((p0, p1 + offset, p0 + offset))
    bridge_vertices = np.array([point for triangle in quads for point in triangle])
    bridge_faces = np.arange(len(bridge_vertices)).reshape(-1, 3)
    return trimesh.Trimesh(vertices=bridge_vertices, faces=bridge_faces, process=False)


def split_and_spread(mesh: trimesh.Trimesh, hinge: float = HINGE_MM, delta: float = DELTA_MM) -> trimesh.Trimesh:
    left = mesh.slice_plane(
        plane_origin=[-hinge, 0.0, 0.0], plane_normal=[-1.0, 0.0, 0.0], cap=False
    )
    right = mesh.slice_plane(
        plane_origin=[hinge, 0.0, 0.0], plane_normal=[1.0, 0.0, 0.0], cap=False
    )
    mid = mesh.slice_plane(
        plane_origin=[hinge, 0.0, 0.0], plane_normal=[-1.0, 0.0, 0.0], cap=False
    )
    mid = mid.slice_plane(
        plane_origin=[-hinge, 0.0, 0.0], plane_normal=[1.0, 0.0, 0.0], cap=False
    )
    bridge_right = extrude_cut_boundary(right, hinge, delta)
    bridge_left = extrude_cut_boundary(left, -hinge, -delta)
    left.apply_translation([-delta, 0.0, 0.0])
    right.apply_translation([delta, 0.0, 0.0])

    combined = trimesh.util.concatenate([mid, left, right, bridge_right, bridge_left])
    combined.merge_vertices(merge_tex=False, merge_norm=False)
    combined.update_faces(combined.unique_faces())
    combined.update_faces(combined.nondegenerate_faces())
    trimesh.repair.fix_normals(combined)
    if not combined.is_watertight:
        combined.fill_holes()
        trimesh.repair.fix_normals(combined)
    return combined


def write_binary_stl(mesh: trimesh.Trimesh, path: Path, header: str) -> None:
    faces = mesh.faces
    vertices = mesh.vertices.astype(np.float32)
    normals = mesh.face_normals.astype(np.float32)
    payload = bytearray(80)
    label = header.encode("ascii", "replace")[:80]
    payload[: len(label)] = label
    payload.extend(struct.pack("<I", len(faces)))
    for normal, face in zip(normals, faces):
        payload.extend(struct.pack("<3f", *normal))
        for index in face:
            payload.extend(struct.pack("<3f", *vertices[index]))
        payload.extend(struct.pack("<H", 0))
    path.write_bytes(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=SOURCE_DIR,
        help="Directory containing the US top and bottom STLs",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Directory to write the eu-el001 STLs",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in PARTS:
        source = args.source_dir / name
        mesh = trimesh.load(source, force="mesh")
        if mesh.bounds[1, 0] - mesh.bounds[0, 0] > 194:
            raise SystemExit(
                f"{name} is already wider than the original 192 mm footprint; "
                "refusing to spread it again"
            )
        widened = split_and_spread(mesh)
        if not widened.is_watertight:
            raise SystemExit(f"{name} is not watertight after the spread")
        destination = args.output_dir / name
        write_binary_stl(widened, destination, "Button Box enclosure EL-001")
        size = widened.bounds[1] - widened.bounds[0]
        print(f"{name}: watertight={widened.is_watertight} size={size}")


if __name__ == "__main__":
    main()
