"""Hand-rolled SVG line charts — self-contained reports with zero external assets (§13)."""

from __future__ import annotations

from html import escape

WIDTH = 680
HEIGHT = 260
MARGIN_LEFT = 62
MARGIN_RIGHT = 18
MARGIN_TOP = 30
MARGIN_BOTTOM = 34
GRID_LINES = 4


def _fmt(value: float) -> str:
    return f"{value:.4g}"


def line_chart(
    points: list[tuple[str, float]],
    *,
    title: str,
    highlight: str | None = None,
) -> str:
    """points: [(x-label, y-value)]; highlighted label gets a bigger marker + label."""
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    if not points:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
            f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img">'
            f'<text x="{MARGIN_LEFT}" y="18" class="chart-title">{escape(title)}</text>'
            f'<text x="{MARGIN_LEFT}" y="{HEIGHT // 2}" class="chart-label">（无数据）</text></svg>'
        )
    values = [value for _label, value in points]
    vmin, vmax = min(values), max(values)
    span = vmax - vmin
    pad = span * 0.12 if span > 1e-12 else max(abs(vmax) * 0.05, 0.5)
    ymin, ymax = vmin - pad, vmax + pad

    def sx(index: int) -> float:
        if len(points) == 1:
            return MARGIN_LEFT + plot_w / 2
        return MARGIN_LEFT + plot_w * index / (len(points) - 1)

    def sy(value: float) -> float:
        return MARGIN_TOP + plot_h * (1 - (value - ymin) / (ymax - ymin))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img">',
        f'<text x="{MARGIN_LEFT}" y="18" class="chart-title">{escape(title)}</text>',
    ]
    for step in range(GRID_LINES + 1):
        value = ymin + (ymax - ymin) * step / GRID_LINES
        y = round(sy(value), 2)
        parts.append(f'<line x1="{MARGIN_LEFT}" y1="{y}" x2="{WIDTH - MARGIN_RIGHT}" y2="{y}" class="chart-grid"/>')
        parts.append(
            f'<text x="{MARGIN_LEFT - 6}" y="{y + 4}" class="chart-label" text-anchor="end">{_fmt(value)}</text>'
        )
    parts.append(
        f'<line x1="{MARGIN_LEFT}" y1="{MARGIN_TOP + plot_h}" x2="{WIDTH - MARGIN_RIGHT}" '
        f'y2="{MARGIN_TOP + plot_h}" class="chart-axis"/>'
    )
    for index, (label, _value) in enumerate(points):
        parts.append(
            f'<text x="{round(sx(index), 2)}" y="{HEIGHT - 12}" class="chart-label" text-anchor="middle">'
            f"{escape(label)}</text>"
        )
    polyline = " ".join(f"{round(sx(index), 2)},{round(sy(value), 2)}" for index, (_label, value) in enumerate(points))
    parts.append(f'<polyline points="{polyline}" class="chart-line"/>')
    for index, (label, value) in enumerate(points):
        x, y = round(sx(index), 2), round(sy(value), 2)
        if highlight is not None and label == highlight:
            parts.append(f'<circle cx="{x}" cy="{y}" r="5.5" class="chart-best"/>')
            parts.append(
                f'<text x="{x}" y="{y - 10}" class="chart-best-label" text-anchor="middle">{escape(label)}</text>'
            )
        else:
            parts.append(f'<circle cx="{x}" cy="{y}" r="3.5" class="chart-dot"/>')
    parts.append("</svg>")
    return "".join(parts)
