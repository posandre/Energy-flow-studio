from __future__ import annotations

import json
import tempfile
from pathlib import Path

from plotly.offline import get_plotlyjs
from plotly.utils import PlotlyJSONEncoder
from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QLabel, QStackedLayout, QWidget

from app.services.i18n import tr_fragment, translate_plotly_payload


class _ChartPage(QWebEnginePage):
    """QWebEngine page wrapper that reports runtime JS/browser diagnostics."""
    def __init__(self, reporter, parent=None) -> None:
        super().__init__(parent)
        self._reporter = reporter

    def createWindow(self, _type):  # noqa: N802
        self._reporter("graph_view:page_create_window_blocked")
        return None

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
        self._reporter(
            "graph_view:js_console",
            level=int(level),
            message=str(message)[:400],
            line=int(line_number),
            source=str(source_id),
        )
        super().javaScriptConsoleMessage(level, message, line_number, source_id)


class GraphView(QWidget):
    """Reusable Plotly host widget backed by QWebEngineView.

    The widget keeps a placeholder and lazily creates the browser only when a
    figure is rendered. This avoids creating heavy WebEngine surfaces for views
    that are never opened.
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._current_figure = None
        self._current_html: str | None = None
        self._render_token = 0
        # Keep a local Plotly bundle so charts never depend on network/CDN.
        self._plotly_js_inline = get_plotlyjs()
        self._assets_dir = Path(tempfile.gettempdir()) / "energyflow_graph_assets"
        self._plotly_js_path = self._assets_dir / "plotly.min.js"
        self._plotly_js_url: str | None = None

        self.browser: QWebEngineView | None = None

        self._placeholder = QLabel("")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet(
            "color:#cbd5e1;background:#0b1220;border:1px solid #243244;"
            "border-radius:14px;padding:20px;font-size:14px;"
        )
        self._stack.addWidget(self._placeholder)
        self._stack.setCurrentWidget(self._placeholder)

        self._diag("graph_view:init", has_browser=False, renderer="qwebengine")
        self.set_placeholder("Open a file or choose a device profile to load a chart.")

    def set_figure(self, figure) -> None:
        """Render a Plotly figure and switch from placeholder to web view."""
        self._current_figure = figure
        self._render_token += 1
        try:
            self._ensure_plotly_asset()
            self._current_html = self._build_plot_html(figure)
        except Exception as exc:
            self._diag("graph_view:set_figure_failed", error=str(exc))
            self.set_placeholder("Failed to render chart.")
            return
        self._diag("graph_view:set_figure", token=self._render_token, **self._figure_metrics(figure))
        QTimer.singleShot(0, self._load_current_html)

    def set_placeholder(self, message: str) -> None:
        """Display a text placeholder and clear current chart state."""
        self._current_figure = None
        self._current_html = None
        self._placeholder.setText(tr_fragment(message))
        self._stack.setCurrentWidget(self._placeholder)
        self._diag("graph_view:set_placeholder", message=message[:120])

    def set_loading(self, message: str = "Loading...") -> None:
        self.set_placeholder(message)

    def reload_current(self) -> None:
        """Rebuild the current figure HTML, useful after style/theme changes."""
        if self._current_figure is not None:
            self.set_figure(self._current_figure)

    def _load_current_html(self) -> None:
        """Load generated HTML into the browser with local asset base URL."""
        if not self._current_html:
            return
        browser = self._ensure_browser()
        browser.show()
        self._stack.setCurrentWidget(browser)
        base_url = QUrl.fromLocalFile(f"{self._assets_dir}/")
        browser.setHtml(self._current_html, base_url)
        self._diag(
            "graph_view:load_html",
            token=self._render_token,
            html_bytes=len(self._current_html),
            base_url=base_url.toString(),
            plotly_exists=self._plotly_js_path.exists(),
            plotly_size=self._plotly_js_path.stat().st_size if self._plotly_js_path.exists() else -1,
        )

    def _ensure_browser(self) -> QWebEngineView:
        """Create the QWebEngine browser once and reuse it for all updates."""
        if self.browser is not None:
            return self.browser
        browser = QWebEngineView(self)
        browser.setPage(_ChartPage(self._diag, browser))
        browser.page().setBackgroundColor(QColor("#0b1220"))
        browser.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        browser.setStyleSheet("background:#0b1220;border:none;")
        # Local HTML must read local plotly.min.js generated into temp assets.
        browser.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        # Remote URL access is intentionally disabled to keep rendering offline.
        browser.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        browser.loadFinished.connect(self._on_webengine_load_finished)
        self._stack.addWidget(browser)
        self.browser = browser
        self._diag("graph_view:create_browser")
        return browser

    def _on_webengine_load_finished(self, ok: bool) -> None:
        """Fallback to placeholder when WebEngine cannot load chart HTML."""
        self._diag("graph_view:webengine_load_finished", token=self._render_token, ok=bool(ok))
        if ok:
            return
        self._placeholder.setText("Failed to render chart.")
        self._stack.setCurrentWidget(self._placeholder)

    def _figure_metrics(self, figure) -> dict[str, object]:
        """Extract lightweight diagnostics used by internal tracing hooks."""
        try:
            traces = list(getattr(figure, "data", []) or [])
            first_points = 0
            first_type = ""
            if traces:
                first_type = str(getattr(traces[0], "type", ""))
                x_values = getattr(traces[0], "x", None)
                if x_values is not None:
                    try:
                        first_points = len(x_values)
                    except Exception:
                        first_points = 0
            return {
                "trace_count": len(traces),
                "first_trace_type": first_type,
                "first_trace_points": first_points,
            }
        except Exception as exc:
            return {"figure_metric_error": str(exc)}

    def _build_plot_html(self, figure) -> str:
        """Create standalone HTML that renders one Plotly figure instance."""
        figure_json = translate_plotly_payload(figure.to_plotly_json())
        plotly_src = "plotly.min.js"
        return f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <script src="{plotly_src}"></script>
            <style>
              html, body {{
                margin: 0;
                padding: 0;
                background: #0b1220;
                overflow: hidden;
                width: 100%;
                height: 100%;
              }}
              #plot {{
                width: 100%;
                height: 100vh;
                background: radial-gradient(circle at top, #13213b 0%, #0f172a 52%, #0b1220 100%);
              }}
            </style>
          </head>
          <body>
            <div id="plot"></div>
            <script>
              const figure = {json.dumps(figure_json, cls=PlotlyJSONEncoder)};
              Plotly.newPlot('plot', figure.data, figure.layout, {{
                responsive: true,
                displaylogo: false
              }});
              window.addEventListener('resize', function() {{
                Plotly.Plots.resize(document.getElementById('plot'));
              }});
            </script>
          </body>
        </html>
        """

    def _ensure_plotly_asset(self) -> None:
        """Write local Plotly runtime into temp assets directory if needed."""
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        if not self._plotly_js_path.exists() or self._plotly_js_path.stat().st_size < 1000:
            self._plotly_js_path.write_text(self._plotly_js_inline, encoding="utf-8")
            self._diag(
                "graph_view:plotly_asset_written",
                path=str(self._plotly_js_path),
                bytes=self._plotly_js_path.stat().st_size,
            )
        self._plotly_js_url = QUrl.fromLocalFile(str(self._plotly_js_path)).toString()

    def _diag(self, event: str, **details: object) -> None:
        _ = (event, details)
        return
