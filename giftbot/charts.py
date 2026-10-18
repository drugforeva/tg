"""PNG-график коллекции: улучшения по часам и флор — две отдельные панели с общей осью времени.

Две разные величины не делят одну ось Y: каждая в своей панели (small multiples).
Один ряд на панель — легенда не нужна, заголовок панели называет ряд.
"""

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

# Роли из эталонной палитры (светлая поверхность): ряд — синий слот 1, текст — текстовые токены.
SURFACE = "#fcfcfb"
SERIES = "#2a78d6"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e6e5e1"
DPI = 150


def _style(ax, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=TEXT_PRIMARY, pad=8, fontweight="bold")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v / 1000:g}K" if v >= 1000 else f"{v:g}"))


def _rounded_columns(ax, fig, xs: list[float], heights: list[int], width: float) -> None:
    """Столбцы со скруглённым верхом (≈4px) и прямым основанием."""
    fig.canvas.draw()
    bbox = ax.get_window_extent()
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    px = 4 * DPI / 100  # ~4px при 100 dpi
    rx = px * (x1 - x0) / bbox.width
    ry = px * (y1 - y0) / bbox.height
    for x, h in zip(xs, heights):
        if h <= 0:
            continue
        left = x - width / 2
        r = min(rx, width / 2)
        ax.add_patch(
            FancyBboxPatch(
                (left, 0),
                width,
                h,
                boxstyle=f"round,pad=0,rounding_size={r}",
                mutation_aspect=ry / rx if rx else 1,
                linewidth=0,
                facecolor=SERIES,
            )
        )
        # Нижняя часть прямоугольником, чтобы основание оставалось прямым.
        ax.add_patch(Rectangle((left, 0), width, max(0, h - ry), linewidth=0, facecolor=SERIES))


def collection_chart(
    title: str, hourly: list[tuple[int, int]], floors: list[tuple[int, int | None]], tz: str
) -> bytes | None:
    """hourly — [(начало часа, число улучшений)], floors — [(время, флор)]. None, если рисовать нечего."""
    floors = [(ts, f) for ts, f in floors if f]
    if not hourly and not floors:
        return None
    zone = ZoneInfo(tz)
    panels = int(bool(hourly)) + int(bool(floors))
    fig, axes = plt.subplots(panels, 1, figsize=(8, 2.6 * panels + 0.4), dpi=DPI, sharex=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(title, x=0.02, y=0.99, ha="left", fontsize=13, color=TEXT_PRIMARY, fontweight="bold")
    axes = [a[0] for a in axes]
    i = 0
    columns = None
    if hourly:
        ax = axes[i]
        i += 1
        xs = [mdates.date2num(datetime.fromtimestamp(ts + 1800, zone)) for ts, _ in hourly]
        heights = [c for _, c in hourly]
        width = (1 / 24) * 0.7  # час минус воздух между столбцами
        _style(ax, "Улучшений в час")
        ax.set_xlim(min(xs) - width, max(xs) + width)
        ax.set_ylim(0, max(heights) * 1.18)
        columns = (ax, xs, heights, width)
        peak = max(range(len(heights)), key=heights.__getitem__)
        ax.annotate(
            str(heights[peak]),
            (xs[peak], heights[peak]),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color=TEXT_PRIMARY,
        )
    if floors:
        ax = axes[i]
        xs = [mdates.date2num(datetime.fromtimestamp(ts, zone)) for ts, _ in floors]
        ys = [f for _, f in floors]
        _style(ax, "Флор, в звёздах")
        ax.plot(xs, ys, color=SERIES, linewidth=2, solid_joinstyle="round", solid_capstyle="round")
        ax.plot(xs[-1:], ys[-1:], "o", color=SERIES, markersize=5, markeredgecolor=SURFACE, markeredgewidth=1.5)
        ax.annotate(
            f"{ys[-1]:,}".replace(",", " "),
            (xs[-1], ys[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=TEXT_PRIMARY,
        )
        low, high = min(ys), max(ys)
        pad = (high - low) * 0.15 or high * 0.05 or 1
        ax.set_ylim(max(0, low - pad), high + pad)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M", tz=zone))
    axes[-1].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8, tz=zone))
    fig.tight_layout(rect=(0, 0, 0.97, 0.96))
    if columns:  # рисуем после раскладки: скругление считается в пикселях готовых осей
        _rounded_columns(columns[0], fig, *columns[1:])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
