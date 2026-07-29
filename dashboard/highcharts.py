import json
import streamlit as st
import pandas as pd
import numpy as np

BLUE = "#37a5ff"
NAVY = "#07162f"
PANEL = "#0c2145"
MINT = "#35d0aa"
AMBER = "#f8b84e"
CORAL = "#fb7185"
MUTED = "#9bb1d1"

DARK_THEME = {
    "chart": {
        "backgroundColor": PANEL,
        "style": {"color": "#b7c8e5"},
        "borderRadius": 10,
        "spacing": [12, 12, 12, 12],
    },
    "title": {"style": {"color": "#f5f9ff", "fontSize": "14px", "fontWeight": "600"}},
    "xAxis": {
        "labels": {"style": {"color": MUTED, "fontSize": "11px"}},
        "lineColor": "#31527f",
        "tickColor": "#31527f",
        "gridLineColor": "#19385f",
        "title": {"style": {"color": "#dce8fa", "fontSize": "12px"}},
    },
    "yAxis": {
        "labels": {"style": {"color": MUTED, "fontSize": "11px"}},
        "lineColor": "#31527f",
        "tickColor": "#31527f",
        "gridLineColor": "#19385f",
        "title": {"style": {"color": "#dce8fa", "fontSize": "12px"}},
    },
    "legend": {
        "itemStyle": {"color": "#dce8fa", "fontSize": "11px"},
        "itemHoverStyle": {"color": "#ffffff"},
    },
    "tooltip": {
        "backgroundColor": "#0d2348",
        "borderColor": "#1a477e",
        "style": {"color": "#edf5ff", "fontSize": "12px"},
        "shadow": False,
    },
    "credits": {"enabled": False},
}


def _html(chart_config: dict, height: int, use_stock: bool = False) -> str:
    chart_id = "hc_" + str(abs(hash(json.dumps(chart_config, sort_keys=True, default=str))))[:10]
    constructor = "stockChart" if use_stock else "chart"
    more_js = '<script src="https://code.highcharts.com/highcharts-more.js"></script>' if not use_stock else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://code.highcharts.com/stock/highstock.js"></script>
{more_js}
</head><body>
<div id="{chart_id}" style="width:100%;height:{height}px;"></div>
<script>
Highcharts.setOptions({json.dumps(DARK_THEME)});
Highcharts.{constructor}('{chart_id}', {json.dumps(chart_config)});
</script>
</body></html>"""


def render(config: dict, height: int = 300, use_stock: bool = False) -> None:
    st.components.v1.html(_html(config, height, use_stock), height=height + 60)


def ranking_chart(df: pd.DataFrame) -> dict:
    df = df.sort_values("score_fiabilite")
    data = []
    for _, r in df.iterrows():
        data.append({"y": round(r["score_fiabilite"], 1), "color": CORAL if r["score_fiabilite"] < 75 else BLUE})
    return {
        "chart": {"type": "bar", "height": 390},
        "title": {"text": None},
        "xAxis": {"categories": df["ligne"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "max": 100, "min": 0},
        "series": [{"name": "Score", "data": data}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
        "tooltip": {"pointFormat": "<b>{point.y}</b> / 100"},
    }


def scatter_chart(df: pd.DataFrame) -> dict:
    series_data = {
        "Peu d'arrêts sautés (< 5 %)": {"color": MINT, "data": []},
        "Arrêts sautés modérés (5-15 %)": {"color": AMBER, "data": []},
        "Nombreux arrêts sautés (> 15 %)": {"color": CORAL, "data": []},
    }
    for _, r in df.iterrows():
        skipped = r.get("pct_arrets_sautes", 0)
        cat = "Peu d'arrêts sautés (< 5 %)" if skipped < 5 else ("Arrêts sautés modérés (5-15 %)" if skipped < 15 else "Nombreux arrêts sautés (> 15 %)")
        series_data[cat]["data"].append({
            "x": round(r["retard_moyen_s"], 1), "y": round(r["pct_retard_5min"], 1),
            "z": max(r["observations"], 1), "name": r["ligne"],
            "pct_arrets_sautes": round(skipped, 2),
        })
    series = [{"type": "bubble", "name": name, "data": d["data"], "color": d["color"],
               "tooltip": {"pointFormat": "<b>{point.name}</b><br/>Retard moyen: {point.x:.0f}s<br/>> 5 min: {point.y:.1f}%<br/>Arrêts sautés: {point.pct_arrets_sautes:.2f}%<br/>Passages: {point.z:,}"}}
              for name, d in series_data.items() if d["data"]]
    return {
        "chart": {"type": "bubble", "height": 390},
        "title": {"text": None},
        "xAxis": {"title": {"text": "Retard moyen (secondes)"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}},
        "series": series,
        "plotOptions": {"bubble": {"minSize": 10, "maxSize": 60, "opacity": 0.82}},
        "legend": {"enabled": True, "verticalAlign": "bottom", "align": "center"},
    }


def timeline_chart(df: pd.DataFrame) -> dict:
    data = [[int(pd.Timestamp(ts).timestamp() * 1000), round(r, 1)] for ts, r in zip(df["date_service"], df["pct_retard_5min"])]
    return {
        "chart": {"type": "area", "height": 285},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"data": data, "fillOpacity": 0.12, "lineWidth": 2, "color": BLUE, "marker": {"enabled": False, "states": {"hover": {"enabled": True}}}}],
    }


def hourly_risk_chart(df: pd.DataFrame, threshold: float) -> dict:
    cat = [str(h) for h in range(24)]
    hm = {int(r["heure"]): r for _, r in df.iterrows()}
    data = []
    for h in range(24):
        if h in hm:
            v = round(hm[h]["pct_retard_5min"], 1)
            data.append({"y": v, "color": CORAL if v > threshold else MINT})
        else:
            data.append({"y": 0, "color": MINT})
    return {
        "chart": {"type": "column", "height": 285},
        "title": {"text": None},
        "xAxis": {"categories": cat, "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "> 5 min", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def delay_distribution_chart(df: pd.DataFrame) -> dict:
    on_time = {"0 à +1", "+1 à +2", "+2 à +5"}
    data = [{"y": int(r["observations"]), "color": MINT if r["plage"] in on_time else AMBER} for _, r in df.iterrows()]
    return {
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": df["plage"].tolist(), "title": {"text": "Écart à l'horaire théorique"}, "labels": {"rotation": -35}},
        "yAxis": {"title": {"text": "Nombre de passages"}},
        "series": [{"name": "Passages", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def collection_minutely_chart(df: pd.DataFrame) -> dict:
    data = [[int(pd.Timestamp(r["minute"]).timestamp() * 1000), int(r["observations"])] for _, r in df.iterrows() if pd.notna(r["observations"])]
    return {
        "chart": {"zoomType": "x"},
        "rangeSelector": {
            "buttons": [
                {"type": "hour", "count": 1, "text": "1h"},
                {"type": "hour", "count": 6, "text": "6h"},
                {"type": "day", "count": 1, "text": "24h"},
                {"type": "day", "count": 7, "text": "1 sem."},
                {"type": "all", "text": "Tout"},
            ],
            "selected": 2,
            "inputDateFormat": "%d/%m/%Y",
            "buttonTheme": {"fill": "#102c58", "stroke": "#1a477e", "style": {"color": "#b7c8e5", "fontSize": "11px"}},
        },
        "navigator": {
            "enabled": True,
            "series": {"color": BLUE, "lineWidth": 1},
            "xAxis": {"labels": {"style": {"color": MUTED}}},
        },
        "scrollbar": {
            "enabled": True,
            "barBackgroundColor": "#102c58",
            "barBorderColor": "#1a477e",
            "buttonBackgroundColor": "#0c2145",
            "buttonBorderColor": "#1a477e",
            "trackBackgroundColor": "#081a37",
            "trackBorderColor": "#1a477e",
        },
        "title": {"text": None},
        "series": [{
            "type": "line",
            "name": "Observations",
            "data": data,
            "gapSize": 5,
            "marker": {"enabled": False, "states": {"hover": {"enabled": True, "radius": 3}}},
        }],
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
    }


def hourly_distribution_chart(df: pd.DataFrame) -> dict:
    data = [int(r["observations"]) for _, r in df.iterrows()]
    return {
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": [str(int(r["heure"])) for _, r in df.iterrows()], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
        "series": [{"name": "Passages", "data": data, "color": BLUE}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.05, "pointPadding": 0.05}},
    }
