"""Paginated, in-memory exports of the same authorized statistics report as the UI."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
import unicodedata

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.support_statistics import METRICS

FONT = "SupportDejaVu"
pdfmetrics.registerFont(TTFont(FONT, str(Path(__file__).parent / "fonts" / "DejaVuSans.ttf")))
LABELS = ("Active dashboard time", "Tickets claimed", "Tickets resolved", "Outbound messages sent")
ROLES = {"agent": "Agent", "admin": "Admin", "supervisor": "Super Admin"}


def display_text(value: object) -> str:
    text = str(value)
    # Preserve Unicode names, including joined Arabic, without accepting PDF markup.
    if any(unicodedata.bidirectional(char) in {"R", "AL"} for char in text):
        text = get_display(arabic_reshaper.reshape(text))
    return escape(text)


def statistics_pdf(report: dict) -> bytes:
    output = BytesIO()
    style = ParagraphStyle("Report", fontName=FONT, fontSize=9, leading=13)
    title = ParagraphStyle("Title", parent=style, fontSize=18, leading=24, spaceAfter=10)
    p = lambda value: Paragraph(display_text(value), style)

    def values(row):
        seconds = int(row["active_seconds"])
        return [f"{seconds // 3600}h {seconds % 3600 // 60}m {seconds % 60}s",
                *[str(row[key]) for key in METRICS[1:]]]

    selected = next((row["name"] for row in report["agents"] if str(row["id"]) == str(report.get("agent_id"))), "All staff")
    direction = "Highest first" if report["direction"] == "desc" else "Lowest first"
    story = [Paragraph("Agent Statistics", title),
             p(f'{report["start"]} through {report["end"]} · {report["timezone"]}'),
             p(f'Staff filter: {selected}'),
             p(f'Ranked by {LABELS[METRICS.index(report["rank_by"])]} · {direction}'), Spacer(1, 12)]
    data = [[p(x) for x in ("Rank", "Staff", *LABELS)]]
    data.append([p(""), p("Totals for selected staff"), *[p(x) for x in values(report["totals"])]])
    for row in report["rows"]:
        name = row["name"] + " · " + ROLES.get(row["role"], row["role"])
        if not row["is_active"]:
            name += " (inactive)"
        data.append([p(row["rank"]), p(name), *[p(x) for x in values(row)]])
    table = Table(data, colWidths=[35, 230, 120, 110, 110, 150], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6edf5")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f0f4f8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#ced6e0")),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [table, Spacer(1, 12)]
    for key, label in [("activity_tracking_started_at", "Active time tracking began"),
                       ("ticket_history_since", "Retained ticket audit begins"),
                       ("message_history_since", "Confirmed staff sends begin")]:
        story.append(p(f'{label}: {report.get(key) or "no retained records"} (UTC).'))
    story.append(p("Active time excludes idle, hidden and unfocused dashboards; no earlier activity is inferred. "
                   "Tickets count once per staff member in the selected dates; totals sum staff counts. "
                   "Messages include confirmed staff sends only, excluding bot/system messages and unidentifiable phone-app replies. "
                   "Deleted staff are excluded; retained conversation history is unchanged."))

    def footer(canvas, doc):
        canvas.setFont(FONT, 8)
        canvas.drawRightString(805, 20, f"Page {doc.page}")

    SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=36, rightMargin=36,
                      topMargin=30, bottomMargin=36, title="Agent Statistics", author="Customer Support").build(
                          story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
