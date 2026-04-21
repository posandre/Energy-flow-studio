from __future__ import annotations

import html
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.io import to_html, write_image


MAX_POINTS = 4000


def _infer_unit_from_y_title(y_title: str) -> str:
    match = re.search(r"\(([^)]+)\)", y_title or "")
    if match:
        return match.group(1).strip()
    lowered = (y_title or "").lower()
    if "power" in lowered:
        return "kW"
    if "energy" in lowered:
        return "kWh"
    if "soc" in lowered or "%" in lowered:
        return "%"
    return ""


def _apply_default_hovertemplates(figure: go.Figure, *, y_title: str) -> None:
    unit = _infer_unit_from_y_title(y_title)
    unit_suffix = f" {unit}" if unit else ""
    for trace in figure.data:
        if getattr(trace, "hoverinfo", None) == "skip":
            continue
        if getattr(trace, "hovertemplate", None):
            continue
        if getattr(trace, "type", "") not in {"scatter", "bar"}:
            continue
        label = f"{trace.name}: " if getattr(trace, "name", None) else ""
        trace.hovertemplate = f"{label}%{{y:.2~f}}{unit_suffix}<extra></extra>"


def apply_energyflow_chart_style(
    figure: go.Figure,
    *,
    y_title: str,
    x_title: str = "",
    margin: dict[str, int] | None = None,
    barmode: str = "group",
    showlegend: bool = False,
    legend: dict | None = None,
    xaxis_extra: dict | None = None,
    yaxis_extra: dict | None = None,
) -> go.Figure:
    figure.update_layout(
        paper_bgcolor="#0b1220",
        plot_bgcolor="#0b1220",
        font={"color": "#dbeafe"},
        hoverlabel={
            "bgcolor": "#0b1220",
            "bordercolor": "#3b4d63",
            "font": {"color": "#dbeafe", "size": 13},
        },
        hovermode="x unified",
        margin=margin or {"l": 40, "r": 40, "t": 20, "b": 88},
        showlegend=showlegend,
        legend=legend,
        barmode=barmode,
        xaxis={
            "gridcolor": "#22304a",
            "title": x_title,
            **(xaxis_extra or {}),
        },
        yaxis={
            "gridcolor": "#22304a",
            "title": y_title,
            **(yaxis_extra or {}),
        },
    )
    _apply_default_hovertemplates(figure, y_title=y_title)
    return figure


def downsample_dataframe(dataframe: pd.DataFrame, max_points: int = MAX_POINTS) -> pd.DataFrame:
    if len(dataframe) <= max_points:
        return dataframe
    step = max(len(dataframe) // max_points, 1)
    return dataframe.iloc[::step].copy()


def build_chart_figure(
    dataframe: pd.DataFrame,
    timestamp_column: str,
    selected_columns: list[str],
) -> go.Figure:
    figure = go.Figure()
    sampled = downsample_dataframe(dataframe)

    for column in selected_columns:
        figure.add_trace(
            go.Scatter(
                x=sampled[timestamp_column],
                y=sampled[column],
                mode="lines",
                name=column,
            )
        )

    apply_energyflow_chart_style(
        figure,
        y_title="Value",
        x_title="Time",
        margin={"l": 40, "r": 20, "t": 30, "b": 40},
        showlegend=True,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        xaxis_extra={"rangeslider": {"visible": True}},
    )
    return figure


def figure_to_html(figure: go.Figure) -> str:
    figure_html = to_html(
        figure,
        include_plotlyjs=True,
        full_html=False,
        default_width="100%",
        default_height="100%",
        config={"responsive": True},
    )
    return f"""
    <html>
      <head>
        <style>
          html, body {{
            margin: 0;
            padding: 0;
            background: #0b1220;
            overflow: hidden;
            width: 100%;
            height: 100%;
          }}
          .plot-shell {{
            width: 100%;
            height: 100%;
            background: radial-gradient(circle at top, #13213b 0%, #0f172a 52%, #0b1220 100%);
          }}
          .plot-shell > div,
          .plot-shell .js-plotly-plot,
          .plot-shell .plot-container,
          .plot-shell .svg-container {{
            width: 100% !important;
            height: 100% !important;
            background: transparent !important;
          }}
        </style>
      </head>
      <body>
        <div class="plot-shell">{figure_html}</div>
        <script>
          window.addEventListener('load', function() {{
            requestAnimationFrame(function() {{
              if (window.Plotly) {{
                var plot = document.querySelector('.js-plotly-plot');
                if (plot) {{
                  window.Plotly.Plots.resize(plot);
                }}
              }}
            }});
          }});
        </script>
      </body>
    </html>
    """


def figure_to_browser_html(figure: go.Figure) -> str:
    svg = _figure_to_simple_svg(figure)
    if svg is None:
        return _browser_placeholder_html(
            figure.layout.title.text if figure.layout.title and figure.layout.title.text else "Chart preview unavailable"
        )
    return f"""
    <html>
      <head>
        <style>
          html, body {{
            margin: 0;
            padding: 0;
            background: #0b1220;
            width: 100%;
            height: 100%;
            overflow: hidden;
          }}
          .plot-shell {{
            width: 100%;
            height: 100%;
            display: flex;
            align-items: stretch;
            justify-content: stretch;
            background: radial-gradient(circle at top, #13213b 0%, #0f172a 52%, #0b1220 100%);
          }}
          .plot-shell svg {{
            width: 100%;
            height: 100%;
            display: block;
          }}
        </style>
      </head>
      <body>
        <div class="plot-shell">{svg}</div>
      </body>
    </html>
    """


def figure_to_svg(figure: go.Figure) -> str | None:
    return _figure_to_simple_svg(figure)


def _browser_placeholder_html(message: str) -> str:
    safe_message = html.escape(message)
    return f"""
    <html>
      <body style="
        margin:0;
        min-height:100vh;
        display:flex;
        align-items:center;
        justify-content:center;
        background: radial-gradient(circle at top, #13213b 0%, #0f172a 52%, #0b1220 100%);
        color:#cbd5e1;
        font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      ">
        <div style="
          padding:24px 28px;
          border:1px solid #243244;
          border-radius:18px;
          background:rgba(8,15,30,0.72);
          max-width:520px;
          text-align:center;
          line-height:1.5;
          font-size:15px;
        ">
          {safe_message}
        </div>
      </body>
    </html>
    """


def _figure_to_simple_svg(figure: go.Figure) -> str | None:
    supported = {"scatter", "bar"}
    if not figure.data or any(getattr(trace, "type", "") not in supported for trace in figure.data):
        return None

    width = 980
    height = 520
    margin_left = 56
    margin_right = 24
    margin_top = 20
    margin_bottom = 44
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    normalized_traces: list[tuple[str, list[float], list[float], str]] = []
    x_values_all: list[float] = []
    y_values_all: list[float] = []
    categorical_x: list[str] | None = None

    for trace in figure.data:
        trace_type = getattr(trace, "type", "")
        name = getattr(trace, "name", trace_type.title()) or trace_type.title()
        raw_color = (
            getattr(getattr(trace, "line", None), "color", None)
            or getattr(getattr(trace, "marker", None), "color", None)
            or "#38bdf8"
        )
        color = _resolve_svg_color(raw_color, default="#38bdf8")
        trace_x = getattr(trace, "x", None)
        trace_y = getattr(trace, "y", None)
        raw_x = list(trace_x) if trace_x is not None else []
        raw_y = list(trace_y) if trace_y is not None else []
        if not raw_x or not raw_y:
            continue

        x_numeric, x_labels = _normalize_x_values(raw_x)
        if x_numeric is None:
            return None
        if x_labels is not None:
            categorical_x = x_labels
        y_numeric = [float(v) for v in raw_y if v is not None]
        if len(y_numeric) != len(x_numeric):
            return None
        normalized_traces.append((trace_type, x_numeric, y_numeric, color))
        x_values_all.extend(x_numeric)
        y_values_all.extend(y_numeric)

    if not normalized_traces or not x_values_all or not y_values_all:
        return None

    min_x = min(x_values_all)
    max_x = max(x_values_all)
    if max_x == min_x:
        max_x = min_x + 1.0
    min_y = min(0.0, min(y_values_all))
    max_y = max(y_values_all)
    if max_y == min_y:
        max_y = min_y + 1.0

    def scale_x(value: float) -> float:
        return margin_left + (value - min_x) / (max_x - min_x) * plot_width

    def scale_y(value: float) -> float:
        return margin_top + plot_height - (value - min_y) / (max_y - min_y) * plot_height

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none">',
        '<rect width="100%" height="100%" fill="url(#bg)"/>',
        '<defs>',
        '<radialGradient id="bg" cx="50%" cy="0%" r="100%">',
        '<stop offset="0%" stop-color="#13213b"/>',
        '<stop offset="52%" stop-color="#0f172a"/>',
        '<stop offset="100%" stop-color="#0b1220"/>',
        '</radialGradient>',
        '</defs>',
    ]

    grid_color = "#22304a"
    axis_color = "#dbeafe"
    for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = margin_top + plot_height * ratio
        parts.append(
            f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width - margin_right}" y2="{y:.1f}" stroke="{grid_color}" stroke-width="1"/>'
        )

    axis_y = scale_y(0.0 if min_y <= 0 <= max_y else min_y)
    parts.append(
        f'<line x1="{margin_left}" y1="{axis_y:.1f}" x2="{width - margin_right}" y2="{axis_y:.1f}" stroke="{axis_color}" stroke-width="1.5"/>'
    )

    for trace_type, x_values, y_values, color in normalized_traces:
        if trace_type == "scatter":
            points = " ".join(f"{scale_x(x):.2f},{scale_y(y):.2f}" for x, y in zip(x_values, y_values))
            parts.append(
                f'<polyline fill="none" stroke="{html.escape(color)}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" points="{points}"/>'
            )
        elif trace_type == "bar":
            count = max(len(x_values), 1)
            bar_width = plot_width / count * 0.72
            for x, y in zip(x_values, y_values):
                left = scale_x(x) - bar_width / 2
                top = min(scale_y(y), axis_y)
                height_value = abs(axis_y - scale_y(y))
                parts.append(
                    f'<rect x="{left:.2f}" y="{top:.2f}" width="{bar_width:.2f}" height="{max(height_value, 1.0):.2f}" '
                    f'rx="4" fill="{html.escape(color)}" fill-opacity="0.78"/>'
                )

    if categorical_x:
        ticks = list(enumerate(categorical_x))
        if len(ticks) > 8:
            step = max(len(ticks) // 8, 1)
            ticks = ticks[::step]
        for index, label in ticks:
            x = scale_x(float(index))
            safe_label = html.escape(str(label))
            parts.append(
                f'<text x="{x:.2f}" y="{height - 14}" text-anchor="middle" fill="#cbd5e1" font-size="12">{safe_label}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


def _resolve_svg_color(value: object, *, default: str) -> str:
    if isinstance(value, str):
        color = value.strip()
        if not color:
            return default
        return color if _is_valid_svg_color(color) else default

    if isinstance(value, (list, tuple)):
        if len(value) in {3, 4} and all(isinstance(item, (int, float)) for item in value):
            channels = [float(item) for item in value]
            if len(channels) == 3:
                r, g, b = (max(0, min(255, int(round(v)))) for v in channels)
                return f"rgb({r},{g},{b})"
            r, g, b = (max(0, min(255, int(round(v)))) for v in channels[:3])
            alpha = max(0.0, min(1.0, float(channels[3])))
            return f"rgba({r},{g},{b},{alpha:.3f})"
        for item in value:
            resolved = _resolve_svg_color(item, default="")
            if resolved:
                return resolved
        return default

    # Plotly can pass numpy arrays / pandas objects / scalars for marker.color.
    # Try a safe iterable probe first, then fallback to string conversion.
    try:
        iterator = iter(value)  # type: ignore[arg-type]
    except Exception:
        text = str(value).strip()
        if text and text.lower() not in {"none", "nan"} and _is_valid_svg_color(text):
            return text
        return default

    for item in iterator:
        resolved = _resolve_svg_color(item, default="")
        if resolved:
            return resolved
    return default


_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_CSS_FUNC_COLOR_RE = re.compile(r"^(?:rgb|rgba|hsl|hsla)\([^)]*\)$", re.IGNORECASE)
_NAMED_COLOR_RE = re.compile(r"^[a-zA-Z]+$")


def _is_valid_svg_color(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if lowered in {"none", "nan", "null"}:
        return False
    if _HEX_COLOR_RE.match(candidate):
        return True
    if _CSS_FUNC_COLOR_RE.match(candidate):
        return True
    if _NAMED_COLOR_RE.match(candidate):
        return True
    return False


def _normalize_x_values(values: list[object]) -> tuple[list[float] | None, list[str] | None]:
    numeric_values: list[float] = []
    labels: list[str] | None = None
    try:
        timestamps = pd.to_datetime(values, errors="raise")
    except Exception:
        timestamps = None
    if timestamps is not None:
        base = timestamps[0].timestamp()
        return [ts.timestamp() - base for ts in timestamps], None

    try:
        return [float(value) for value in values], None
    except Exception:
        labels = [str(value) for value in values]
        return [float(index) for index in range(len(labels))], labels


def export_figure(figure: go.Figure, output_path: str | Path) -> None:
    path = Path(output_path)
    suffix = path.suffix.lower()
    if suffix == ".html":
        path.write_text(figure_to_html(figure), encoding="utf-8")
        return
    if suffix in {".png", ".pdf"}:
        write_image(figure, str(path))
        return
    raise ValueError("Unsupported export format. Use .html, .png, or .pdf")


def export_all_formats(figure: go.Figure, base_path: str | Path) -> list[Path]:
    base = Path(base_path)
    exported_paths: list[Path] = []
    for suffix in (".html", ".png", ".pdf"):
        destination = base.with_suffix(suffix)
        export_figure(figure, destination)
        exported_paths.append(destination)
    return exported_paths
