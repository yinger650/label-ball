"""`client/review_views.py`：坐标页 / 复核页的页面像素 ↔ ROI 往返。

底图每个像素的颜色就是它自己的 ROI 坐标，页面上任一点读回颜色即可核对
`page_to_roi` 算出的坐标，不依赖真实数据。
"""
import unittest

import numpy as np

from client import review_views as RV

W, H = 2000, 1200


def coded_frame(fi: int = 0) -> np.ndarray:
    ys, xs = np.mgrid[0:H, 0:W]
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[..., 0] = xs % 256
    img[..., 1] = ys % 256
    img[..., 2] = (xs // 256) + 16 * (ys // 256) + 1
    return img


def decode(px) -> tuple:
    b, g, r = (int(v) for v in px)
    if r == 0:
        return None
    hi = r - 1
    return (hi % 16) * 256 + b, (hi // 16) * 256 + g


class FakePrep:
    def __init__(self):
        self.frame = coded_frame()

    def raw(self, fi: int) -> np.ndarray:
        return self.frame


class ReviewViewsTest(unittest.TestCase):
    def setUp(self):
        self.prep = FakePrep()

    def pages(self, centers, size, scale, statuses=None):
        specs = [RV.TileSpec(frame_idx=10 + 3 * i, center=c, status=(statuses or [""] * len(centers))[i])
                 for i, c in enumerate(centers)]
        return RV.render_pages(self.prep, specs, size, scale)

    def assert_round_trip(self, page, xs, ys):
        for x in xs:
            for y in ys:
                hit = RV.page_to_roi(page.tiles, page.size, (x + 0.5, y + 0.5))
                if "error" in hit:
                    continue
                truth = decode(page.image[y, x])
                rx, ry = hit["roi_xy"]
                if truth is None:
                    t = hit["tile"]
                    self.assertTrue(rx < 0 or ry < 0 or rx >= W or ry >= H, (x, y, rx, ry, t))
                    continue
                scale = hit["tile"].scale
                self.assertEqual((int(rx // 1), int(ry // 1)), truth, (x, y, scale))

    def test_geometry_budget(self):
        self.assertIsNone(RV.check_geometry(480, 1))
        self.assertIsNone(RV.check_geometry(320, 2))
        self.assertIsNotNone(RV.check_geometry(640, 2))
        self.assertIsNotNone(RV.check_geometry(200, 1))
        self.assertIsNotNone(RV.check_geometry(480, 3))
        self.assertEqual(RV.page_grid(480, 1), (2, 2))
        self.assertEqual(RV.page_grid(320, 2), (2, 1))
        self.assertEqual(RV.page_grid(240, 1), (5, 4))

    def test_round_trip_1x_and_2x(self):
        for size, scale in ((480, 1), (320, 2), (240, 1)):
            centers = [(1000.3, 600.7)] * 5
            for page in self.pages(centers, size, scale):
                self.assertLessEqual(page.size[0], RV.DEFAULT_PAGE_PX[0])
                self.assertLessEqual(page.size[1], RV.DEFAULT_PAGE_PX[1])
                self.assert_round_trip(page, range(0, page.size[0], 37), range(0, page.size[1], 41))

    def test_paging_keeps_frame_order(self):
        pages = self.pages([(900, 500)] * 9, 480, 1)
        self.assertEqual([len(p.tiles) for p in pages], [4, 4, 1])
        frames = [t.frame_idx for p in pages for t in p.tiles]
        self.assertEqual(frames, [10 + 3 * i for i in range(9)])
        last = pages[-1]
        self.assertEqual(last.size, (480, 480 + RV.CAPTION))

    def test_negative_origin_pads_black_and_maps_outside_roi(self):
        page = self.pages([(50.0, 30.0)], 480, 1)[0]
        t = page.tiles[0]
        self.assertEqual(t.roi_origin, (-190, -210))
        hit = RV.page_to_roi(page.tiles, page.size, (10.5, RV.CAPTION + 10.5))
        self.assertEqual(hit["roi_xy"], (-179.5, -199.5))
        self.assertTrue((page.image[RV.CAPTION + 10, 10] == 0).all())
        self.assert_round_trip(page, range(0, 480, 23), range(RV.CAPTION, 520, 19))

    def test_caption_blank_and_outside_rejected(self):
        pages = self.pages([(900, 500)] * 3, 480, 1)
        page = pages[0]
        self.assertIn("标题条", RV.page_to_roi(page.tiles, page.size, (100, 10))["error"])
        self.assertIn("空白", RV.page_to_roi(page.tiles, page.size, (700, 700))["error"])
        self.assertIn("不在页面", RV.page_to_roi(page.tiles, page.size, (2000, 10))["error"])
        self.assertIn("error", RV.page_to_roi(page.tiles, page.size, ("a", 1)))

    def test_seen_size_scales_back_and_checks_aspect(self):
        page = self.pages([(900.0, 500.0)], 480, 1)[0]
        pw, ph = page.size
        direct = RV.page_to_roi(page.tiles, page.size, (240, 300))
        seen = RV.page_to_roi(page.tiles, page.size, (120, 150), seen_size=(pw / 2, ph / 2))
        self.assertEqual(direct["roi_xy"], seen["roi_xy"])
        self.assertIn("长宽比", RV.page_to_roi(page.tiles, page.size, (10, 50), seen_size=(pw, pw))["error"])

    def test_tile_dict_round_trip(self):
        page = self.pages([(900.0, 500.0)], 320, 2, statuses=["visible"])[0]
        t = page.tiles[0]
        back = RV.Tile.from_dict(t.to_dict())
        self.assertEqual(back, t)

    def test_mark_is_drawn_at_label(self):
        spec = RV.TileSpec(frame_idx=1, center=(900, 500), status="visible", mark_xy=(900.0, 500.0))
        panel, _ = RV.render_tile(self.prep.raw(1), spec, 320, 2)
        cy, cx = 160 * 2 + RV.CAPTION, 160 * 2
        ring = [int(v) for v in panel[cy, cx + 20]]
        self.assertGreater(ring[2], 150)
        self.assertLess(max(ring[0], ring[1]), 100)
        self.assertEqual(decode(panel[cy, cx]), (900, 500))

    def test_interp_and_segments(self):
        vis = [(10, 100.0, 50.0), (20, 200.0, 150.0)]
        self.assertEqual(RV.interp_center(15, vis), (150.0, 100.0))
        self.assertEqual(RV.interp_center(5, vis), (100.0, 50.0))
        self.assertEqual(RV.interp_center(25, vis), (200.0, 150.0))
        self.assertIsNone(RV.interp_center(5, []))
        segs = RV.trajectory_segments(list(range(0, 300, 3)), 24)
        self.assertEqual([len(s) for s in segs], [24, 24, 24, 24, 4])

    def test_trajectory_breaks_on_gaps(self):
        seg = [0, 3, 6, 9, 12]
        labels = {0: (500.0, 400.0), 3: (520.0, 410.0), 6: None, 9: (560.0, 430.0), 12: (580.0, 440.0)}
        img, info = RV.render_trajectory(self.prep, seg, labels, step=3)
        self.assertEqual(info["gap_frames"], [6])
        self.assertEqual(info["n_points"], 4)
        self.assertEqual(info["roi_origin"], [340, 240])
        self.assertEqual(info["display_scale"], 1.0)
        self.assertIsNone(RV.render_trajectory(self.prep, seg, {f: None for f in seg}, step=3))
        big = {0: (100.0, 100.0), 3: (1900.0, 1100.0)}
        _, info = RV.render_trajectory(self.prep, [0, 3], big, step=3)
        self.assertLess(info["display_scale"], 1.0)


if __name__ == "__main__":
    unittest.main()
