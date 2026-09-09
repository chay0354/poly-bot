"""Local P&L dashboard. Read-only on Supabase. Does not talk to the bot or CLOB.

Shows only settled windows — never the in-progress game.
Open http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

from pm5.supabase_log import SupabaseLog

# Israel (IDT). Avoids the tzdata package on Windows.
IL = timezone(timedelta(hours=3))
HOST, PORT = "127.0.0.1", 8787


def _now() -> float:
    return time.time()


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _settled(row: dict, now: float) -> bool:
    end = row.get("window_end")
    if end is not None:
        try:
            return float(end) <= now - 1
        except (TypeError, ValueError):
            pass
    dt = _parse(row.get("ts"))
    return dt is not None and dt.timestamp() <= now - 1


def _il_day(dt: datetime) -> str:
    return dt.astimezone(IL).date().isoformat()


def _shape(row: dict) -> dict:
    up = float(row.get("up_shares") or 0)
    dn = float(row.get("down_shares") or 0)
    paired = abs(up - dn) < 0.5 and min(up, dn) > 0.01
    ts = _parse(row.get("ts"))
    return {
        "window": row.get("window_slug"),
        "ts": row.get("ts"),
        "when": ts.astimezone(IL).strftime("%H:%M") if ts else "",
        "date": _il_day(ts) if ts else "",
        "cost": float(row.get("cost") or 0),
        "pnl": None if row.get("estimated_pnl") is None else float(row["estimated_pnl"]),
        "result": row.get("result"),
        "up_shares": up,
        "down_shares": dn,
        "paired": paired,
        "strategies": row.get("strategies") or [],
        "up_won": row.get("up_won"),
    }


def snapshot() -> dict:
    sb = SupabaseLog()
    if not sb.enabled:
        return {"error": "supabase not configured"}
    now = _now()
    today = datetime.now(IL).date().isoformat()
    raw = (
        sb._sb.table("windows")
        .select("*")
        .eq("mode", "live")
        .order("ts", desc=True)
        .limit(80)
        .execute()
        .data
        or []
    )
    settled = [_shape(r) for r in raw if _settled(r, now)]
    day = [w for w in settled if w["date"] == today]
    last = settled[0] if settled else None

    def _sum(rows, key):
        return round(sum(float(r[key] or 0) for r in rows if r.get(key) is not None), 2)

    wins = [w for w in day if w["result"] == "win"]
    losses = [w for w in day if w["result"] == "loss"]
    flats = [w for w in day if w["result"] == "flat"]
    paired = [w for w in day if w["paired"]]
    naked = [w for w in day if not w["paired"]]
    pnls = [w["pnl"] for w in day if w["pnl"] is not None]
    chrono = list(reversed(day))
    cum, run = [], 0.0
    for w in chrono:
        if w["pnl"] is not None:
            run += w["pnl"]
        cum.append({"when": w["when"], "cum": round(run, 2), "pnl": w["pnl"]})

    last_fills = []
    if last:
        fills = (
            sb._sb.table("fills")
            .select("ts,side,price,shares,cost,strategy,maker")
            .eq("mode", "live")
            .eq("window_slug", last["window"])
            .order("ts")
            .execute()
            .data
            or []
        )
        last_fills = fills

    return {
        "now": datetime.now(IL).strftime("%H:%M:%S"),
        "today": today,
        "last": last,
        "last_fills": last_fills,
        "day": {
            "n": len(day),
            "wins": len(wins),
            "losses": len(losses),
            "flats": len(flats),
            "pnl": round(sum(pnls), 2) if pnls else 0.0,
            "spent": _sum(day, "cost"),
            "paired": len(paired),
            "naked": len(naked),
            "best": max(day, key=lambda w: w["pnl"] if w["pnl"] is not None else -1e9) if day else None,
            "worst": min(day, key=lambda w: w["pnl"] if w["pnl"] is not None else 1e9) if day else None,
        },
        "recent": settled[:16],
        "curve": cum,
    }


HTML = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>pm5 — תוצאות</title>
<style>
  :root {
    --bg:#0e1116; --card:#171b22; --line:#262c36;
    --txt:#e8edf4; --muted:#8b95a5;
    --win:#3dd68c; --loss:#ff6b6b; --flat:#c8b273; --accent:#6ea8fe;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; font-family: "Segoe UI", system-ui, sans-serif;
    background:var(--bg); color:var(--txt);
  }
  header {
    padding:18px 28px 8px; display:flex; justify-content:space-between; align-items:end;
  }
  h1 { margin:0; font-size:20px; font-weight:650; }
  .sub { color:var(--muted); font-size:13px; }
  main { padding:12px 28px 40px; display:grid; gap:14px; }
  .row { display:grid; gap:14px; grid-template-columns:repeat(4,1fr); }
  .wide { grid-column:1/-1; }
  .half { display:grid; grid-template-columns:1.1fr .9fr; gap:14px; }
  .card {
    background:var(--card); border:1px solid var(--line);
    border-radius:14px; padding:16px 18px;
  }
  .k { color:var(--muted); font-size:12px; margin-bottom:6px; }
  .v { font-size:28px; font-weight:700; letter-spacing:-.03em; }
  .win { color:var(--win); } .loss { color:var(--loss); } .flat { color:var(--flat); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { color:var(--muted); font-weight:500; text-align:right; padding:6px 8px; }
  td { padding:7px 8px; border-top:1px solid var(--line); }
  .pill {
    display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600;
  }
  .pill.win { background:#163526; color:var(--win); }
  .pill.loss { background:#3a1b1b; color:var(--loss); }
  .pill.flat { background:#332d16; color:var(--flat); }
  svg.chart { width:100%; height:120px; display:block; }
  .fill { font-size:13px; color:var(--muted); }
  @media (max-width:900px) {
    .row, .half { grid-template-columns:1fr 1fr; }
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>תוצאות הבוט</h1>
    <div class="sub">רק משחקים שנגמרו · בלי החלון הפתוח · מתעדכן כל 8 שניות</div>
  </div>
  <div class="sub" id="clock"></div>
</header>
<main>
  <div class="row" id="kpis"></div>
  <div class="half">
    <div class="card">
      <div class="k">המשחק הקודם</div>
      <div id="last"></div>
    </div>
    <div class="card">
      <div class="k">מצטבר היום</div>
      <svg class="chart" id="chart" viewBox="0 0 400 120" preserveAspectRatio="none"></svg>
    </div>
  </div>
  <div class="card wide">
    <div class="k">חלונות שנסגרו</div>
    <table>
      <thead><tr><th>שעה</th><th>תוצאה</th><th>PnL</th><th>עלות</th><th>Up / Down</th><th>סוג</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const money = n => n==null ? "—" : ((n>=0?"+":"") + n.toFixed(2) + "$");
const cls = n => n==null ? "flat" : (n>0.005 ? "win" : n<-0.005 ? "loss" : "flat");

function render(d) {
  $("clock").textContent = "עודכן " + d.now;
  const t = d.day;
  $("kpis").innerHTML = [
    kpi("PnL היום", money(t.pnl), cls(t.pnl)),
    kpi("משחקים היום", t.n, ""),
    kpi("ניצחונות / הפסדים", `${t.wins} / ${t.losses}`, t.wins>=t.losses?"win":"loss"),
    kpi("זוגות מאוזנים", `${t.paired} · חד־צדדי ${t.naked}`, ""),
  ].join("");

  const last = d.last;
  if (!last) {
    $("last").innerHTML = '<div class="fill">עוד אין משחק שנסגר</div>';
  } else {
    const fills = (d.last_fills||[]).map(f =>
      `${f.side} ${Number(f.shares).toFixed(2)} @ ${Number(f.price).toFixed(2)} ($${Number(f.cost).toFixed(2)}) ${f.strategy||""}`
    ).join("<br>");
    $("last").innerHTML = `
      <div class="v ${cls(last.pnl)}">${money(last.pnl)}</div>
      <div class="fill" style="margin:8px 0 10px">${last.when} · עלות $${last.cost.toFixed(2)} ·
        Up ${last.up_shares.toFixed(2)} / Down ${last.down_shares.toFixed(2)} ·
        ${last.paired ? "זוג נעול" : "לא מאוזן"}</div>
      <div class="fill">${fills || "אין מילויים"}</div>`;
  }

  const pts = d.curve || [];
  const svg = $("chart");
  if (pts.length < 2) {
    svg.innerHTML = "";
  } else {
    const ys = pts.map(p => p.cum);
    const min = Math.min(...ys, 0), max = Math.max(...ys, 0);
    const span = (max-min) || 1;
    const coords = pts.map((p,i) => {
      const x = (i/(pts.length-1))*400;
      const y = 110 - ((p.cum-min)/span)*100;
      return [x,y];
    });
    const dth = coords.map((c,i)=> (i?"L":"M")+c[0].toFixed(1)+","+c[1].toFixed(1)).join(" ");
    const color = pts[pts.length-1].cum >= 0 ? "#3dd68c" : "#ff6b6b";
    svg.innerHTML = `<path d="${dth}" fill="none" stroke="${color}" stroke-width="2.2"/>
      <line x1="0" y1="${(110-((0-min)/span)*100).toFixed(1)}" x2="400" y2="${(110-((0-min)/span)*100).toFixed(1)}" stroke="#262c36"/>`;
  }

  $("rows").innerHTML = d.recent.map(w => `
    <tr>
      <td>${w.when}</td>
      <td><span class="pill ${w.result||"flat"}">${w.result||"—"}</span></td>
      <td class="${cls(w.pnl)}">${money(w.pnl)}</td>
      <td>$${w.cost.toFixed(2)}</td>
      <td>${w.up_shares.toFixed(1)} / ${w.down_shares.toFixed(1)}</td>
      <td>${w.paired ? "זוג" : "צד אחד"}</td>
    </tr>`).join("");
}
function kpi(k,v,c) {
  return `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`;
}
async function tick() {
  try {
    const d = await (await fetch("/api/summary")).json();
    if (!d.error) render(d);
  } catch (e) {}
}
tick();
setInterval(tick, 8000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/summary"):
            body = json.dumps(snapshot()).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if self.path in {"/", "/index.html"}:
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"dashboard http://{HOST}:{PORT}  (settled windows only)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
