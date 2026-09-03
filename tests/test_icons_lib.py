import unittest
from pathlib import Path
from PIL import Image

import _icons_lib


class IconsLibTest(unittest.TestCase):
    def test_dropdown_icons_exist_and_base64_encoded(self):
        for name in ["claude", "codex", "gemini", "antigravity", "google", "grok", "kimi"]:
            b64_dark = _icons_lib.get_dropdown_icon_base64(name, theme="dark")
            b64_light = _icons_lib.get_dropdown_icon_base64(name, theme="light")
            self.assertTrue(len(b64_dark) > 0, f"Missing dark icon for {name}")
            self.assertTrue(len(b64_light) > 0, f"Missing light icon for {name}")

    def test_render_menubar_image(self):
        segments = [
            {"icon": "claude", "text": "26%·32"},
            {"icon": "codex", "text": "99%·251"},
            {"icon": "kimi", "text": "51%·202"},
            {"icon": "gemini", "text": "33%·146"},
            {"icon": "gemini", "label": "3P", "text": "0%·0"},
            {"icon": "grok", "text": "8%·20"},
        ]
        b64 = _icons_lib.render_menubar_image(segments, theme="dark")
        self.assertIsNotNone(b64)
        self.assertTrue(len(b64) > 100)

        b64_light = _icons_lib.render_menubar_image(segments, theme="light")
        self.assertIsNotNone(b64_light)
        self.assertTrue(len(b64_light) > 100)


if __name__ == "__main__":
    unittest.main()
