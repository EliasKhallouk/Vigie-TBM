"""Configurations Highcharts pour le dashboard Vigie TBM.

Charte graphique alignée sur les rapports mensuels (couleurs TBM) :
bleu #009EE3, vert #94C21E, magenta #E7007C, orange #F5A623, gris #4A4A4A / #E8E9EB.
"""

import json
import streamlit as st
import pandas as pd

TBM_BLEU = "#009EE3"
TBM_VERT = "#94C21E"
TBM_MAGENTA = "#E7007C"
TBM_ORANGE = "#F5A623"
TBM_GRIS = "#E8E9EB"
TBM_GRIS_TEXTE = "#4A4A4A"
TBM_SOMBRE = "#17181A"

MODE_COLORS = {0: TBM_BLEU, 3: TBM_VERT, 4: TBM_MAGENTA}

LIGHT_THEME = {
    "chart": {
        "backgroundColor": "#FFFFFF",
        "style": {"color": TBM_GRIS_TEXTE, "fontFamily": "Inter, 'Segoe UI', sans-serif"},
        "borderRadius": 8,
        "spacing": [12, 12, 12, 12],
    },
    "title": {"style": {"color": TBM_SOMBRE, "fontSize": "14px", "fontWeight": "700"}},
    "xAxis": {
        "labels": {"style": {"color": TBM_GRIS_TEXTE, "fontSize": "11px"}},
        "lineColor": "#d5dae2",
        "tickColor": "#d5dae2",
        "gridLineColor": "#eef1f5",
        "title": {"style": {"color": TBM_GRIS_TEXTE, "fontSize": "12px"}},
    },
    "yAxis": {
        "labels": {"style": {"color": TBM_GRIS_TEXTE, "fontSize": "11px"}},
        "lineColor": "#d5dae2",
        "tickColor": "#d5dae2",
        "gridLineColor": "#eef1f5",
        "title": {"style": {"color": TBM_GRIS_TEXTE, "fontSize": "12px"}},
    },
    "legend": {
        "itemStyle": {"color": TBM_GRIS_TEXTE, "fontSize": "11px"},
        "itemHoverStyle": {"color": TBM_SOMBRE},
    },
    "tooltip": {
        "backgroundColor": "#FFFFFF",
        "borderColor": "#d5dae2",
        "style": {"color": TBM_SOMBRE, "fontSize": "12px"},
        "shadow": True,
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
Highcharts.setOptions({json.dumps(LIGHT_THEME)});
Highcharts.{constructor}('{chart_id}', {json.dumps(chart_config)});
</script>
</body></html>"""


def render(config: dict, height: int = 300, use_stock: bool = False) -> None:
    st.components.v1.html(_html(config, height, use_stock), height=height + 60)


def _score_color(value: float) -> str:
    if value >= 80:
        return TBM_VERT
    if value >= 50:
        return TBM_ORANGE
    return TBM_MAGENTA


def ranking_chart(df: pd.DataFrame) -> dict:
    df = df.sort_values("score_fiabilite")
    data = [{"y": round(r["score_fiabilite"], 1), "color": _score_color(r["score_fiabilite"])} for _, r in df.iterrows()]
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
    """Carte de risque : retard médian (x) vs retards > 5 min (y), coloré par moyen de transport."""
    series_data: dict[str, dict] = {}
    for _, r in df.iterrows():
        mode = r.get("mode", "Autre")
        if mode not in series_data:
            series_data[mode] = {"color": r.get("mode_color", TBM_BLEU), "data": []}
        series_data[mode]["data"].append({
            "x": round(r["retard_median_s"], 1),
            "y": round(r["pct_retard_5min"], 1),
            "z": max(r["observations"], 1),
            "name": r["ligne"],
            "score": round(r["score_fiabilite"], 1),
        })
    series = [{"type": "bubble", "name": name, "data": d["data"], "color": d["color"],
               "tooltip": {"pointFormat": "<b>{point.name}</b> ({point.series.name})<br/>Retard médian : {point.x:.0f} s<br/>&gt; 5 min : {point.y:.1f} %<br/>Score : {point.score}/100<br/>Passages : {point.z:,}"}}
              for name, d in series_data.items() if d["data"]]
    return {
        "chart": {"type": "bubble", "height": 390},
        "title": {"text": None},
        "xAxis": {"title": {"text": "Retard médian (secondes)"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}},
        "series": series,
        "plotOptions": {"bubble": {"minSize": 10, "maxSize": 60, "opacity": 0.8}},
        "legend": {"enabled": True, "verticalAlign": "bottom", "align": "center"},
    }


def network_daily_chart(df: pd.DataFrame) -> dict:
    data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": round(r, 1),
             "passages": int(obs), "color": _score_color(100 - r)}
            for ts, r, obs in zip(df["date_service"], df["pct_retard_5min"], df["observations"])]
    return {
        "chart": {"type": "line", "height": 300},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "Retards > 5 min", "data": data, "color": TBM_BLEU, "lineWidth": 2,
                    "marker": {"enabled": True, "radius": 5},
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,}"}}],
        "plotOptions": {"series": {"dataLabels": {"enabled": False}}},
    }


def network_hourly_chart(df: pd.DataFrame) -> dict:
    hm = {int(r["heure"]): r for _, r in df.iterrows()}
    data = []
    for h in range(24):
        if h in hm:
            v = round(hm[h]["pct_retard_5min"], 1)
            data.append({"y": v, "color": TBM_VERT if v <= 5 else (TBM_ORANGE if v <= 15 else TBM_MAGENTA)})
        else:
            data.append({"y": 0, "color": TBM_GRIS})
    return {
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": [str(h) for h in range(24)], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "> 5 min", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def mode_comparison_chart(mode_stats: pd.DataFrame) -> dict:
    metrics = [("pct_a_l_heure", "Ponctualité ≤ 5 min"),
               ("pct_retard_5min", "Retards > 5 min"),
               ("pct_avance_1min", "En avance > 1 min"),
               ("pct_arrets_sautes", "Arrêts sautés")]
    series = []
    for _, r in mode_stats.iterrows():
        series.append({"name": r["mode"], "color": r["mode_color"], "data": [round(r[m], 1) for m, _ in metrics]})
    return {
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [label for _, label in metrics], "title": {"text": None}},
        "yAxis": {"title": {"text": "%"}, "min": 0, "max": 100},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.1, "pointPadding": 0.05}},
    }


def mode_daily_chart(df: pd.DataFrame) -> dict:
    series = []
    for mode, sub in df.groupby("mode"):
        sub = sub.sort_values("date_service")
        data = [[int(pd.Timestamp(ts).timestamp() * 1000), round(v, 1)] for ts, v in zip(sub["date_service"], sub["pct_retard_5min"])]
        series.append({"name": mode, "data": data, "color": sub["mode_color"].iloc[0], "lineWidth": 2,
                       "marker": {"enabled": False, "states": {"hover": {"enabled": True}}}})
    return {
        "chart": {"type": "line", "height": 300},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
    }


def mode_hourly_chart(df: pd.DataFrame) -> dict:
    series = []
    for mode, sub in df.groupby("mode"):
        hm = {int(r["heure"]): round(r["pct_retard_5min"], 1) for _, r in sub.iterrows()}
        data = [{"y": hm.get(h, 0), "color": sub["mode_color"].iloc[0] if h in hm else TBM_GRIS} for h in range(24)]
        series.append({"name": mode, "data": data, "color": sub["mode_color"].iloc[0]})
    return {
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [str(h) for h in range(24)], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.1, "pointPadding": 0.05}},
    }


def timeline_chart(df: pd.DataFrame) -> dict:
    data = [[int(pd.Timestamp(ts).timestamp() * 1000), round(r, 1)] for ts, r in zip(df["date_service"], df["pct_retard_5min"])]
    return {
        "chart": {"type": "area", "height": 285},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"data": data, "fillOpacity": 0.12, "lineWidth": 2, "color": TBM_BLEU, "marker": {"enabled": False, "states": {"hover": {"enabled": True}}}}],
    }


def hourly_risk_chart(df: pd.DataFrame, threshold: float) -> dict:
    cat = [str(h) for h in range(24)]
    hm = {int(r["heure"]): r for _, r in df.iterrows()}
    data = []
    for h in range(24):
        if h in hm:
            v = round(hm[h]["pct_retard_5min"], 1)
            data.append({"y": v, "color": TBM_MAGENTA if v > threshold else TBM_VERT})
        else:
            data.append({"y": 0, "color": TBM_GRIS})
    return {
        "chart": {"type": "column", "height": 285},
        "title": {"text": None},
        "xAxis": {"categories": cat, "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "> 5 min", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def delay_distribution_chart(df: pd.DataFrame) -> dict:
    dist_color_map = {
        "< −10 min": TBM_MAGENTA, "−10 à −5": TBM_MAGENTA, "−5 à −2": TBM_MAGENTA,
        "−2 à −1": TBM_ORANGE, "−1 à 0": TBM_VERT, "0 à +1": TBM_VERT,
        "+1 à +2": TBM_VERT, "+2 à +5": TBM_ORANGE,
        "+5 à +10": TBM_MAGENTA, "+10 à +20": TBM_MAGENTA, "> +20 min": TBM_MAGENTA,
    }
    data = [{"y": int(r["observations"]), "color": dist_color_map.get(r["plage"], TBM_MAGENTA)} for _, r in df.iterrows()]
    return {
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": df["plage"].tolist(), "title": {"text": "Écart à l'horaire théorique"}, "labels": {"rotation": -35}},
        "yAxis": {"title": {"text": "Nombre de passages"}},
        "series": [{"name": "Passages", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def collection_minutely_chart(df: pd.DataFrame) -> dict:
    data = [[int(pd.Timestamp(r["minute"]).timestamp() * 1000), int(r["observations"])] for _, r in df.iterrows()]
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
            "inputEnabled": False,
            "buttonTheme": {"fill": "#FFFFFF", "stroke": "#d5dae2", "style": {"color": TBM_GRIS_TEXTE, "fontSize": "11px"}},
        },
        "navigator": {
            "enabled": True,
            "series": {"color": TBM_BLEU, "lineWidth": 1},
            "xAxis": {"labels": {"style": {"color": TBM_GRIS_TEXTE}}},
        },
        "scrollbar": {
            "enabled": True,
            "barBackgroundColor": "#FFFFFF",
            "barBorderColor": "#d5dae2",
            "buttonBackgroundColor": "#eef1f5",
            "buttonBorderColor": "#d5dae2",
            "trackBackgroundColor": "#f6f8fb",
            "trackBorderColor": "#d5dae2",
        },
        "title": {"text": None},
        "series": [{"type": "line", "name": "Observations", "data": data, "color": TBM_BLEU, "marker": {"enabled": False}}],
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
    }


def hourly_distribution_chart(df: pd.DataFrame) -> dict:
    data = [int(r["observations"]) for _, r in df.iterrows()]
    return {
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": [str(int(r["heure"])) for _, r in df.iterrows()], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
        "series": [{"name": "Passages", "data": data, "color": TBM_BLEU}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.05, "pointPadding": 0.05}},
    }
