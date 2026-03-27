"""CentienC — Report generation: CSV, HTML, PDF exports."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("centient.reports")


# ═══════════════════════════════════════════════════════════════════
#  Data Builders
# ═══════════════════════════════════════════════════════════════════

async def build_report_data(db, engine, report_type: str, days: int = 7,
                            target_type: str | None = None,
                            target_id: int | None = None) -> dict[str, Any]:
    """Build comprehensive report data from DB + engine cache.

    report_type: 'overview' | 'server' | 'website' | 'service' | 'incidents' | 'bans'
    """
    data: dict[str, Any] = {
        "report_type": report_type,
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if report_type == "overview":
        # Cross-target summary
        summaries = await db.get_all_targets_summary(days)
        incidents = await db.get_incidents_for_period(days)

        # Enrich with target names
        servers = {s["id"]: s["name"] for s in await db.list_servers()}
        services = {s["id"]: s["name"] for s in await db.list_services()}
        websites = {w["id"]: w["name"] for w in await db.list_websites()}
        name_maps = {"server": servers, "service": services, "website": websites}

        for s in summaries:
            nm = name_maps.get(s["target_type"], {})
            s["name"] = nm.get(s["target_id"], f"#{s['target_id']}")

        data["summaries"] = summaries
        data["incidents"] = incidents
        data["totals"] = {
            "targets": len(summaries),
            "total_checks": sum(s["checks"] for s in summaries),
            "avg_uptime": round(sum(s["uptime_pct"] for s in summaries) / len(summaries), 2) if summaries else 100.0,
            "total_incidents": len(incidents),
        }

    elif report_type in ("server", "website", "service"):
        if target_id is None:
            # All targets of this type
            targets_map = {"server": db.list_servers, "website": db.list_websites, "service": db.list_services}
            targets = await targets_map[report_type]()
            all_daily = []
            for t in targets:
                daily = await db.get_daily_summary(report_type, t["id"], days)
                for d in daily:
                    d["target_id"] = t["id"]
                    d["name"] = t["name"]
                all_daily.extend(daily)
            data["daily"] = all_daily
            data["targets"] = targets
        else:
            daily = await db.get_daily_summary(report_type, target_id, days)
            hourly = await db.get_hourly_summary(report_type, target_id, min(days, 3))
            timeline = await db.get_status_timeline(report_type, target_id, days)
            incidents = await db.get_incidents_for_period(days, report_type, target_id)
            data["daily"] = daily
            data["hourly"] = hourly
            data["timeline"] = timeline
            data["incidents"] = incidents

    elif report_type == "incidents":
        incidents = await db.get_incidents_for_period(days, target_type, target_id)
        # Enrich with names
        servers = {s["id"]: s["name"] for s in await db.list_servers()}
        services = {s["id"]: s["name"] for s in await db.list_services()}
        websites = {w["id"]: w["name"] for w in await db.list_websites()}
        name_maps = {"server": servers, "service": services, "website": websites}
        for inc in incidents:
            nm = name_maps.get(inc["target_type"], {})
            inc["target_name"] = nm.get(inc["target_id"], f"#{inc['target_id']}")
        data["incidents"] = incidents

    elif report_type == "bans":
        # Aggregate fail2ban data from engine's live cache
        overview = engine.get_overview()
        ban_data = overview.get("fail2ban", {})
        data["fail2ban"] = ban_data
        # Also get server-level ban details from each cached server
        ban_details = []
        for key, cache_entry in engine.status_cache.items():
            if key.startswith("server:"):
                details = cache_entry.get("details", {})
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except (json.JSONDecodeError, TypeError):
                        details = {}
                f2b = details.get("fail2ban", {})
                if f2b:
                    sid = int(key.split(":")[1])
                    servers = {s["id"]: s["name"] for s in await db.list_servers()}
                    ban_details.append({
                        "server_id": sid,
                        "server_name": servers.get(sid, f"Server #{sid}"),
                        **f2b,
                    })
        data["ban_details"] = ban_details

    return data


# ═══════════════════════════════════════════════════════════════════
#  CSV Export
# ═══════════════════════════════════════════════════════════════════

def export_csv(report_data: dict[str, Any]) -> str:
    """Convert report data to CSV string."""
    output = io.StringIO()
    rtype = report_data.get("report_type", "overview")

    if rtype == "overview":
        writer = csv.writer(output)
        writer.writerow(["Type", "Name", "Checks", "Uptime %", "Avg Response (ms)", "Max Response (ms)"])
        for s in report_data.get("summaries", []):
            writer.writerow([
                s["target_type"], s["name"], s["checks"],
                s["uptime_pct"], s["avg_response"], s["max_response"],
            ])
        if report_data.get("incidents"):
            writer.writerow([])
            writer.writerow(["--- Incidents ---"])
            writer.writerow(["Type", "Target", "Status", "Started", "Resolved", "Duration (s)"])
            for inc in report_data["incidents"]:
                writer.writerow([
                    inc["target_type"], inc.get("target_name", inc["target_id"]),
                    inc["status"], inc["started_at"], inc.get("resolved_at", ""),
                    inc.get("duration", ""),
                ])

    elif rtype in ("server", "website", "service"):
        writer = csv.writer(output)
        writer.writerow(["Date", "Name", "Checks", "Uptime %", "Avg Response (ms)", "Min Response (ms)", "Max Response (ms)"])
        for d in report_data.get("daily", []):
            writer.writerow([
                d["day"], d.get("name", ""), d["checks"],
                d["uptime_pct"], d["avg_response"], d["min_response"], d["max_response"],
            ])

    elif rtype == "incidents":
        writer = csv.writer(output)
        writer.writerow(["Type", "Target", "Status", "Started", "Resolved", "Duration (s)"])
        for inc in report_data.get("incidents", []):
            writer.writerow([
                inc["target_type"], inc.get("target_name", inc["target_id"]),
                inc["status"], inc["started_at"], inc.get("resolved_at", ""),
                inc.get("duration", ""),
            ])

    elif rtype == "bans":
        writer = csv.writer(output)
        writer.writerow(["Server", "Total Bans", "Active Bans", "Jails"])
        for bd in report_data.get("ban_details", []):
            writer.writerow([
                bd["server_name"], bd.get("total_banned", 0),
                bd.get("currently_banned", 0),
                ", ".join(bd.get("jails", [])),
            ])

    return output.getvalue()


# ═══════════════════════════════════════════════════════════════════
#  HTML Export (beautiful self-contained report)
# ═══════════════════════════════════════════════════════════════════

_REPORT_CSS = """
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;padding:32px;max-width:1200px;margin:0 auto}
h1{font-size:1.8em;margin-bottom:6px;color:#58a6ff}
.meta{color:rgba(255,255,255,.45);font-size:.82em;margin-bottom:24px}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:28px}
.summary-card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:18px;text-align:center}
.summary-card .label{font-size:.72em;text-transform:uppercase;letter-spacing:.06em;color:rgba(255,255,255,.5);margin-bottom:4px}
.summary-card .value{font-size:2em;font-weight:700}
.c-green{color:#66bb6a}.c-blue{color:#58a6ff}.c-orange{color:#ffa726}.c-red{color:#f44336}.c-purple{color:#ab47bc}
table{width:100%;border-collapse:collapse;margin-bottom:24px}
thead{background:rgba(255,255,255,.06)}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid rgba(255,255,255,.06);font-size:.88em}
th{font-weight:600;color:rgba(255,255,255,.7);text-transform:uppercase;font-size:.72em;letter-spacing:.04em}
tr:hover{background:rgba(255,255,255,.03)}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.78em;font-weight:600}
.badge-up{background:rgba(102,187,106,.15);color:#66bb6a;border:1px solid rgba(102,187,106,.3)}
.badge-down{background:rgba(244,67,54,.15);color:#f44336;border:1px solid rgba(244,67,54,.3)}
.badge-open{background:rgba(255,167,38,.15);color:#ffa726;border:1px solid rgba(255,167,38,.3)}
.badge-resolved{background:rgba(102,187,106,.15);color:#66bb6a;border:1px solid rgba(102,187,106,.3)}
.uptime-bar{width:100%;height:8px;background:rgba(255,255,255,.08);border-radius:4px;overflow:hidden}
.uptime-fill{height:100%;border-radius:4px;transition:width .3s}
.chart-container{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:20px;margin-bottom:20px}
.chart-title{font-size:.9em;font-weight:600;color:#58a6ff;margin-bottom:14px}
h2{font-size:1.2em;margin:24px 0 12px;color:rgba(255,255,255,.8);border-bottom:1px solid rgba(255,255,255,.07);padding-bottom:8px}
.footer{margin-top:32px;padding-top:16px;border-top:1px solid rgba(255,255,255,.07);text-align:center;color:rgba(255,255,255,.35);font-size:.78em}
@media print{body{background:#fff;color:#111}table th,table td{border-color:#ddd}.summary-card{border-color:#ddd;background:#f8f9fa}.c-green{color:#2e7d32}.c-blue{color:#1565c0}.c-orange{color:#e65100}.c-red{color:#c62828}}
</style>
"""


def export_html(report_data: dict[str, Any], title: str = "CentienC Report") -> str:
    """Generate a beautiful self-contained HTML report with inline Chart.js."""
    rtype = report_data.get("report_type", "overview")
    days = report_data.get("days", 7)
    generated = report_data.get("generated_at", "")

    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        f"<title>{_escape(title)}</title>",
        "<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js'></script>",
        _REPORT_CSS,
        "</head><body>",
        f"<h1>{_escape(title)}</h1>",
        f"<p class='meta'>Period: last {days} days &middot; Generated: {_escape(generated)}</p>",
    ]

    if rtype == "overview":
        totals = report_data.get("totals", {})
        parts.append("<div class='summary-grid'>")
        parts.append(_summary_card("Targets", totals.get("targets", 0), "c-blue"))
        parts.append(_summary_card("Total Checks", f"{totals.get('total_checks', 0):,}", "c-purple"))
        parts.append(_summary_card("Avg Uptime", f"{totals.get('avg_uptime', 100):.1f}%", "c-green"))
        parts.append(_summary_card("Incidents", totals.get("total_incidents", 0), "c-orange"))
        parts.append("</div>")

        # Uptime table
        summaries = report_data.get("summaries", [])
        if summaries:
            parts.append("<h2>Target Summary</h2><table><thead><tr>")
            parts.append("<th>Type</th><th>Name</th><th>Uptime</th><th>Avg Response</th><th>Checks</th>")
            parts.append("</tr></thead><tbody>")
            for s in summaries:
                upt = s["uptime_pct"]
                color = "#66bb6a" if upt >= 99 else "#ffa726" if upt >= 95 else "#f44336"
                parts.append(f"<tr><td>{_escape(s['target_type'])}</td><td>{_escape(s['name'])}</td>")
                parts.append(f"<td><div class='uptime-bar'><div class='uptime-fill' style='width:{upt}%;background:{color}'></div></div>{upt}%</td>")
                parts.append(f"<td>{s['avg_response'] or '—'} ms</td><td>{s['checks']:,}</td></tr>")
            parts.append("</tbody></table>")

            # Chart: uptime by target
            labels = json.dumps([s["name"] for s in summaries])
            values = json.dumps([s["uptime_pct"] for s in summaries])
            colors = json.dumps([_uptime_color(s["uptime_pct"]) for s in summaries])
            parts.append(f"""
            <div class='chart-container'>
                <div class='chart-title'>Uptime by Target (%)</div>
                <canvas id='uptimeChart' height='200'></canvas>
            </div>
            <script>
            new Chart(document.getElementById('uptimeChart'),{{type:'bar',data:{{labels:{labels},datasets:[{{label:'Uptime %',data:{values},backgroundColor:{colors},borderRadius:6}}]}},options:{{responsive:true,scales:{{y:{{min:0,max:100,ticks:{{color:'rgba(255,255,255,.5)'}},grid:{{color:'rgba(255,255,255,.06)'}}}},x:{{ticks:{{color:'rgba(255,255,255,.5)'}},grid:{{display:false}}}}}},plugins:{{legend:{{display:false}}}}}}}});
            </script>""")

        # Incidents
        incidents = report_data.get("incidents", [])
        if incidents:
            parts.append(_incidents_table(incidents))

    elif rtype in ("server", "website", "service"):
        daily = report_data.get("daily", [])
        if daily:
            parts.append("<h2>Daily Performance</h2><table><thead><tr>")
            parts.append("<th>Date</th><th>Name</th><th>Uptime %</th><th>Avg Response</th><th>Min</th><th>Max</th><th>Checks</th>")
            parts.append("</tr></thead><tbody>")
            for d in daily:
                upt = d["uptime_pct"]
                color = "#66bb6a" if upt >= 99 else "#ffa726" if upt >= 95 else "#f44336"
                parts.append(f"<tr><td>{_escape(d['day'])}</td><td>{_escape(d.get('name',''))}</td>")
                parts.append(f"<td style='color:{color}'>{upt}%</td>")
                parts.append(f"<td>{d['avg_response'] or '—'} ms</td>")
                parts.append(f"<td>{d['min_response'] or '—'} ms</td>")
                parts.append(f"<td>{d['max_response'] or '—'} ms</td>")
                parts.append(f"<td>{d['checks']}</td></tr>")
            parts.append("</tbody></table>")

            # Charts
            _add_daily_charts(parts, daily)

        incidents = report_data.get("incidents", [])
        if incidents:
            parts.append(_incidents_table(incidents))

    elif rtype == "incidents":
        incidents = report_data.get("incidents", [])
        parts.append(f"<p style='margin-bottom:16px'>Total incidents in period: <strong>{len(incidents)}</strong></p>")
        if incidents:
            parts.append(_incidents_table(incidents, show_target=True))

    elif rtype == "bans":
        ban_details = report_data.get("ban_details", [])
        if ban_details:
            parts.append("<h2>Fail2Ban Summary</h2><table><thead><tr>")
            parts.append("<th>Server</th><th>Total Bans</th><th>Active Bans</th><th>Jails</th>")
            parts.append("</tr></thead><tbody>")
            for bd in ban_details:
                parts.append(f"<tr><td>{_escape(bd['server_name'])}</td>")
                parts.append(f"<td>{bd.get('total_banned', 0)}</td>")
                parts.append(f"<td>{bd.get('currently_banned', 0)}</td>")
                parts.append(f"<td>{', '.join(bd.get('jails', []))}</td></tr>")
            parts.append("</tbody></table>")
        else:
            parts.append("<p>No fail2ban data available for this period.</p>")

    parts.append("<div class='footer'>Generated by CentienC Monitoring System &middot; joshuagoth.com/tools/centient</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
#  PDF Export (HTML → PDF via browser print or WeasyPrint)
# ═══════════════════════════════════════════════════════════════════

def export_pdf_html(report_data: dict[str, Any], title: str = "CentienC Report") -> str:
    """Generate print-optimised HTML for PDF (client renders via window.print or server-side WeasyPrint)."""
    html = export_html(report_data, title)
    # Inject auto-print trigger and print-friendly tweaks
    print_script = """
    <script>
    // Auto-open print dialog for PDF save
    if (window.location.search.includes('autoprint=1')) {
        window.onload = function() { setTimeout(function(){ window.print(); }, 500); };
    }
    </script>
    <style>
    @media print {
        body { background: #fff !important; color: #000 !important; padding: 16px !important; }
        .summary-card { background: #f5f5f5 !important; border-color: #ddd !important; }
        .summary-card .value { color: #000 !important; }
        .chart-container { background: #fafafa !important; border-color: #ddd !important; }
        table th { background: #eee !important; color: #333 !important; }
        table td { border-color: #ddd !important; color: #333 !important; }
        .uptime-bar { background: #eee !important; }
        h1, h2, .chart-title { color: #1a73e8 !important; }
        .meta, .footer { color: #666 !important; }
    }
    </style>
    """
    return html.replace("</head>", print_script + "</head>")


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════

def _escape(text: Any) -> str:
    """HTML-escape a value."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _summary_card(label: str, value: Any, color_class: str) -> str:
    return f"<div class='summary-card'><div class='label'>{label}</div><div class='value {color_class}'>{value}</div></div>"


def _uptime_color(pct: float) -> str:
    if pct >= 99:
        return "rgba(102,187,106,.7)"
    if pct >= 95:
        return "rgba(255,167,38,.7)"
    return "rgba(244,67,54,.7)"


def _incidents_table(incidents: list[dict], show_target: bool = False) -> str:
    parts = ["<h2>Incidents</h2><table><thead><tr>"]
    if show_target:
        parts.append("<th>Type</th><th>Target</th>")
    parts.append("<th>Status</th><th>Started</th><th>Resolved</th><th>Duration</th></tr></thead><tbody>")
    for inc in incidents:
        badge = "badge-open" if inc["status"] == "open" else "badge-resolved"
        duration = ""
        if inc.get("duration"):
            m, s = divmod(int(inc["duration"]), 60)
            h, m = divmod(m, 60)
            duration = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
        parts.append("<tr>")
        if show_target:
            parts.append(f"<td>{_escape(inc['target_type'])}</td><td>{_escape(inc.get('target_name', inc['target_id']))}</td>")
        parts.append(f"<td><span class='badge {badge}'>{inc['status']}</span></td>")
        parts.append(f"<td>{_escape(inc['started_at'])}</td>")
        parts.append(f"<td>{_escape(inc.get('resolved_at') or '—')}</td>")
        parts.append(f"<td>{duration or '—'}</td></tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _add_daily_charts(parts: list[str], daily: list[dict]) -> None:
    """Add uptime + response time line charts from daily data."""
    labels = json.dumps([d["day"] for d in daily])
    uptimes = json.dumps([d["uptime_pct"] for d in daily])
    avg_resp = json.dumps([d["avg_response"] for d in daily])
    max_resp = json.dumps([d["max_response"] for d in daily])

    parts.append(f"""
    <div class='chart-container'>
        <div class='chart-title'>Daily Uptime %</div>
        <canvas id='dailyUptime' height='160'></canvas>
    </div>
    <script>
    new Chart(document.getElementById('dailyUptime'),{{type:'line',data:{{labels:{labels},datasets:[{{label:'Uptime %',data:{uptimes},borderColor:'#66bb6a',backgroundColor:'rgba(102,187,106,.1)',fill:true,tension:.3,pointRadius:3}}]}},options:{{responsive:true,scales:{{y:{{min:80,max:100,ticks:{{color:'rgba(255,255,255,.5)'}},grid:{{color:'rgba(255,255,255,.06)'}}}},x:{{ticks:{{color:'rgba(255,255,255,.5)',maxRotation:45}},grid:{{display:false}}}}}},plugins:{{legend:{{labels:{{color:'rgba(255,255,255,.7)'}}}}}}}}}});
    </script>

    <div class='chart-container'>
        <div class='chart-title'>Response Time (ms)</div>
        <canvas id='dailyResponse' height='160'></canvas>
    </div>
    <script>
    new Chart(document.getElementById('dailyResponse'),{{type:'line',data:{{labels:{labels},datasets:[{{label:'Avg',data:{avg_resp},borderColor:'#58a6ff',tension:.3,pointRadius:3}},{{label:'Max',data:{max_resp},borderColor:'rgba(255,167,38,.6)',borderDash:[5,3],tension:.3,pointRadius:2}}]}},options:{{responsive:true,scales:{{y:{{ticks:{{color:'rgba(255,255,255,.5)'}},grid:{{color:'rgba(255,255,255,.06)'}}}},x:{{ticks:{{color:'rgba(255,255,255,.5)',maxRotation:45}},grid:{{display:false}}}}}},plugins:{{legend:{{labels:{{color:'rgba(255,255,255,.7)'}}}}}}}}}});
    </script>""")
