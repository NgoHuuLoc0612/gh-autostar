"""
Dash-based interactive analytics dashboard for gh-autostar.
"""

from __future__ import annotations

import threading
import webbrowser
from typing import Any

from gh_autostar.logging_setup import get_logger
from gh_autostar.storage.database import Database

logger = get_logger("dashboard")

_DEFAULT_PORT = 8751
_BG      = "#0d1117"
_PAPER   = "#161b22"
_GRID    = "#21262d"
_TEXT    = "#e6edf3"
_MUTED   = "#8b949e"
_ACCENT  = "#58a6ff"
_GREEN   = "#3fb950"
_ORANGE  = "#ffa657"
_PURPLE  = "#d2a8ff"
_RED     = "#f78166"

_PALETTE = [
    _ACCENT, _GREEN, _ORANGE, _PURPLE, _RED,
    "#79c0ff", "#56d364", "#ff7b72", "#e3b341", "#bc8cff",
    "#54aeff", "#2ea043", "#cae8ff", "#ffdf5d",
]


def _base(title: str) -> dict[str, Any]:
    return dict(
        title=dict(text=title, font=dict(color=_TEXT, size=15)),
        paper_bgcolor=_PAPER,
        plot_bgcolor=_BG,
        font=dict(color=_TEXT, family="'Segoe UI', Arial, sans-serif"),
        margin=dict(l=50, r=30, t=55, b=45),
        legend=dict(bgcolor=_PAPER, bordercolor=_GRID, borderwidth=1),
    )


def _ax() -> dict[str, Any]:
    """Shared axis style (no yaxis key — caller applies per-axis)."""
    return dict(gridcolor=_GRID, zerolinecolor=_GRID, linecolor=_GRID)


def build_app(db: Database) -> "Any":
    import dash
    from dash import dcc, html
    import plotly.graph_objects as go
    import pandas as pd

    app = dash.Dash(__name__, title="gh-autostar Analytics", update_title=None)

    # ── Data ─────────────────────────────────────────────────────────────────
    summary    = db.get_full_stats_summary()
    cumulative = db.get_cumulative_stars()
    per_day    = db.get_stars_per_day(days=90)
    per_week   = db.get_stars_per_week(weeks=26)
    by_lang    = db.get_language_breakdown()
    by_source  = db.get_source_breakdown()
    by_hour    = db.get_stars_per_hour_of_day()
    by_dow     = db.get_stars_per_day_of_week()
    top_repos  = db.get_top_starred_repos(limit=25)
    batches    = db.get_batch_performance(limit=40)

    # ── Chart 1: Growth (dual-axis bar + line) ────────────────────────────────
    if cumulative:
        df = pd.DataFrame(cumulative)
        fig_growth = go.Figure()
        fig_growth.add_trace(go.Bar(
            x=df["date"], y=df["daily"], name="Daily",
            marker_color=_ACCENT, opacity=0.55, yaxis="y",
        ))
        fig_growth.add_trace(go.Scatter(
            x=df["date"], y=df["cumulative"], name="Cumulative",
            line=dict(color=_GREEN, width=2.5), yaxis="y2", mode="lines",
        ))
        ax = _ax()
        fig_growth.update_layout(
            **_base("⭐ Stars Growth"),
            xaxis=ax,
            yaxis={**ax, "title": "Daily"},
            yaxis2={**ax, "title": "Cumulative", "overlaying": "y", "side": "right"},
            barmode="overlay",
            hovermode="x unified",
        )
    else:
        fig_growth = go.Figure().update_layout(**_base("No data yet"))

    # ── Chart 2: Language donut ───────────────────────────────────────────────
    if by_lang:
        df_lang = pd.DataFrame(by_lang).head(14)
        fig_lang = go.Figure(go.Pie(
            labels=df_lang["language"], values=df_lang["count"],
            hole=0.42,
            marker=dict(colors=_PALETTE, line=dict(color=_BG, width=2)),
            textinfo="label+percent",
            hovertemplate="<b>%{label}</b><br>%{value} repos (%{percent})<extra></extra>",
        ))
        fig_lang.update_layout(**_base("🌐 Language Breakdown"))
    else:
        fig_lang = go.Figure().update_layout(**_base("No language data"))

    # ── Chart 3: Language sunburst ────────────────────────────────────────────
    if by_lang:
        df_lang2 = pd.DataFrame(by_lang).head(14)
        fig_sun = go.Figure(go.Sunburst(
            labels=["All"] + df_lang2["language"].tolist(),
            parents=[""] + ["All"] * len(df_lang2),
            values=[df_lang2["count"].sum()] + df_lang2["count"].tolist(),
            marker=dict(colors=[""] + _PALETTE[:len(df_lang2)]),
            branchvalues="total",
            hovertemplate="<b>%{label}</b><br>%{value} repos<extra></extra>",
        ))
        fig_sun.update_layout(**_base("🌐 Language Sunburst"))
    else:
        fig_sun = go.Figure().update_layout(**_base("No language data"))

    # ── Chart 4: Source funnel ────────────────────────────────────────────────
    if by_source:
        df_src = pd.DataFrame(by_source)
        fig_funnel = go.Figure(go.Funnel(
            y=df_src["source"], x=df_src["count"],
            textinfo="value+percent initial",
            marker=dict(color=_PALETTE[:len(df_src)]),
        ))
        fig_funnel.update_layout(**_base("🔍 Discovery Source Funnel"))
    else:
        fig_funnel = go.Figure().update_layout(**_base("No source data"))

    # ── Chart 5: Hour-of-day polar clock ─────────────────────────────────────
    if by_hour:
        df_hour = pd.DataFrame(by_hour)
        all_h = pd.DataFrame({"hour": range(24)})
        df_hour = all_h.merge(df_hour, on="hour", how="left").fillna(0)
        theta = [f"{h:02d}:00" for h in df_hour["hour"]]
        fig_clock = go.Figure(go.Barpolar(
            r=df_hour["count"], theta=theta,
            marker_color=_ACCENT, marker_line_color=_BG,
            marker_line_width=1, opacity=0.85,
        ))
        fig_clock.update_layout(
            **_base("🕐 Activity by Hour"),
            polar=dict(
                bgcolor=_BG,
                radialaxis=dict(visible=True, gridcolor=_GRID),
                angularaxis=dict(
                    tickmode="array", tickvals=theta,
                    gridcolor=_GRID, direction="clockwise",
                ),
            ),
        )
    else:
        fig_clock = go.Figure().update_layout(**_base("No hourly data"))

    # ── Chart 6: Day-of-week radar ────────────────────────────────────────────
    if by_dow:
        df_dow = pd.DataFrame(by_dow)
        all_days = pd.DataFrame({
            "dow": range(7),
            "label": ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"],
        })
        df_dow = all_days.merge(df_dow[["dow","count"]], on="dow", how="left").fillna(0)
        labels = df_dow["label"].tolist()
        values = df_dow["count"].tolist()
        fig_radar = go.Figure(go.Scatterpolar(
            r=values + [values[0]],
            theta=labels + [labels[0]],
            fill="toself",
            line_color=_ACCENT,
            fillcolor=_ACCENT,
            opacity=0.35,
        ))
        fig_radar.update_layout(
            **_base("📅 Day of Week"),
            polar=dict(
                bgcolor=_BG,
                radialaxis=dict(visible=True, gridcolor=_GRID),
                angularaxis=dict(gridcolor=_GRID),
            ),
        )
    else:
        fig_radar = go.Figure().update_layout(**_base("No weekly data"))

    # ── Chart 7: Weekly bars ──────────────────────────────────────────────────
    if per_week:
        df_wk = pd.DataFrame(per_week)
        fig_weekly = go.Figure(go.Bar(
            x=df_wk["week"], y=df_wk["count"],
            marker_color=_PURPLE,
            text=df_wk["count"], textposition="outside",
        ))
        ax2 = _ax()
        fig_weekly.update_layout(
            **_base("📆 Stars per Week"),
            xaxis=ax2, yaxis=ax2,
        )
    else:
        fig_weekly = go.Figure().update_layout(**_base("No weekly data"))

    # ── Chart 8: Batch performance scatter ────────────────────────────────────
    if batches:
        df_b = pd.DataFrame(batches)
        total = df_b["total_starred"] + df_b["total_failed"] + 0.001
        df_b["success_pct"] = (df_b["total_starred"] / total * 100).round(1)
        fig_batch = go.Figure(go.Scatter(
            x=df_b["started_at"],
            y=df_b["total_starred"],
            mode="markers+lines",
            marker=dict(
                size=(df_b["api_calls_used"].clip(upper=40) + 8).tolist(),
                color=df_b["success_pct"].tolist(),
                colorscale="RdYlGn",
                colorbar=dict(title="Success %", thickness=12),
                showscale=True,
            ),
            text=[
                f"Batch #{r['id']}<br>Starred:{r['total_starred']} "
                f"Failed:{r['total_failed']} API:{r['api_calls_used']}"
                for _, r in df_b.iterrows()
            ],
            hoverinfo="text",
            line=dict(color=_GRID, dash="dot"),
        ))
        ax3 = _ax()
        fig_batch.update_layout(
            **_base("⚙️ Batch Performance"),
            xaxis={**ax3, "title": "Run time"},
            yaxis={**ax3, "title": "Repos starred"},
        )
    else:
        fig_batch = go.Figure().update_layout(**_base("No batch data"))

    # ── Chart 9: 3D scatter — stars × language × date ─────────────────────────
    if top_repos:
        df_top = pd.DataFrame(top_repos)
        df_top["ts"] = pd.to_datetime(df_top["starred_at"], utc=True).astype("int64") // 10**9
        lang_map = {l: i for i, l in enumerate(df_top["language"].unique())}
        df_top["lang_idx"] = df_top["language"].map(lang_map)
        fig_3d = go.Figure(go.Scatter3d(
            x=df_top["stars"],
            y=df_top["lang_idx"],
            z=df_top["ts"],
            mode="markers",
            marker=dict(
                size=6, color=df_top["stars"].tolist(),
                colorscale="Viridis", opacity=0.85,
                showscale=True,
                colorbar=dict(title="Stars", thickness=12),
            ),
            text=df_top["repo_full_name"],
            customdata=df_top["language"],
            hovertemplate="<b>%{text}</b><br>Stars:%{x:,}<br>Lang:%{customdata}<extra></extra>",
        ))
        lang_labels = list(lang_map.keys())
        fig_3d.update_layout(
            **_base("🌌 3D: Stars × Language × Date"),
            scene=dict(
                bgcolor=_BG,
                xaxis=dict(title="Stars", gridcolor=_GRID, backgroundcolor=_BG),
                yaxis=dict(
                    title="Language",
                    tickvals=list(range(len(lang_labels))),
                    ticktext=lang_labels,
                    gridcolor=_GRID, backgroundcolor=_BG,
                ),
                zaxis=dict(title="Date (unix)", gridcolor=_GRID, backgroundcolor=_BG),
            ),
        )
    else:
        fig_3d = go.Figure().update_layout(**_base("No repo data"))

    # ── Summary card helper ───────────────────────────────────────────────────
    def card(label: str, value: Any, color: str = _ACCENT) -> "Any":
        return html.Div([
            html.P(label, style={"color": _MUTED, "margin": "0", "fontSize": "11px"}),
            html.H2(str(value), style={"color": color, "margin": "4px 0 0", "fontSize": "26px"}),
        ], style={
            "background": _PAPER, "border": f"1px solid {_GRID}",
            "borderRadius": "8px", "padding": "14px 18px", "flex": "1", "minWidth": "130px",
        })

    # ── Repo table rows ───────────────────────────────────────────────────────
    repo_rows = [
        html.Tr([
            html.Td(html.A(r["repo_full_name"],
                href=f"https://github.com/{r['repo_full_name']}", target="_blank",
                style={"color": _ACCENT}),
                style={"padding": "7px 12px"}),
            html.Td(f"⭐ {int(r.get('stars', 0)):,}",
                style={"padding": "7px 12px", "color": _ORANGE, "textAlign": "right"}),
            html.Td(r.get("language", "—"),
                style={"padding": "7px 12px", "color": _MUTED}),
            html.Td(r.get("starred_at", "")[:10],
                style={"padding": "7px 12px", "color": _MUTED, "fontSize": "12px"}),
        ], style={"borderBottom": f"1px solid {_GRID}"})
        for r in top_repos
    ]

    G = "12px"   # gap shorthand

    # ── Layout ────────────────────────────────────────────────────────────────
    app.layout = html.Div([

        # Header
        html.Div([
            html.H1("⭐ gh-autostar", style={"color": _TEXT, "margin": "0", "fontSize": "22px"}),
            html.P("Analytics Dashboard", style={"color": _MUTED, "margin": "4px 0 0", "fontSize": "13px"}),
        ], style={"borderBottom": f"1px solid {_GRID}", "padding": "18px 28px 14px"}),

        # KPI cards
        html.Div([
            card("Total Starred",  f"{summary['total_starred']:,}", _GREEN),
            card("This Week",      f"{summary['starred_this_week']:,}", _ACCENT),
            card("Today",          f"{summary['starred_today']:,}", _ORANGE),
            card("Total Batches",  f"{summary['total_batches']:,}", _PURPLE),
            card("Failed",         f"{summary['total_failed']:,}", _RED),
        ], style={"display": "flex", "gap": G, "padding": f"18px 28px", "flexWrap": "wrap"}),

        # Row 1: growth + 3D
        html.Div([
            dcc.Graph(figure=fig_growth, style={"flex": "2", "minWidth": "380px"}),
            dcc.Graph(figure=fig_3d,     style={"flex": "1.4", "minWidth": "320px"}),
        ], style={"display": "flex", "gap": G, "padding": f"0 28px {G}"}),

        # Row 2: donut + sunburst + funnel
        html.Div([
            dcc.Graph(figure=fig_lang,   style={"flex": "1", "minWidth": "260px"}),
            dcc.Graph(figure=fig_sun,    style={"flex": "1", "minWidth": "260px"}),
            dcc.Graph(figure=fig_funnel, style={"flex": "1", "minWidth": "260px"}),
        ], style={"display": "flex", "gap": G, "padding": f"0 28px {G}"}),

        # Row 3: clock + radar + weekly
        html.Div([
            dcc.Graph(figure=fig_clock,  style={"flex": "1", "minWidth": "260px"}),
            dcc.Graph(figure=fig_radar,  style={"flex": "1", "minWidth": "260px"}),
            dcc.Graph(figure=fig_weekly, style={"flex": "1.4", "minWidth": "280px"}),
        ], style={"display": "flex", "gap": G, "padding": f"0 28px {G}"}),

        # Row 4: batch performance
        dcc.Graph(figure=fig_batch, style={"padding": f"0 28px {G}"}),

        # Top repos table
        html.Div([
            html.H3("🏆 Top Starred Repos",
                style={"color": _TEXT, "margin": "0 0 12px", "fontSize": "15px"}),
            html.Table(
                [html.Thead(html.Tr([
                    html.Th(h, style={
                        "textAlign": "left", "padding": "8px 12px",
                        "color": _MUTED, "borderBottom": f"1px solid {_GRID}",
                        "fontSize": "12px",
                    })
                    for h in ["Repository", "Stars", "Language", "Starred At"]
                ]))] + [html.Tbody(repo_rows)],
                style={
                    "width": "100%", "borderCollapse": "collapse",
                    "background": _PAPER, "borderRadius": "8px",
                }
            ),
        ], style={
            "margin": f"0 28px 28px",
            "padding": "18px",
            "background": _PAPER,
            "border": f"1px solid {_GRID}",
            "borderRadius": "8px",
        }),

    ], style={
        "background": _BG,
        "minHeight": "100vh",
        "fontFamily": "'Segoe UI', Arial, sans-serif",
        "color": _TEXT,
    })

    return app


def launch_dashboard(
    db: Database,
    port: int = _DEFAULT_PORT,
    open_browser: bool = True,
    debug: bool = False,
) -> None:
    """Launch the Dash analytics dashboard."""
    try:
        import dash      # noqa: F401
        import plotly    # noqa: F401
        import pandas    # noqa: F401
    except ImportError:
        raise ImportError("Dashboard requires: pip install dash plotly pandas")

    app = build_app(db)
    url = f"http://127.0.0.1:{port}"
    logger.info("Dashboard at %s", url)

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=False)
