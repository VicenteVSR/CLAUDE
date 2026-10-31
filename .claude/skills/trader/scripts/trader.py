#!/usr/bin/env python3
"""Herramienta de trading para la skill `trader` (Coinbase Advanced Trade).

Subcomandos: portfolio, analyze, scan, size, preview, order, journal.
Los datos de mercado usan endpoints públicos; portfolio/preview/order requieren
COINBASE_API_KEY y COINBASE_API_SECRET en el entorno.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

API_HOST = "api.coinbase.com"
BASE = f"https://{API_HOST}"

# --- Perfil de riesgo (mantener en sincronía con SKILL.md) ---
INITIAL_CAPITAL = 3000.0
RISK_PER_TRADE = 0.02      # 2 % del capital
MIN_RR = 2.0               # beneficio/riesgo mínimo
MAX_POSITION_PCT = 0.35    # 35 % del capital por activo
KILL_SWITCH_DD = 0.15      # parar nuevas entradas al 15 % de caída
MAX_DD = 0.20              # límite absoluto del usuario

WATCHLIST = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "AVAX-EUR",
             "LINK-EUR", "DOGE-EUR", "DOT-EUR", "LTC-EUR"]

ROOT = Path(__file__).resolve().parents[4]  # raíz del repo
JOURNAL = ROOT / "trading" / "journal"
TRADES_CSV = JOURNAL / "trades.csv"
EQUITY_CSV = JOURNAL / "equity.csv"
TRADE_FIELDS = ["timestamp", "product", "side", "size", "price", "eur", "stop", "target",
                "risk_eur", "order_id", "status", "pnl_eur", "notes"]

GRANULARITY_SECONDS = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900,
                       "THIRTY_MINUTE": 1800, "ONE_HOUR": 3600, "TWO_HOUR": 7200,
                       "SIX_HOUR": 21600, "ONE_DAY": 86400}


# ---------------------------------------------------------------- HTTP / auth
def _build_jwt(method: str, path: str) -> str:
    try:
        import jwt  # pyjwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        sys.exit("Faltan dependencias: pip install -r .claude/skills/trader/scripts/requirements.txt")
    key_name = os.environ.get("COINBASE_API_KEY")
    secret = os.environ.get("COINBASE_API_SECRET")
    if not key_name or not secret:
        sys.exit("Faltan COINBASE_API_KEY / COINBASE_API_SECRET en el entorno.")
    secret = secret.replace("\\n", "\n").strip()
    now = int(time.time())
    payload = {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
               "uri": f"{method} {API_HOST}{path}"}
    headers = {"kid": key_name, "nonce": secrets.token_hex(16)}
    if "BEGIN" in secret:  # clave EC en PEM -> ES256
        key = serialization.load_pem_private_key(secret.encode(), password=None)
        return jwt.encode(payload, key, algorithm="ES256", headers=headers)
    import base64  # clave Ed25519 en base64 (64 bytes: semilla + pública)
    raw = base64.b64decode(secret)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    return jwt.encode(payload, key, algorithm="EdDSA", headers=headers)


def api(method: str, path: str, params: dict | None = None, body: dict | None = None,
        auth: bool = False) -> dict:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "trader-skill/1.0")
    if auth:
        req.add_header("Authorization", "Bearer " + _build_jwt(method, path))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} en {path}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Sin conexión con {API_HOST} ({e.reason}). ¿Está permitido en la red del entorno?")


# ---------------------------------------------------------------- mercado
def price(product: str) -> float:
    return float(api("GET", f"/api/v3/brokerage/market/products/{product}")["price"])


def candles(product: str, tf: str = "ONE_HOUR", n: int = 300) -> list[dict]:
    sec = GRANULARITY_SECONDS[tf]
    end = int(time.time())
    start = end - sec * min(n, 349)
    res = api("GET", f"/api/v3/brokerage/market/products/{product}/candles",
              {"start": start, "end": end, "granularity": tf})
    out = [{k: float(c[k]) for k in ("start", "open", "high", "low", "close", "volume")}
           for c in res.get("candles", [])]
    return sorted(out, key=lambda c: c["start"])


# ---------------------------------------------------------------- indicadores
def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(cs: list[dict], period: int = 14) -> float | None:
    if len(cs) <= period:
        return None
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(cs, cs[1:])]
    a = sum(trs[:period]) / period
    for t in trs[period:]:
        a = (a * (period - 1) + t) / period
    return a


def indicators(cs: list[dict]) -> dict:
    closes = [c["close"] for c in cs]
    last = closes[-1]
    e20, e50, e200 = (ema(closes, p)[-1] for p in (20, 50, 200))
    macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    signal = ema(macd_line, 9)
    look = cs[-50:]
    vol20 = sum(c["volume"] for c in cs[-21:-1]) / 20 if len(cs) > 21 else None
    trend = ("alcista" if last > e50 > e200 else
             "bajista" if last < e50 < e200 else "lateral/mixta")
    return {
        "precio": last,
        "ema20": e20, "ema50": e50, "ema200": e200 if len(cs) >= 200 else None,
        "tendencia": trend,
        "rsi14": rsi(closes),
        "macd": macd_line[-1], "macd_senal": signal[-1],
        "macd_hist": macd_line[-1] - signal[-1],
        "atr14": atr(cs),
        "max_50": max(c["high"] for c in look), "min_50": min(c["low"] for c in look),
        "var_24_velas_pct": (last / closes[-25] - 1) * 100 if len(closes) > 25 else None,
        "vol_rel": cs[-1]["volume"] / vol20 if vol20 else None,
    }


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"
    return str(v)


# ---------------------------------------------------------------- diario / riesgo
def _ensure_journal() -> None:
    JOURNAL.mkdir(parents=True, exist_ok=True)
    if not TRADES_CSV.exists():
        with TRADES_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(TRADE_FIELDS)
    if not EQUITY_CSV.exists():
        with EQUITY_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "equity_eur"])


def equity_history() -> list[float]:
    _ensure_journal()
    with EQUITY_CSV.open() as f:
        return [float(r["equity_eur"]) for r in csv.DictReader(f)]


def drawdown(current: float) -> tuple[float, float]:
    peak = max(equity_history() + [INITIAL_CAPITAL, current])
    return peak, (peak - current) / peak


def log_trade(**row) -> None:
    _ensure_journal()
    row.setdefault("timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with TRADES_CSV.open("a", newline="") as f:
        csv.DictWriter(f, TRADE_FIELDS).writerow({k: row.get(k, "") for k in TRADE_FIELDS})


def position_size(equity: float, entry: float, stop: float, target: float | None = None) -> dict:
    if stop >= entry:
        raise ValueError("Solo largos: el stop debe estar por debajo de la entrada.")
    risk_unit = entry - stop
    max_risk = equity * RISK_PER_TRADE
    qty = max_risk / risk_unit
    notional = qty * entry
    capped = False
    if notional > equity * MAX_POSITION_PCT:
        notional = equity * MAX_POSITION_PCT
        qty = notional / entry
        capped = True
    res = {"cantidad": qty, "nominal_eur": notional, "riesgo_eur": qty * risk_unit,
           "riesgo_pct": qty * risk_unit / equity * 100, "stop_pct": risk_unit / entry * 100,
           "recortado_por_exposicion": capped}
    if target:
        res["rr"] = (target - entry) / risk_unit
    return res


# ---------------------------------------------------------------- comandos
def cmd_portfolio(_a) -> None:
    accounts, cursor = [], None
    while True:
        params = {"limit": 250, **({"cursor": cursor} if cursor else {})}
        res = api("GET", "/api/v3/brokerage/accounts", params, auth=True)
        accounts += res.get("accounts", [])
        if not res.get("has_next"):
            break
        cursor = res.get("cursor")
    rows, total = [], 0.0
    for acc in accounts:
        cur = acc["currency"]
        bal = float(acc["available_balance"]["value"]) + float(acc.get("hold", {}).get("value", 0))
        if bal <= 0:
            continue
        if cur == "EUR":
            px = 1.0
        else:
            px = None
            for quote in ("EUR", "USDC"):
                try:
                    px = price(f"{cur}-{quote}")
                    if quote == "USDC":
                        px *= price("USDC-EUR")
                    break
                except SystemExit:
                    continue
        val = bal * px if px else 0.0
        total += val
        rows.append((cur, bal, px, val))
    rows.sort(key=lambda r: -r[3])
    print(f"{'Activo':<8}{'Saldo':>18}{'Precio €':>14}{'Valor €':>12}{'%':>7}")
    for cur, bal, px, val in rows:
        pct = val / total * 100 if total else 0
        print(f"{cur:<8}{bal:>18.8f}{fmt(px):>14}{val:>12.2f}{pct:>6.1f}%")
    peak, dd = drawdown(total)
    print(f"\nTotal: {total:,.2f} €  | Máximo: {peak:,.2f} €  | Drawdown: {dd*100:.1f} %")
    if dd >= KILL_SWITCH_DD:
        print(f"⚠ INTERRUPTOR ACTIVADO (≥{KILL_SWITCH_DD*100:.0f} %): no abrir nuevas posiciones.")
    with EQUITY_CSV.open("a", newline="") as f:
        csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), f"{total:.2f}"])


def cmd_analyze(a) -> None:
    cs = candles(a.product, a.tf)
    if len(cs) < 30:
        sys.exit(f"Datos insuficientes para {a.product} ({len(cs)} velas).")
    ind = indicators(cs)
    if a.json:
        print(json.dumps(ind, indent=2))
        return
    print(f"== {a.product} [{a.tf}] — {len(cs)} velas ==")
    for k, v in ind.items():
        print(f"  {k:<16}{fmt(v)}")
    if ind["atr14"]:
        p, at = ind["precio"], ind["atr14"]
        print(f"  stop orientativo (1,5×ATR): {fmt(p - 1.5 * at)}  | objetivo 2R: {fmt(p + 3 * at)}")


def cmd_scan(a) -> None:
    print(f"{'Par':<10}{'Precio':>12}{'Tend.(1D)':>14}{'RSI 1D':>8}{'RSI 1H':>8}{'Var24h%':>9}{'ATR%1D':>8}")
    for p in a.products or WATCHLIST:
        try:
            d = indicators(candles(p, "ONE_DAY"))
            h = indicators(candles(p, "ONE_HOUR"))
        except SystemExit as e:
            print(f"{p:<10} error: {e}")
            continue
        print(f"{p:<10}{fmt(d['precio']):>12}{d['tendencia']:>14}{fmt(d['rsi14']):>8}"
              f"{fmt(h['rsi14']):>8}{fmt(h['var_24_velas_pct']):>9}"
              f"{fmt(d['atr14'] / d['precio'] * 100 if d['atr14'] else None):>8}")


def cmd_size(a) -> None:
    eq = a.equity or (equity_history() or [INITIAL_CAPITAL])[-1]
    res = position_size(eq, a.entry, a.stop, a.target)
    print(f"Capital de referencia: {eq:,.2f} €")
    for k, v in res.items():
        print(f"  {k:<26}{fmt(v)}")
    if a.target and res["rr"] < MIN_RR:
        print(f"⚠ Beneficio/riesgo {res['rr']:.2f} < {MIN_RR}: operación NO válida según las reglas.")


def _order_body(product: str, eur: float) -> dict:
    return {"product_id": product, "side": "BUY",
            "order_configuration": {"market_market_ioc": {"quote_size": f"{eur:.2f}"}}}


def _risk_checks(a) -> tuple[float, float]:
    eq = a.equity or (equity_history() or [INITIAL_CAPITAL])[-1]
    _, dd = drawdown(eq)
    if dd >= KILL_SWITCH_DD:
        sys.exit(f"Bloqueado: drawdown {dd*100:.1f} % ≥ interruptor {KILL_SWITCH_DD*100:.0f} %.")
    px = price(a.product)
    if a.stop >= px:
        sys.exit("El stop debe estar por debajo del precio actual.")
    if a.target and (a.target - px) / (px - a.stop) < MIN_RR:
        sys.exit(f"Beneficio/riesgo < {MIN_RR} al precio actual {px}: no válido.")
    qty = a.eur / px
    risk = qty * (px - a.stop)
    if risk > eq * RISK_PER_TRADE * 1.02:
        sys.exit(f"Riesgo {risk:.2f} € > {RISK_PER_TRADE*100:.0f} % del capital ({eq*RISK_PER_TRADE:.2f} €). Reduce --eur.")
    if a.eur > eq * MAX_POSITION_PCT * 1.02:
        sys.exit(f"Nominal {a.eur:.2f} € > {MAX_POSITION_PCT*100:.0f} % del capital.")
    print(f"Controles OK — precio {px}, cantidad ≈{qty:.8f}, riesgo {risk:.2f} € ({risk/eq*100:.2f} %)")
    return px, risk


def cmd_preview(a) -> None:
    _risk_checks(a)
    res = api("POST", "/api/v3/brokerage/orders/preview", body=_order_body(a.product, a.eur), auth=True)
    print(json.dumps(res, indent=2))


def cmd_order(a) -> None:
    if os.environ.get("TRADER_LIVE") != "1":
        sys.exit("Ejecución real deshabilitada (TRADER_LIVE != 1). Estamos en fase 1: solo propuestas.")
    if not a.confirm:
        sys.exit("Falta --confirm.")
    px, risk = _risk_checks(a)
    body = {"client_order_id": str(uuid.uuid4()), **_order_body(a.product, a.eur)}
    res = api("POST", "/api/v3/brokerage/orders", body=body, auth=True)
    if not res.get("success"):
        log_trade(product=a.product, side="BUY", eur=a.eur, stop=a.stop, target=a.target or "",
                  status="error", notes=json.dumps(res)[:300])
        sys.exit(f"Orden rechazada: {json.dumps(res)}")
    oid = res["success_response"]["order_id"]
    time.sleep(2)
    fill = api("GET", f"/api/v3/brokerage/orders/historical/{oid}", auth=True).get("order", {})
    size = float(fill.get("filled_size") or a.eur / px)
    avg = float(fill.get("average_filled_price") or px)
    log_trade(product=a.product, side="BUY", size=f"{size:.8f}", price=avg, eur=a.eur, stop=a.stop,
              target=a.target or "", risk_eur=f"{risk:.2f}", order_id=oid, status="open")
    print(f"Compra ejecutada: {size:.8f} a {avg} (orden {oid})")
    # Salida protegida: bracket (objetivo + stop) o stop-limit si no hay objetivo
    inc = api("GET", f"/api/v3/brokerage/market/products/{a.product}").get("base_increment", "0.00000001")
    decimals = len(inc.rstrip("0").split(".")[1]) if "." in inc.rstrip("0") else 0
    qty = f"{int(size * 10**decimals) / 10**decimals:.{decimals}f}"
    if a.target:
        cfg = {"trigger_bracket_gtc": {"base_size": qty, "limit_price": str(a.target),
                                       "stop_trigger_price": str(a.stop)}}
    else:
        cfg = {"stop_limit_stop_limit_gtc": {"base_size": qty, "stop_price": str(a.stop),
                                             "limit_price": str(round(a.stop * 0.995, 8)),
                                             "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}
    exit_res = api("POST", "/api/v3/brokerage/orders", auth=True,
                   body={"client_order_id": str(uuid.uuid4()), "product_id": a.product,
                         "side": "SELL", "order_configuration": cfg})
    ok = exit_res.get("success")
    print("Orden de salida colocada." if ok else f"⚠ FALLÓ la orden de salida: {json.dumps(exit_res)}")
    log_trade(product=a.product, side="SELL-PROTECT", size=qty, stop=a.stop, target=a.target or "",
              order_id=(exit_res.get("success_response") or {}).get("order_id", ""),
              status="placed" if ok else "error")


def cmd_journal(_a) -> None:
    _ensure_journal()
    with TRADES_CSV.open() as f:
        trades = list(csv.DictReader(f))
    closed = [t for t in trades if t["status"] == "closed" and t["pnl_eur"]]
    pnl = [float(t["pnl_eur"]) for t in closed]
    eq = equity_history()
    print(f"Operaciones registradas: {len(trades)} | cerradas: {len(closed)}")
    if pnl:
        wins = [p for p in pnl if p > 0]
        print(f"P&L total: {sum(pnl):.2f} € | acierto: {len(wins)/len(pnl)*100:.0f} % | "
              f"media: {sum(pnl)/len(pnl):.2f} €")
    if eq:
        peak, dd = drawdown(eq[-1])
        print(f"Equity actual: {eq[-1]:.2f} € | máximo: {peak:.2f} € | drawdown: {dd*100:.1f} %")
    for t in trades[-10:]:
        print("  ", {k: v for k, v in t.items() if v})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("portfolio")
    s = sub.add_parser("analyze")
    s.add_argument("product")
    s.add_argument("--tf", default="ONE_HOUR", choices=GRANULARITY_SECONDS)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("scan")
    s.add_argument("products", nargs="*")
    s = sub.add_parser("size")
    s.add_argument("--entry", type=float, required=True)
    s.add_argument("--stop", type=float, required=True)
    s.add_argument("--target", type=float)
    s.add_argument("--equity", type=float)
    for name in ("preview", "order"):
        s = sub.add_parser(name)
        s.add_argument("product")
        s.add_argument("--eur", type=float, required=True, help="importe en EUR a comprar")
        s.add_argument("--stop", type=float, required=True)
        s.add_argument("--target", type=float)
        s.add_argument("--equity", type=float)
        if name == "order":
            s.add_argument("--confirm", action="store_true")
    sub.add_parser("journal")
    a = p.parse_args()
    {"portfolio": cmd_portfolio, "analyze": cmd_analyze, "scan": cmd_scan, "size": cmd_size,
     "preview": cmd_preview, "order": cmd_order, "journal": cmd_journal}[a.cmd](a)


if __name__ == "__main__":
    main()
