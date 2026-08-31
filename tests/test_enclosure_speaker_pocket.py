import collections
import struct
import unittest
from pathlib import Path


ENCLOSURE = Path(__file__).resolve().parents[1] / "hardware" / "enclosure"
TOP = ENCLOSURE / "button-box-enclosure-top.stl"
BOTTOM = ENCLOSURE / "button-box-enclosure-bottom.stl"

EL001_WIDTH_MM = 187.0
MIN_POCKET_WIDTH_MM = 188.0
MAX_POCKET_WIDTH_MM = 190.0
EXPECTED_DEPTH_MM = 41.69
EXPECTED_FOOTPRINT = (197.0, 120.0)


def load_stl(path):
    data = path.read_bytes()
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
    # Speaker pocket inner walls are the inward-facing pair nearest the origin
    # among large X-facing planes in the bay.
    inner_positive = min(positives)
    inner_negative = max(negatives)
    return inner_negative, inner_positive, inner_positive - inner_negative


class EnclosureSpeakerPocketTests(unittest.TestCase):
    def test_top_speaker_pocket_clears_el001_width(self):
        faces, normals = load_stl(TOP)
        low, high = bounds(faces)
        self.assertAlmostEqual(high[0] - low[0], EXPECTED_FOOTPRINT[0], places=1)
        self.assertAlmostEqual(high[1] - low[1], EXPECTED_FOOTPRINT[1], places=1)

        inner_areas = clustered_planes(
            faces, normals, axis=0, y_min=-56.3, y_max=-16.0
        )
        inner = widest_inward_pair(inner_areas, min_area=200)
        self.assertIsNotNone(inner)
        _left, _right, width = inner
        self.assertGreaterEqual(width, MIN_POCKET_WIDTH_MM)
        self.assertLessEqual(width, MAX_POCKET_WIDTH_MM)
        self.assertGreater(width, EL001_WIDTH_MM)

        window_areas = clustered_planes(
            faces, normals, axis=0, y_min=-59.3, y_max=-57.7
        )
        window = widest_inward_pair(window_areas, min_area=20)
        self.assertIsNotNone(window)
        self.assertGreaterEqual(window[2], MIN_POCKET_WIDTH_MM)

        y_areas = clustered_planes(
            faces, normals, axis=1, y_min=-60.0, y_max=-14.0
        )
        front = max((y for y, area in y_areas.items() if y < -50 and area >= 200), default=None)
        back = min((y for y, area in y_areas.items() if -20 < y < -14 and area >= 20), default=None)
        self.assertIsNotNone(front)
        self.assertIsNotNone(back)
        self.assertAlmostEqual(back - front, EXPECTED_DEPTH_MM, places=1)

    def test_bottom_speaker_bay_is_at_least_as_wide_as_the_top(self):
        faces, normals = load_stl(BOTTOM)
        low, high = bounds(faces)
        self.assertAlmostEqual(high[0] - low[0], EXPECTED_FOOTPRINT[0], places=1)
        self.assertAlmostEqual(high[1] - low[1], EXPECTED_FOOTPRINT[1], places=1)
        areas = clustered_planes(faces, normals, axis=0, y_min=-54.0, y_max=-8.0)
        bay = widest_inward_pair(areas, min_area=200)
        self.assertIsNotNone(bay)
        self.assertGreaterEqual(bay[2], MIN_POCKET_WIDTH_MM)


if __name__ == "__main__":
    unittest.main()
