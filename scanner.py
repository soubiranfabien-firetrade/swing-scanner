#!/usr/bin/env python3
"""
Scanner Swing Pullback V5 (port Python du script Pine) :
analyse une liste de symboles sur 2h / journalier / hebdomadaire et envoie les alertes
par Telegram, e-mail et tableau de bord HTML.

Usage :
    python scanner.py                       # unités de temps du config.yaml
    python scanner.py --tf 1d 1wk           # seulement journalier + hebdo
    python scanner.py --demo                # données synthétiques, aucun envoi (test)
    python scanner.py --no-send             # calcule et écrit le tableau de bord, sans envoyer
"""
from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import sys
import warnings
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

DEFAULT_PARAMS = dict(
    trend_len=50, slope_bars=5, use_weekly=True, weekly_len=30,
    pb_len=20, pb_bars=5, use_rsi=True, rsi_len=14, rsi_dip=45.0,
    stop_mode="pullback", atr_len=14, atr_mult=2.0, rr=2.0,
    hh_pivot=2, ema_long=180, table_fast=15, table_slow_monthly=50,
    pivot_lens=[5, 25, 50, 100], pivot_enabled=[True, True, True, True],
    mom_ema=15, linreg_on=True, linreg_len=11, sma_signal=True, signal_len=11,
)
DEFAULT_EVENTS = dict(buy=True, hh=True, ema180=True, table=True, pivot=True, ribbons=True, momentum=True)

RANK_CHART = {"2h": 0, "1d": 1, "1wk": 2}
RANK_HTF = {"D": 1, "W": 2, "M": 3}


# ═════════════════════════════════════════════════════════════════════════
#  INDICATEURS (équivalents des fonctions ta.* de Pine)
# ═════════════════════════════════════════════════════════════════════════
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up, dn = rma(d.clip(lower=0), n), rma((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0).where(up.notna())


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def linreg(s: pd.Series, n: int) -> pd.Series:
    """Valeur de la droite de régression linéaire au dernier point de chaque fenêtre."""
    if n <= 1:
        return s.copy()
    x = np.arange(n, dtype=float)
    xm = x.mean()
    sxx = ((x - xm) ** 2).sum()
    w = 1.0 / n + (x[-1] - xm) * (x - xm) / sxx
    return s.rolling(n).apply(lambda y: float(np.dot(w, y)), raw=True)


def pivot_conf(s: pd.Series, n: int, high: bool = True) -> pd.Series:
    """Pivot haut/bas confirmé : la valeur apparaît n barres APRÈS le sommet (pas de lecture du futur)."""
    if high:
        left = s.rolling(n).max().shift(1)
        right = s[::-1].rolling(n).max().shift(1)[::-1]
        piv = s.where((s >= left) & (s > right))
    else:
        left = s.rolling(n).min().shift(1)
        right = s[::-1].rolling(n).min().shift(1)[::-1]
        piv = s.where((s <= left) & (s < right))
    return piv.shift(n)


def rel_state(close: pd.Series, fast: int, slow: int) -> pd.Series:
    """1 = EMA courte > EMA lente (vert), -1 = en dessous (rouge), 0 = égal / indisponible."""
    a, b = ema(close, fast), ema(close, slow)
    return pd.Series(np.where(a > b, 1, np.where(a < b, -1, 0)), index=close.index)


# ═════════════════════════════════════════════════════════════════════════
#  DONNÉES
# ═════════════════════════════════════════════════════════════════════════
def _clean(df: pd.DataFrame) -> pd.DataFrame | None:
    df = df.rename(columns=lambda c: str(c).lower())
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    if not {"open", "high", "low", "close"} <= set(cols):
        return None
    df = df[cols].dropna(subset=["open", "high", "low", "close"])
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df if len(df) else None


def download(tickers: list[str], interval: str, period: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), 40):
        chunk = tickers[i:i + 40]
        try:
            raw = yf.download(chunk, interval=interval, period=period, group_by="ticker",
                              auto_adjust=True, threads=True, progress=False)
        except Exception as exc:  # réseau, quota...
            print(f"  ! téléchargement {interval} échoué pour {chunk[:3]}... : {exc}")
            continue
        if raw is None or raw.empty:
            continue
        for t in chunk:
            df = None
            if isinstance(raw.columns, pd.MultiIndex):
                if t in raw.columns.get_level_values(0):
                    df = raw[t]
                elif t in raw.columns.get_level_values(1):
                    df = raw.xs(t, axis=1, level=1)
            elif len(chunk) == 1:
                df = raw
            if df is not None:
                df = _clean(df.copy())
                if df is not None:
                    out[t] = df
    return out


def to_naive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df


def month_rule() -> str:
    major, minor = (int(x) for x in pd.__version__.split(".")[:2])
    return "ME" if (major, minor) >= (2, 2) else "M"


def resample_ohlc(d: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return d.resample(rule).agg(agg).dropna(subset=["close"])


def hourly_to_2h(h: pd.DataFrame, now_utc: pd.Timestamp, drop_partial_last: bool = True) -> pd.DataFrame:
    """Construit des bougies 2h ancrées sur l'ouverture de chaque séance (comme TradingView)."""
    idx = h.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    keep = (idx + pd.Timedelta(hours=1)) <= now_utc            # bougies 1h terminées uniquement
    h = h[keep]
    h = to_naive(h.set_axis(idx[keep]))
    if h.empty:
        return h
    day = h.index.normalize()
    pos = h.groupby(day).cumcount() // 2
    g = h.groupby([day, pos])
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), n=("close", "size"))
    out.index = pd.DatetimeIndex(h.index.to_series().groupby([day, pos]).first().values)
    if drop_partial_last and len(out) and out["n"].iloc[-1] < 2:
        out = out.iloc[:-1]
    return out.drop(columns="n")


def label_for(idx: pd.DatetimeIndex, kind: str) -> pd.DatetimeIndex:
    """Étiquette de la période (jour / semaine = dimanche / mois = fin de mois) d'une bougie."""
    d = idx.normalize()
    if kind == "D":
        return d
    if kind == "W":
        return d + pd.to_timedelta((6 - d.dayofweek) % 7, unit="D")
    return d.to_period("M").end_time.normalize()


def map_state(bars_idx: pd.DatetimeIndex, chart_tf: str, kind: str, st: pd.Series) -> np.ndarray:
    """Reporte l'état d'une UT (M/W/D) sur les bougies du graphique, comme le fait le script Pine."""
    labels = label_for(bars_idx, kind)
    r_c, r_h = RANK_CHART[chart_tf], RANK_HTF[kind]
    if r_h > r_c:       # UT supérieure : dernière période CLÔTURÉE
        s = st.shift(1).reindex(labels)
    elif r_h == r_c:    # même UT : la bougie elle-même
        s = st.reindex(labels)
    else:               # UT inférieure : dernier état connu
        s = st.reindex(labels, method="ffill")
    return s.fillna(0).to_numpy()


# ═════════════════════════════════════════════════════════════════════════
#  ANALYSE D'UN SYMBOLE SUR UNE UNITÉ DE TEMPS
# ═════════════════════════════════════════════════════════════════════════
def fmt(x: float) -> str:
    return f"{x:.2f}" if abs(x) >= 10 else f"{x:.4f}".rstrip("0").rstrip(".")


def analyze(tf: str, bars: pd.DataFrame, d_all, w_all, m_all, d_cplt, w_cplt, P: dict, E: dict):
    """Retourne (événements, instantané). Événements = [(clé, texte)] sur la dernière bougie CLÔTURÉE."""
    n = len(bars)
    c, h, l, o = bars["close"], bars["high"], bars["low"], bars["open"]
    cv, hv = c.to_numpy(), h.to_numpy()
    events: list[tuple[str, str]] = []

    # --- Indicateurs de base ---
    ema_tr = ema(c, P["trend_len"])
    ema_pb = ema(c, P["pb_len"])
    rsi_ = rsi(c, P["rsi_len"])
    atr_ = atr(bars, P["atr_len"])
    ema180 = ema(c, P["ema_long"])

    # --- Filtre hebdo : clôture de la SEMAINE PRÉCÉDENTE au-dessus de son EMA ---
    wc = w_all["close"]
    wok = (wc > ema(wc, P["weekly_len"])).astype(float)
    wprior = wok.shift(1).reindex(label_for(bars.index, "W"))
    weekly_ok = (wprior.fillna(0).to_numpy() > 0) if P["use_weekly"] else np.ones(n, bool)

    # --- Signal d'achat swing ---
    trend_up = ((c > ema_tr) & (ema_tr > ema_tr.shift(P["slope_bars"]))).to_numpy()
    swing_low = l.rolling(P["pb_bars"]).min()
    pullback = (swing_low <= ema_pb).to_numpy()
    rsi_ok = ((rsi_.rolling(P["pb_bars"]).min() <= P["rsi_dip"]).to_numpy()
              if P["use_rsi"] else np.ones(n, bool))
    trigger = (c > h.shift(1)).to_numpy()
    stop = (c - P["atr_mult"] * atr_) if P["stop_mode"] == "atr" else swing_low
    risk = (c - stop).to_numpy()
    target = (c + P["rr"] * (c - stop)).to_numpy()
    raw = trend_up & weekly_ok & pullback & rsi_ok & trigger & (risk > 0)
    sig = np.zeros(n, bool)
    last = -10 ** 9
    for i in np.flatnonzero(raw):                      # un seul signal par fenêtre de repli
        if i - last > P["pb_bars"]:
            sig[i] = True
            last = i
    if E["buy"] and sig[-1]:
        events.append(("buy", f"ACHAT SWING (entrée {fmt(cv[-1])} | SL {fmt(stop.iloc[-1])} | TP {fmt(target[-1])})"))

    # --- Cassure HH : 2 sommets pivots ascendants puis clôture au-dessus ---
    hp = P["hh_pivot"]
    pv = pivot_conf(h, hp, True).to_numpy()
    hh_prev = hh_latest = np.nan
    latest_bar, broken = -1, False
    hh_hit, hh_level = np.zeros(n, bool), np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(pv[i]):
            hh_prev, hh_latest, latest_bar, broken = hh_latest, pv[i], i - hp, False
        asc = (not np.isnan(hh_prev)) and (not np.isnan(hh_latest)) and hh_latest > hh_prev
        if (asc and not broken and i > latest_bar and i > 0 and cv[i] > hh_latest
                and cv[i - 1] <= hh_latest and trend_up[i] and weekly_ok[i]):
            hh_hit[i], hh_level[i], broken = True, hh_latest, True
    if E["hh"] and hh_hit[-1]:
        events.append(("hh", f"CASSURE HH (sommet {fmt(hh_level[-1])})"))

    # --- EMA 180 : couleur (rose = montante ou stable, violette = descendante) ---
    e_up = (ema180 >= ema180.shift(1)).to_numpy()
    if E["ema180"] and n > 1 and not np.isnan(ema180.iloc[-2]) and e_up[-1] != e_up[-2]:
        events.append((f"ema:{int(e_up[-1])}", f"EMA {P['ema_long']} " + ("ROSE (montante)" if e_up[-1] else "VIOLETTE (descendante)")))

    # --- Tableau Mensuel / Hebdo / Journalier ---
    st_m = rel_state(m_all["close"], P["table_fast"], P["table_slow_monthly"])
    st_w = rel_state(w_all["close"], P["table_fast"], P["ema_long"])
    st_d = rel_state(d_all["close"], P["table_fast"], P["ema_long"])
    tab = {"MENSUEL": map_state(bars.index, tf, "M", st_m),
           "HEBDO": map_state(bars.index, tf, "W", st_w),
           "JOURNALIER": map_state(bars.index, tf, "D", st_d)}
    colname = {1: "VERT", -1: "ROUGE", 0: "NEUTRE"}
    if E["table"] and n > 1:
        for name, arr in tab.items():
            if arr[-1] != arr[-2]:
                events.append((f"table:{name}:{int(arr[-1])}", f"{name} {colname[int(arr[-1])]}"))

    # --- Lignes pivots : clôture qui casse un niveau (graphique / journalier / hebdo) ---
    if E["pivot"] and n > 1:
        up_names, dn_names = [], []
        lens, enabled = P["pivot_lens"], P["pivot_enabled"]

        def series_for(df):
            res = []
            for ln in lens:
                res += [pivot_conf(df["high"], ln, True).ffill(), pivot_conf(df["low"], ln, False).ffill()]
            return res

        up_c = dn_c = False
        for k, s in enumerate(series_for(bars)):                 # niveaux de l'UT du graphique
            lv = s.iloc[-2]
            if enabled[k // 2] and not np.isnan(lv):
                up_c |= bool(cv[-1] > lv and cv[-2] <= lv)
                dn_c |= bool(cv[-1] < lv and cv[-2] >= lv)
        if up_c:
            up_names.append("GRAPHIQUE")
        if dn_c:
            dn_names.append("GRAPHIQUE")
        htfs = {"2h": [("JOURNALIER", d_cplt), ("HEBDO", w_cplt)], "1d": [("HEBDO", w_cplt)], "1wk": []}[tf]
        for name, df in htfs:
            up_h = dn_h = False
            for k, s in enumerate(series_for(df)):
                lv = s.iloc[-1]
                if enabled[k // 2] and not np.isnan(lv):
                    up_h |= bool(cv[-1] > lv and cv[-2] <= lv)
                    dn_h |= bool(cv[-1] < lv and cv[-2] >= lv)
            if up_h:
                up_names.append(name)
            if dn_h:
                dn_names.append(name)
        if up_names:
            events.append(("pivot_up:" + ",".join(up_names), f"CASSURE PIVOT HAUSSIÈRE ({', '.join(up_names)})"))
        if dn_names:
            events.append(("pivot_dn:" + ",".join(dn_names), f"CASSURE PIVOT BAISSIÈRE ({', '.join(dn_names)})"))

    # --- Rubans T / S / A / A-1 : prix au-dessus (bleu) ou en dessous (rose) de l'ouverture de la période ---
    ov = o.to_numpy()
    yr, mo = bars.index.year.to_numpy(), bars.index.month.to_numpy()
    oq = os_ = oy = oy1 = ov[0]
    cyo = np.nan
    rib = np.zeros((4, n), int)
    for i in range(n):
        if i > 0 and (yr[i], mo[i]) != (yr[i - 1], mo[i - 1]):
            m = mo[i]
            if m in (1, 4, 7, 10):
                oq = ov[i]
            if m in (1, 7):
                os_ = ov[i]
            if m == 1:
                oy1, oy, cyo = cyo, ov[i], ov[i]
        rib[:, i] = [1 if cv[i] > oq else -1, 1 if cv[i] > os_ else -1,
                     1 if cv[i] > oy else -1, 1 if cv[i] > oy1 else -1]
    if E["ribbons"] and n > 1:
        for name, row in zip(["TRIMESTRE", "SEMESTRE", "ANNÉE", "ANNÉE N-1"], rib):
            if row[-1] != row[-2]:
                events.append((f"rib:{name}:{row[-1]}", f"{name} {'BLEU' if row[-1] == 1 else 'ROSE'}"))

    # --- Momentum : EMA + ligne signal LinReg (vert = les deux montent, rouge = les deux baissent) ---
    e_l = ema(c, P["mom_ema"])
    bclose = linreg(c, P["linreg_len"]) if P["linreg_on"] else c
    sg = bclose.rolling(P["signal_len"]).mean() if P["sma_signal"] else ema(bclose, P["signal_len"])
    er, ef = (e_l > e_l.shift(1)).to_numpy(), (e_l < e_l.shift(1)).to_numpy()
    sr, sf = (sg > sg.shift(1)).to_numpy(), (sg < sg.shift(1)).to_numpy()
    st_mom = np.where(er & sr, 1, np.where(ef & sf, -1, 0))
    if E["momentum"] and n > 1 and st_mom[-1] != st_mom[-2]:
        events.append((f"mom:{st_mom[-1]}", f"MOMENTUM {({1: 'VERT', -1: 'ROUGE', 0: 'GRIS'})[int(st_mom[-1])]}"))

    snap = dict(bar=bars.index[-1].strftime("%Y-%m-%d %H:%M"), close=float(cv[-1]),
                ema180=int(e_up[-1]), mom=int(st_mom[-1]),
                M=int(tab["MENSUEL"][-1]), W=int(tab["HEBDO"][-1]), D=int(tab["JOURNALIER"][-1]),
                T=int(rib[0, -1]), S=int(rib[1, -1]), A=int(rib[2, -1]), A1=int(rib[3, -1]))
    return events, snap


# ═════════════════════════════════════════════════════════════════════════
#  ENVOI : TELEGRAM, E-MAIL, TABLEAU DE BORD
# ═════════════════════════════════════════════════════════════════════════
def send_telegram(lines_html: list[str]) -> bool:
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    chunk, size = [], 0
    chunks = []
    for ln in lines_html:
        if size + len(ln) > 3800:
            chunks.append("\n".join(chunk)); chunk, size = [], 0
        chunk.append(ln); size += len(ln) + 1
    if chunk:
        chunks.append("\n".join(chunk))
    ok = True
    for txt in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": txt, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        ok &= r.ok
        if not r.ok:
            print("  ! Telegram :", r.text[:200])
    return ok


def send_email(subject: str, body: str) -> bool:
    user, pwd, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pwd and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))) as s:
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception as exc:
        print("  ! E-mail :", exc)
        return False


CSS = """body{font-family:system-ui,sans-serif;margin:20px;background:#fafafa;color:#222}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #ddd;padding:5px 8px;text-align:left}th{background:#2d1e37;color:#fff;position:sticky;top:0}
.g{background:#c8f0d0}.r{background:#f8c9c9}.n{background:#e5e5e5}.b{background:#bfe9fb}.p{background:#fbc3f2}.v{background:#d9c4ec}
input{padding:6px;width:260px;margin:8px 0}small{color:#666}"""

SCRIPT = """function f(id,q){q=q.toLowerCase();document.querySelectorAll('#'+id+' tbody tr').forEach(function(r){r.style.display=r.innerText.toLowerCase().indexOf(q)>-1?'':'none'})}"""


def cell(state: int, kind: str) -> str:
    cls = {"tri": {1: "g", -1: "r", 0: "n"}, "rib": {1: "b", -1: "p", 0: "n"}, "ema": {1: "p", 0: "v"}}[kind][state]
    label = {"tri": {1: "VERT", -1: "ROUGE", 0: "–"}, "rib": {1: "BLEU", -1: "ROSE", 0: "–"},
             "ema": {1: "ROSE", 0: "VIOLETTE"}}[kind][state]
    return f'<td class="{cls}">{label}</td>'


def write_dashboard(state: dict) -> None:
    hist = state.get("history", [])[::-1][:300]
    snaps = state.get("snapshot", {})
    rows_h = "".join(
        f"<tr><td>{html.escape(x['time'])}</td><td><b>{html.escape(x['symbol'])}</b></td><td>{x['tf']}</td>"
        f"<td>{html.escape(x['bar'])}</td><td>{html.escape(x['text'])}</td></tr>" for x in hist)
    rows_s = ""
    for key in sorted(snaps):
        sym, tf = key.split("|")
        s = snaps[key]
        rows_s += (f"<tr><td><b>{html.escape(sym)}</b></td><td>{tf}</td><td>{s['bar']}</td><td>{fmt(s['close'])}</td>"
                   + cell(s["ema180"], "ema") + cell(s["mom"], "tri") + cell(s["M"], "tri") + cell(s["W"], "tri")
                   + cell(s["D"], "tri") + cell(s["T"], "rib") + cell(s["S"], "rib") + cell(s["A"], "rib")
                   + cell(s["A1"], "rib") + "</tr>")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scanner Swing Pullback</title><style>{CSS}</style></head><body>
<h1>Scanner Swing Pullback V5</h1><small>Dernière mise à jour : {now}</small>
<h2>Alertes récentes</h2><input placeholder="Filtrer (symbole, UT, événement…)" oninput="f('h',this.value)">
<table id="h"><thead><tr><th>Envoyée (UTC)</th><th>Symbole</th><th>UT</th><th>Bougie</th><th>Événements</th></tr></thead><tbody>{rows_h}</tbody></table>
<h2>État actuel par symbole</h2><input placeholder="Filtrer (symbole, UT…)" oninput="f('s',this.value)">
<table id="s"><thead><tr><th>Symbole</th><th>UT</th><th>Bougie</th><th>Clôture</th><th>EMA 180</th><th>Momentum</th>
<th>Mens.</th><th>Hebdo</th><th>Jour.</th><th>T</th><th>S</th><th>A</th><th>A-1</th></tr></thead><tbody>{rows_s}</tbody></table>
<script>{SCRIPT}</script></body></html>"""
    DASHBOARD_FILE.parent.mkdir(exist_ok=True)
    DASHBOARD_FILE.write_text(page, encoding="utf-8")


# ═════════════════════════════════════════════════════════════════════════
#  DONNÉES DE DÉMONSTRATION (test sans réseau)
# ═════════════════════════════════════════════════════════════════════════
def demo_data(tickers):
    daily, hourly = {}, {}
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    for k, t in enumerate(tickers):
        rng = np.random.default_rng(k)
        di = pd.bdate_range(end=today, periods=3000)
        p = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, len(di))))
        d = pd.DataFrame({"open": p * (1 + rng.normal(0, .003, len(di))), "close": p}, index=di)
        d["high"] = d[["open", "close"]].max(axis=1) * (1 + abs(rng.normal(0, .005, len(di))))
        d["low"] = d[["open", "close"]].min(axis=1) * (1 - abs(rng.normal(0, .005, len(di))))
        d["volume"] = rng.integers(1e5, 1e6, len(di)).astype(float)
        daily[t] = d
        hi = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h"), periods=24 * 250, freq="h")
        hi = hi[(hi.dayofweek < 5) & (hi.hour >= 13) & (hi.hour <= 20)]
        q = 100 * np.exp(np.cumsum(rng.normal(0, .004, len(hi))))
        h = pd.DataFrame({"open": q, "close": q * (1 + rng.normal(0, .001, len(hi)))}, index=hi)
        h["high"] = h[["open", "close"]].max(axis=1) * 1.001
        h["low"] = h[["open", "close"]].min(axis=1) * 0.999
        h["volume"] = 1e5
        hourly[t] = h
    return daily, hourly


# ═════════════════════════════════════════════════════════════════════════
#  PROGRAMME PRINCIPAL
# ═════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--tf", nargs="*", help="unités de temps : 2h 1d 1wk")
    ap.add_argument("--demo", action="store_true", help="données synthétiques, aucun envoi")
    ap.add_argument("--no-send", action="store_true", help="ne rien envoyer (écrit seulement le tableau de bord)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    P = {**DEFAULT_PARAMS, **(cfg.get("params") or {})}
    E = {**DEFAULT_EVENTS, **(cfg.get("events") or {})}
    tickers = list(dict.fromkeys(cfg["tickers"]))
    tfs = args.tf or cfg.get("timeframes", ["1d", "1wk", "2h"])
    no_send = args.no_send or args.demo
    now_utc = pd.Timestamp.now(tz="UTC")
    today = now_utc.tz_localize(None).normalize()

    print(f"Scan de {len(tickers)} symboles sur {tfs} ({now_utc:%Y-%m-%d %H:%M} UTC)")
    if args.demo:
        daily_raw, hourly_raw = demo_data(tickers)
    else:
        daily_raw = download(tickers, "1d", "12y")
        hourly_raw = download(tickers, "1h", "1y") if "2h" in tfs else {}

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("sent", []); state.setdefault("history", []); state.setdefault("snapshot", {})
    sent = set(state["sent"])
    new_alerts = []

    for sym in tickers:
        d = daily_raw.get(sym)
        if d is None or len(d) < 400:
            print(f"  - {sym} : historique insuffisant ou indisponible, ignoré")
            continue
        try:
            d_all = to_naive(d)
            w_all, m_all = resample_ohlc(d_all, "W"), resample_ohlc(d_all, month_rule())
            d_cplt = d_all[d_all.index.normalize() < today]
            w_cplt = w_all[w_all.index < today]
            for tf in tfs:
                if tf == "1d":
                    bars = d_cplt
                elif tf == "1wk":
                    bars = w_cplt.copy()
                    bars.index = bars.index - pd.Timedelta(days=6)      # début de semaine (lundi)
                elif tf == "2h":
                    h = hourly_raw.get(sym)
                    if h is None:
                        continue
                    bars = hourly_to_2h(h, now_utc)
                else:
                    print(f"  ! unité de temps inconnue : {tf}")
                    continue
                if len(bars) < 300:
                    print(f"  - {sym} [{tf}] : {len(bars)} bougies seulement, ignoré")
                    continue
                events, snap = analyze(tf, bars, d_all, w_all, m_all, d_cplt, w_cplt, P, E)
                state["snapshot"][f"{sym}|{tf}"] = snap
                fresh = [(k, t) for k, t in events if f"{sym}|{tf}|{snap['bar']}|{k}" not in sent]
                if fresh:
                    new_alerts.append((sym, tf, snap["bar"], fresh))
        except Exception as exc:
            print(f"  ! {sym} : erreur d'analyse : {exc}")

    # --- Envoi groupé ---
    if new_alerts:
        lines_html, lines_txt = [], []
        for sym, tf, bar, fresh in new_alerts:
            text = " ; ".join(t for _, t in fresh)
            lines_html.append(f"<b>{html.escape(sym)}</b> [{tf}] {bar}\n  • " + html.escape(text).replace(" ; ", "\n  • "))
            lines_txt.append(f"{sym} [{tf}] {bar}\n  • " + text.replace(" ; ", "\n  • "))
        print(f"\n{len(new_alerts)} alerte(s) :\n" + "\n".join(lines_txt))
        if not no_send:
            ok_t = send_telegram(["<b>Swing PB V5</b>"] + lines_html)
            ok_e = send_email(f"Swing PB V5 : {len(new_alerts)} alerte(s)", "\n\n".join(lines_txt))
            if not (ok_t or ok_e):
                print("  (aucun canal configuré : alertes visibles seulement dans le tableau de bord)")
            stamp = now_utc.strftime("%Y-%m-%d %H:%M")
            for sym, tf, bar, fresh in new_alerts:
                for k, t in fresh:
                    sent.add(f"{sym}|{tf}|{bar}|{k}")
                state["history"].append(dict(time=stamp, symbol=sym, tf=tf, bar=bar,
                                             text=" ; ".join(t for _, t in fresh)))
    else:
        print("Aucune nouvelle alerte.")

    state["sent"] = sorted(sent)[-6000:]
    state["history"] = state["history"][-1000:]
    if not args.demo:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_dashboard(state if not args.demo else {**state, "history": [
        dict(time="démo", symbol=s, tf=tf, bar=b, text=" ; ".join(t for _, t in f)) for s, tf, b, f in new_alerts]})
    print("Tableau de bord écrit :", DASHBOARD_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
