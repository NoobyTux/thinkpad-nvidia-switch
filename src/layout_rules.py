# SPDX-License-Identifier: MIT
"""Small testable width/height rules used by the Qt dashboard."""


def responsive_columns(available_width, minimum_card_width, gap=10):
    """Only use two cards per row when *both* receive safe readable widths."""
    return 2 if available_width >= 2 * minimum_card_width + gap else 1


def selected_tab_height(page_hint_height, tab_bar_hint_height, extra=6):
    """Never size all pages using the tallest unselected page.

    ``extra`` covers the tab pane's border and a little style-dependent slack.
    The page's own margins are already part of ``page_hint_height``.
    """
    return max(90, int(page_hint_height) + int(tab_bar_hint_height) + int(extra))
