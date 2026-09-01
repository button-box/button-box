import collections
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "hardware" / "models"
ENCLOSURE_POINTER = ROOT / "hardware" / "enclosure"
REQUIRED_MODEL_FILES = (
    "README.md",
    "bom.md",
    "enclosure/top.stl",
    "enclosure/bottom.stl",
)
REQUIRED_BOM_COLUMNS = ("Part", "Vendor-agnostic name", "Example SKU", "Notes")
KNOWN_MODELS = ("us", "eu-el001")
DEFAULT_MODEL_ID = "us"

EL001_WIDTH_MM = 187.0
US_FOOTPRINT = (192.0, 120.0)
EU_FOOTPRINT = (197.0, 120.0)
EXPECTED_DEPTH_MM = 41.69
US_INNER_WIDTH_MM = 183.22
EU_MIN_POCKET_WIDTH_MM = 188.0
EU_MAX_POCKET_WIDTH_MM = 190.0


def load_stl(path):
    data = path.read_bytes()
    if data[:5] != b"solid" and len(data) < 84:
        raise ValueError(f"{path} is not a binary STL")
    count = struct.unpack_from("<I", data, 80)[0]
    faces = []
    normals = []
    offset = 84
    for _ in range(count):
        values = struct.unpack_from("<12fH", data, offset)
        normals.append(values[0:3])
        faces.append((values[3:6], values[6:9], values[9:12]))
        offset += 50
    return faces, normals


def bounds(faces):
    xs, ys, zs = [], [], []
    for triangle in faces:
        for x, y, z in triangle:
            xs.append(x)
            ys.append(y)
            zs.append(z)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def mean_vertex(triangle, axis):
    return sum(vertex[axis] for vertex in triangle) / 3.0


def triangle_area(triangle):
    ax, bx, cx = triangle
    ux = (bx[0] - ax[0], bx[1] - ax[1], bx[2] - ax[2])
    vx = (cx[0] - ax[0], cx[1] - ax[1], cx[2] - ax[2])
    cross = (
        ux[1] * vx[2] - ux[2] * vx[1],
        ux[2] * vx[0] - ux[0] * vx[2],
        ux[0] * vx[1] - ux[1] * vx[0],
    )
    return 0.5 * (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5


def clustered_planes(faces, normals, *, axis, y_min, y_max, normal_min=0.95):
    areas = collections.defaultdict(float)
    for triangle, normal in zip(faces, normals):
        if abs(normal[axis]) < normal_min:
            continue
        y = mean_vertex(triangle, 1)
        if y < y_min or y > y_max:
            continue
        key = round(mean_vertex(triangle, axis), 2)
        areas[key] += triangle_area(triangle)
    return areas


def widest_inward_pair(areas, *, min_area):
    positives = [x for x, area in areas.items() if x > 0 and area >= min_area]
    negatives = [x for x, area in areas.items() if x < 0 and area >= min_area]
    if not positives or not negatives:
        return None
    inner_positive = min(positives)
    inner_negative = max(negatives)
    return inner_negative, inner_positive, inner_positive - inner_negative


def model_ids():
    return tuple(
        sorted(
            path.name
            for path in MODELS.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    )


def pocket_width(path, *, y_min, y_max, min_area):
    faces, normals = load_stl(path)
    return widest_inward_pair(
        clustered_planes(faces, normals, axis=0, y_min=y_min, y_max=y_max),
        min_area=min_area,
    )


def pocket_depth(path):
    faces, normals = load_stl(path)
    y_areas = clustered_planes(faces, normals, axis=1, y_min=-60.0, y_max=-14.0)
    front = max((y for y, area in y_areas.items() if y < -50 and area >= 200), default=None)
    back = min((y for y, area in y_areas.items() if -20 < y < -14 and area >= 20), default=None)
    if front is None or back is None:
        return None
    return back - front


class HardwareModelCatalogTests(unittest.TestCase):
    def test_required_models_and_files_exist(self):
        ids = model_ids()
        self.assertEqual(set(ids), set(KNOWN_MODELS))
        for model_id in ids:
            root = MODELS / model_id
            for relative in REQUIRED_MODEL_FILES:
                path = root / relative
                self.assertTrue(path.is_file(), f"missing {path}")
                self.assertGreater(path.stat().st_size, 0, f"empty {path}")
            bom = (root / "bom.md").read_text(encoding="utf-8")
            for column in REQUIRED_BOM_COLUMNS:
                self.assertIn(column, bom)
            self.assertIn("Speaker", bom)
            self.assertIn("Microphone", bom)

    def test_unnamed_enclosure_stls_were_retired(self):
        leftovers = list(ENCLOSURE_POINTER.glob("*.stl"))
        self.assertEqual(leftovers, [])
        pointer = (ENCLOSURE_POINTER / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"](../models/{DEFAULT_MODEL_ID}/", pointer)
        self.assertIn("models/eu-el001/", pointer)


class UsEnclosureTests(unittest.TestCase):
    def test_us_pocket_matches_original_width(self):
        top = MODELS / "us" / "enclosure" / "top.stl"
        bottom = MODELS / "us" / "enclosure" / "bottom.stl"
        faces, _normals = load_stl(top)
        low, high = bounds(faces)
        self.assertAlmostEqual(high[0] - low[0], US_FOOTPRINT[0], places=1)
        self.assertAlmostEqual(high[1] - low[1], US_FOOTPRINT[1], places=1)

        inner = pocket_width(top, y_min=-56.3, y_max=-16.0, min_area=200)
        self.assertIsNotNone(inner)
        self.assertAlmostEqual(inner[2], US_INNER_WIDTH_MM, places=1)
        self.assertLess(inner[2], EL001_WIDTH_MM)

        window = pocket_width(top, y_min=-59.3, y_max=-57.7, min_area=20)
        self.assertIsNotNone(window)
        self.assertAlmostEqual(window[2], 183.10, places=1)

        self.assertAlmostEqual(pocket_depth(top), EXPECTED_DEPTH_MM, places=1)

        bottom_faces, _ = load_stl(bottom)
        blow, bhigh = bounds(bottom_faces)
        self.assertAlmostEqual(bhigh[0] - blow[0], US_FOOTPRINT[0], places=1)
        bay = pocket_width(bottom, y_min=-54.0, y_max=-8.0, min_area=200)
        self.assertIsNotNone(bay)
        self.assertAlmostEqual(bay[2], 187.0, places=1)


class EuEl001EnclosureTests(unittest.TestCase):
    def test_eu_pocket_clears_el001_width(self):
        top = MODELS / "eu-el001" / "enclosure" / "top.stl"
        bottom = MODELS / "eu-el001" / "enclosure" / "bottom.stl"
        faces, _normals = load_stl(top)
        low, high = bounds(faces)
        self.assertAlmostEqual(high[0] - low[0], EU_FOOTPRINT[0], places=1)
        self.assertAlmostEqual(high[1] - low[1], EU_FOOTPRINT[1], places=1)

        inner = pocket_width(top, y_min=-56.3, y_max=-16.0, min_area=200)
        self.assertIsNotNone(inner)
        self.assertGreaterEqual(inner[2], EU_MIN_POCKET_WIDTH_MM)
        self.assertLessEqual(inner[2], EU_MAX_POCKET_WIDTH_MM)
        self.assertGreater(inner[2], EL001_WIDTH_MM)

        window = pocket_width(top, y_min=-59.3, y_max=-57.7, min_area=20)
        self.assertIsNotNone(window)
        self.assertGreaterEqual(window[2], EU_MIN_POCKET_WIDTH_MM)
        self.assertAlmostEqual(pocket_depth(top), EXPECTED_DEPTH_MM, places=1)

        bottom_faces, _ = load_stl(bottom)
        blow, bhigh = bounds(bottom_faces)
        self.assertAlmostEqual(bhigh[0] - blow[0], EU_FOOTPRINT[0], places=1)
        bay = pocket_width(bottom, y_min=-54.0, y_max=-8.0, min_area=200)
        self.assertIsNotNone(bay)
        self.assertGreaterEqual(bay[2], EU_MIN_POCKET_WIDTH_MM)


if __name__ == "__main__":
    unittest.main()
