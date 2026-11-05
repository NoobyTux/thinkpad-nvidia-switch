# SPDX-License-Identifier: MIT
"""Test the breakpoint decisions independently from the user's Qt installation."""
import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from layout_rules import responsive_columns, selected_tab_height


class ResponsiveLayoutRulesTests(unittest.TestCase):
    def test_ordinary_laptop_width_stacks_sections(self):
        self.assertEqual(responsive_columns(840, 635), 1)
        self.assertEqual(responsive_columns(1000, 620), 1)

    def test_wide_window_uses_two_columns(self):
        self.assertEqual(responsive_columns(1600, 635), 2)
        self.assertEqual(responsive_columns(1250, 620), 2)

    def test_exact_boundary_and_shrinking(self):
        self.assertEqual(responsive_columns(1279, 635, 10), 1)
        self.assertEqual(responsive_columns(1280, 635, 10), 2)
        self.assertEqual(responsive_columns(1279, 635, 10), 1)

    def test_tab_height_is_selected_page_only(self):
        self.assertEqual(selected_tab_height(220, 39), 265)
        self.assertEqual(selected_tab_height(720, 39), 765)

    def test_ui_uses_viewport_not_stale_child_width(self):
        source = (ROOT/'src/optimus_tray.py').read_text()
        self.assertIn('event.size().width()', source)
        self.assertIn("self.main_scroll_viewport.installEventFilter(self)", source)
        self.assertIn('selected_tab_height(height, self.tabBar().sizeHint().height())', source)
        self.assertNotIn('self.grid.setRowStretch(row + 1, 1)', source)
        self.assertNotIn('gpu_tab_layout.addStretch()', source)
        self.assertNotIn('profiles_layout.addStretch()', source)
        self.assertIn('protection_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)', source)
        self.assertIn('class ContentSizedTabWidget(QTabWidget)', source)
        self.assertNotIn('self.tabs.setFixedHeight(', source)
        self.assertNotIn('card.mapTo(page, QPoint(0, card.height()))', source)

class LayoutFeedbackRegressionTests(unittest.TestCase):
    """Execute the sizing algorithm with minimal widget mocks when Qt is absent.

    This catches the v2.6.6 bug: height must not depend on any previously
    compressed/overlapping child geometry.
    """

    def make_tabs(self):
        source = (ROOT / 'src' / 'optimus_tray.py').read_text()
        tree = ast.parse(source)
        klass = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == 'ContentSizedTabWidget')

        class Size:
            def __init__(self, width, height):
                self._width, self._height = width, height
            def width(self): return self._width
            def height(self): return self._height

        class TabBar:
            def sizeHint(self): return Size(700, 39)

        class TabWidget:
            def __init__(self, parent=None): self.page = None
            def sizeHint(self): return Size(900, 1600)  # Height of another, hidden tab
            def currentWidget(self): return self.page
            def tabBar(self): return TabBar()
            def updateGeometry(self): pass

        ns = {'QTabWidget': TabWidget, 'QSize': Size,
              'selected_tab_height': selected_tab_height}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[])),
                     '<tab-size>', 'exec'), ns)
        return ns['ContentSizedTabWidget'](), Size

    def page(self, Size, hint_height, wrapped_height=None):
        class Layout:
            def invalidate(self): pass
            def minimumSize(self): return Size(500, 80)
            def sizeHint(self): return Size(500, hint_height)
            def hasHeightForWidth(self): return wrapped_height is not None
            def heightForWidth(self, width): return wrapped_height

        class Page:
            rendered_height = 40  # Represents an already-clipped page
            def layout(self): return Layout()

        return Page()

    def test_clipped_old_geometry_does_not_compress_tab(self):
        tabs, Size = self.make_tabs()
        tabs.page = self.page(Size, 720)
        tabs.setAvailablePageWidth(860)
        for _ in range(10):
            self.assertEqual(tabs.sizeHint().height(), selected_tab_height(720, 39))
        self.assertEqual(tabs.minimumSizeHint().height(), selected_tab_height(720, 39))

    def test_switching_from_tall_cpu_to_short_gpu_and_back(self):
        tabs, Size = self.make_tabs()
        tabs.setAvailablePageWidth(850)
        tab_heights = []
        for page_height in (750, 205, 450, 750):
            tabs.page = self.page(Size, page_height)
            tab_heights.append(tabs.sizeHint().height())
        self.assertEqual(tab_heights, [selected_tab_height(n, 39) for n in
                                       (750, 205, 450, 750)])

    def test_word_wrapping_never_undersizes_page(self):
        tabs, Size = self.make_tabs()
        tabs.page = self.page(Size, 480, wrapped_height=660)
        tabs.setAvailablePageWidth(725)
        self.assertEqual(tabs.sizeHint().height(), selected_tab_height(660, 39))


if __name__ == '__main__':
    unittest.main()
