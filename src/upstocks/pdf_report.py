"""Landscape PDF of a VWAP / SMC run — the same table as the page.

Columns, colours, row tints and the 8 totals mirror the results table on
/vwap (which mirrors the frontend app's tables). Font: SF Mono from macOS
(has ₹ → − ✓ ✗); falls back to Helvetica with "Rs" if it's missing.
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_FONT_FILE = Path("/System/Library/Fonts/SFNSMono.ttf")
if _FONT_FILE.exists():
    pdfmetrics.registerFont(TTFont("Mono", str(_FONT_FILE)))
    FONT, RS = "Mono", "₹"
else:  # no ₹ glyph in the built-in fonts
    FONT, RS = "Helvetica", "Rs "

UP, DOWN, AMBER, PURPLE, SLATE, MUTED, INK = "#16a34a", "#e11d48", "#b45309", "#7c3aed", "#64748b", "#94a3b8", "#1e293b"
CE_C, PE_C = "#0284c7", "#e11d48"
CONCEPT_COLOR = {"LiqGrab": "#7c3aed", "FVG": "#0284c7", "OrdBlock": "#b45309", "Breaker": "#ea580c", "SMTrap": "#e11d48"}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def charges(entry: float, exit_: float, qty: float) -> float:
    """The frontend app's calcCharges."""
    turnover = (entry + exit_) * qty
    brokerage, stt = 40, exit_ * qty * 0.000625
    exchange, clearing = turnover * 0.00053, turnover * 0.00003
    gst = (brokerage + exchange + clearing) * 0.18
    return brokerage + stt + exchange + clearing + gst + turnover * 0.000001 + entry * qty * 0.00003


def inr(n: float) -> str:
    """Indian digit grouping: 205494.96 → 2,05,494.96 (as the page shows)."""
    whole, frac = f"{abs(n):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{whole}.{frac}"


def money(n: float) -> str:
    return f"{RS}{inr(n)}"


def signed(n: float) -> str:
    return ("+" if n >= 0 else "−") + money(n)


def lot_short(n: float) -> str:
    a, sg = abs(n), "+" if n >= 0 else "−"
    if a >= 100000:
        return f"{sg}{RS}{a / 100000:.2f}L"
    if a >= 1000:
        return f"{sg}{RS}{a / 1000:.1f}K"
    return f"{sg}{RS}{a:.0f}"


def ampm(t: str | None) -> str:
    if not t:
        return "—"
    h, m = map(int, t.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}"


def exp_short(e: str) -> str:
    _, m, d = map(int, e.split("-"))
    return f"{d} {MONTHS[m - 1]}"


def build(days: list[dict], *, title: str, subtitle: str, lots: int, lot_size: int) -> bytes:
    qty = lots * lot_size
    trades = sorted((t for d in days for t in d["trades"]), key=lambda t: (t["date"], t["entry_time"]))
    smc = bool(trades) and trades[0].get("candle") == ""
    no_trade = [d for d in sorted(days, key=lambda d: d["date"]) if not d["trades"]]

    base = ParagraphStyle("b", fontName=FONT, fontSize=7, leading=9, textColor=colors.HexColor(INK))

    def p(html: str, size: float = 7, color: str = INK, leading: float | None = None) -> Paragraph:
        # leading fits the largest inline <font size> used in these cells (8)
        return Paragraph(html, ParagraphStyle("x", parent=base, fontSize=size, leading=leading or max(size, 7.2) + 2.3,
                                              textColor=colors.HexColor(color)))

    def c(txt: str, color: str, size: float = 7) -> str:
        return f'<font color="{color}" size="{size}">{txt}</font>'

    heads = ["#", "DATE", "TIME", "SIGNALS" if smc else "CONCEPTS", "STRIKE", "ENTRY", *(["CMP"] if smc else []), "SL", "T1", "T2",
             "STATUS", f"P&amp;L · LOT ({lots}×{lot_size}={qty})", "CHARGES", "MAX PTS", "MAX PROFITS"]
    rows = [[p(h, 5.5, SLATE) for h in heads]]
    tints: list[str] = []
    tgt = sl = eod = 0
    pts = chg = 0.0
    for i, t in enumerate(trades):
        entry, pnl = t["entry"], t["pnl"]
        exit_p = entry + pnl
        ch = charges(entry, exit_p, qty)
        lot_pnl = pnl * qty
        pts += pnl
        chg += ch
        st = t["status"]
        tgt += st == "TARGET"
        sl += st == "SL"
        eod += st not in ("TARGET", "SL")
        win = st == "TARGET" or (st == "TIME_EXIT" and pnl >= 0)
        loss = st == "SL" or (st == "TIME_EXIT" and pnl < 0)
        tints.append("#f0fdf4" if win else "#fff5f5" if loss else "#fefce8" if st == "EOD" else ("#ffffff" if i % 2 == 0 else "#fafafa"))
        st_clr = UP if win else DOWN if loss else AMBER if st == "EOD" else CE_C
        dir_c = CE_C if t["type"] == "CE" else PE_C
        t1 = round(entry * 1.12, 2) if smc else t["target"]
        t1_hit = st == "TARGET" or bool(t.get("t1_time"))
        sl_shown = entry if t.get("sl_to_breakeven") else t["sl"]
        pc = UP if pnl >= 0 else DOWN
        if smc:
            sig = c(f"{t['type']} {t.get('score', 0)}/5", dir_c) + "  " + "  ".join(
                c(x, CONCEPT_COLOR.get(x, SLATE)) for x in t.get("concepts", [])) + (c("  +EMA✓", UP) if t.get("trend_ok") else "")
            strike = c(f"NIFTY {exp_short(t['expiry'])} {t['strike']:.0f} {t['type']}", dir_c, 6.8) + "<br/>" + c(f"spot {t.get('spot', 0):.0f}", MUTED, 5.4)
        else:
            sig = c(t["type"], dir_c) + "  " + c(f"VWAP {money(t['vwap'])}", SLATE)
            strike = c(f"{t['strike']:.0f} {t['type']}", dir_c, 7.2) + "<br/>" + c(f"{exp_short(t['expiry'])} · spot {t.get('spot', 0):.0f}", MUTED, 5.4)
        label = {"TIME_EXIT": "TIME EXIT", "STAGNANT_EXIT": "STAGNANT"}.get(st, st)
        row = [
            p(str(i + 1), 5.9, MUTED),
            p(t["date"], 6.3),
            p(ampm(t["entry_time"]) + "<br/>" + c("→" + ampm(t["exit_time"]), MUTED, 5.4)),
            p(sig),
            p(strike),
            p(money(entry), 7.2, dir_c),
            *([p(money(exit_p), 6.8, SLATE)] if smc else []),
            p(money(sl_shown) + ("<br/>" + c("at entry", MUTED, 5) if t.get("sl_to_breakeven") else ""), 6.8, DOWN),
            p(money(t1), 6.8, AMBER),
            p(money(t["target"]), 6.8, UP),
            p(c(label, st_clr, 5.9) + "<br/>" + c("T1✓" if t1_hit else "T1✗", "#15803d" if t1_hit else MUTED, 5.4) + " "
              + c("T2✓" if st == "TARGET" else "T2✗", "#15803d" if st == "TARGET" else MUTED, 5.4) + "<br/>" + c(lot_short(lot_pnl), st_clr, 5.4)),
            p(c(signed(lot_pnl), pc, 7.2) + "<br/>" + c(f"{money(abs(pnl))} × {qty}", pc, 5.4) + "<br/>"
              + c(f"{'+' if pnl >= 0 else ''}{t['pnl_pct']:.2f}%", pc, 5.4) + "<br/>" + c(f"exit {money(exit_p)}", SLATE, 5.4)),
            p(c("−" + money(ch), AMBER, 6.8) + "<br/>" + c("incl. STT+GST", MUTED, 5)),
            p(c(f"+{t['peak']:.2f}", PURPLE, 6.8) + "<br/>" + c(f"+{t['peak'] / entry * 100:.1f}%", PURPLE, 5.4)) if t["peak"] > 0 else p("—", 6.3, MUTED),
            p(c("+" + money(t["peak"] * qty), PURPLE, 6.8) + "<br/>" + c(f"{t['peak']:.2f} × {qty}", PURPLE, 5.4)) if t["peak"] > 0 else p("—", 6.3, MUTED),
        ]
        rows.append(row)

    page_w = landscape(A4)[0] - 16 * mm
    widths_rel = [2.6, 7.5, 7, 14.5, 10.5, 6.5, *([6.5] if smc else []), 6.5, 6.5, 6.5, 7.2, 11.5, 9, 6, 11]
    scale = page_w / sum(widths_rel)
    table = LongTable(rows, colWidths=[w * scale for w in widths_rel], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor("#cbd5e1")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (0, -1), 6), ("RIGHTPADDING", (-1, 0), (-1, -1), 10),  # breathing room at both edges
    ]
    for r, tint in enumerate(tints, start=1):
        style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(tint)))
    table.setStyle(TableStyle(style))

    closed = tgt + sl
    lot_total = pts * qty
    stats = [
        ("TOTAL TRADES", str(len(trades)), INK, ""),
        ("TARGET HIT", str(tgt), UP, ""),
        ("SL HIT", str(sl), DOWN, ""),
        ("EOD / OPEN", str(eod), AMBER, ""),
        ("WIN RATE", f"{tgt / closed * 100:.1f}%" if closed else "—", UP if closed and tgt / closed >= 0.5 else DOWN, ""),
        ("PREMIUM P&amp;L", f"{'+' if pts >= 0 else '−'}{abs(pts):.2f} {RS.strip()}", UP if pts >= 0 else DOWN, ""),
        ("TOTAL CHARGES", "−" + money(chg), AMBER, "brokerage · STT · GST · NSE"),
        (f"LOT P&amp;L ({qty}×)", signed(lot_total), UP if lot_total >= 0 else DOWN, "before charges"),
    ]
    net = lot_total - chg
    net_c = UP if net >= 0 else DOWN
    cells = [[p(lbl, 6, SLATE), Spacer(0, 1.5), p(val, 8.8, clr, leading=11)] + ([p(sub, 5.5, MUTED, leading=7)] if sub else [])
             for lbl, val, clr, sub in stats]
    # The number that matters most: Lot P&L − charges, bigger and boxed.
    cells.append([p("NET P&amp;L · AFTER CHARGES", 6.5, SLATE), Spacer(0, 1.5), p(signed(net), 14, net_c, leading=17),
                  p("Lot P&amp;L − charges", 5.5, MUTED, leading=7)])
    unit = page_w / 9.45
    foot = Table([cells], colWidths=[unit] * 8 + [unit * 1.45])
    foot.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (-1, 0), (-1, 0), colors.HexColor("#f0fdf4" if net >= 0 else "#fff5f5")),
        ("LINEBEFORE", (-1, 0), (-1, 0), 1.6, colors.HexColor(net_c)),
    ]))

    story = [p(title, 13), p(subtitle, 7.5, SLATE), Spacer(0, 3 * mm), foot, Spacer(0, 4 * mm)]
    story.append(table if trades else p("No trades in this range.", 9, SLATE))
    if no_trade:
        story += [Spacer(0, 4 * mm), p(f"Days with no trade ({len(no_trade)})", 8, SLATE)]
        story += [p(f"{d['date']}: {d['note']}", 6.5, MUTED) for d in no_trade]

    def on_page(canv, doc):
        canv.saveState()
        canv.setFont(FONT, 6.5)
        canv.setFillColor(colors.HexColor(MUTED))
        canv.drawString(8 * mm, 6 * mm, f"upstocks · generated {datetime.now().strftime('%d %b %Y %H:%M')}")
        canv.drawRightString(landscape(A4)[0] - 8 * mm, 6 * mm, f"Page {doc.page}")
        canv.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=8 * mm, rightMargin=8 * mm, topMargin=9 * mm, bottomMargin=11 * mm,
                      title=title).build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
