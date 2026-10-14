"""
How much do extra stops actually save? Same route, same stations, several refuelling strategies.

Usage (from the repo root):
  python research/prototype/compare_strategies.py "Los Angeles, CA" "New York, NY"
  python research/prototype/compare_strategies.py "Chicago, IL" "Dallas, TX" --reserve 50

Strategies:
  naive driver      drive until < 125 mi of fuel is left, then fill up at the next station (no optimizing)
  cheapest          lowest fuel cost, any number of stops
  min 10 gal        cheapest, but drop stops that buy < 10 gal (what plan_route.py does now)
  stop cost $X      lowest (fuel cost + X per stop): a stop must save more than X to be worth making
  fewest stops      minimum number of stops, cheapest among those
"""
import argparse, importlib.util, math, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("plan_route", HERE / "plan_route.py")
pr = importlib.util.module_from_spec(spec); spec.loader.exec_module(pr)

UNIT = 5.0   # fuel tracked in 5-mile steps for the stop-cost search


def nodes_of(cands, total_mi):
    n = [{"mile": 0.0, "price": None, "row": None}]
    n += [{"mile": r.mile, "price": r.price, "row": r} for r in cands.itertuples()]
    n.append({"mile": total_mi, "price": None, "row": None})
    return n


def plan_with_stop_cost(cands, total_mi, start_fuel, reserve, stop_cost):
    """Exact search: minimise fuel cost + stop_cost * number_of_stops.
    At each stop the best purchase is either 'just enough to reach the next stop' or 'fill up',
    so only those two options are tried for every possible next stop."""
    nodes = nodes_of(cands, total_mi)
    usable = int((pr.RANGE_MI - reserve) // UNIT)
    f0 = int((min(start_fuel, pr.RANGE_MI) - reserve) // UNIT)
    if f0 < 0:
        raise ValueError(0)
    miles = [n["mile"] for n in nodes]
    best = [dict() for _ in nodes]          # fuel_units -> (cost, stops, back)
    best[0][f0] = (0.0, 0, None)
    last = len(nodes) - 1
    for i in range(last):
        if not best[i]:
            continue
        price = nodes[i]["price"]
        for f, (c, k, _) in best[i].items():
            j = i + 1
            while j <= last:
                need = math.ceil((miles[j] - miles[i]) / UNIT - 1e-9)
                if need > usable:
                    break
                if price is None and need > f:      # at the start we can't buy
                    break
                options = {max(f, need)} if price is None else {max(f, need), max(f, usable)}
                for g in options:
                    if g < need:
                        continue
                    buy = g - f
                    cost = c + (buy * UNIT / pr.MPG * price + stop_cost if buy > 0 else 0)
                    stops = k + (1 if buy > 0 else 0)
                    arrive = g - need
                    cur = best[j].get(arrive)
                    if cur is None or (cost, stops) < (cur[0], cur[1]):
                        best[j][arrive] = (cost, stops, (i, f, buy))
                j += 1
    if not best[last]:
        raise ValueError(0)
    # walk back from the cheapest arrival
    arrive, (cost, _, back) = min(best[last].items(), key=lambda kv: (kv[1][0], kv[1][1]))
    stops, j = [], last
    while back:
        i, f, buy = back
        if buy > 0:
            r = nodes[i]["row"]
            stops.append({"mile": r.mile, "name": r.name, "city": r.city, "state": r.state, "price": r.price,
                          "gallons": buy * UNIT / pr.MPG, "cost": buy * UNIT / pr.MPG * r.price})
        _, _, back = best[i][f]
        j = i
    return stops[::-1]


def naive_driver(cands, total_mi, start_fuel, low=125.0):
    fuel, pos, stops = min(start_fuel, pr.RANGE_MI), 0.0, []
    rows = list(cands.itertuples())
    for k, r in enumerate(rows):
        fuel -= r.mile - pos; pos = r.mile
        if fuel < 0:
            raise ValueError(pos)
        nxt = rows[k + 1].mile if k + 1 < len(rows) else total_mi
        if fuel < low or fuel < nxt - pos:          # running low, or can't reach the next station
            if total_mi - pos <= fuel:
                break
            buy = pr.RANGE_MI - fuel
            stops.append({"mile": r.mile, "name": r.name, "price": r.price, "gallons": buy / pr.MPG,
                          "cost": buy / pr.MPG * r.price})
            fuel = pr.RANGE_MI
    if fuel - (total_mi - pos) < -1e-6:
        raise ValueError(pos)
    return stops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start"); ap.add_argument("finish")
    ap.add_argument("--reserve", type=float, default=0)
    ap.add_argument("--start-fuel", type=float, default=pr.RANGE_MI)
    a = ap.parse_args()

    stations = pr.pd.read_csv(pr.STATIONS); stations = stations[stations.precision != "none"]
    s, f = pr.locate(a.start), pr.locate(a.finish)
    coords, _, hours, refs = pr.get_route(s, f)
    cum = pr.along_route(coords); total = cum[-1]
    cands = pr.candidates(stations, coords, cum, refs)
    print(f"\n{a.start} -> {a.finish}: {total:.0f} mi, ~{hours:.1f} h, {len(cands)} stations on route, "
          f"fuel burned {total / pr.MPG:.0f} gal (start tank {a.start_fuel:.0f} mi, reserve {a.reserve:.0f} mi)\n")

    results = []
    def run(label, fn):
        t = time.time()
        try:
            st = fn()
            results.append((label, len(st), sum(x["gallons"] for x in st), sum(x["cost"] for x in st), time.time() - t, st))
        except ValueError:
            results.append((label, None, None, None, time.time() - t, []))

    run("naive driver (fill when <125 mi)", lambda: naive_driver(cands, total, a.start_fuel))
    run("cheapest (any # of stops)", lambda: pr.plan_stops(cands, total, a.start_fuel, a.reserve)[0])
    run("min 10 gal per stop", lambda: pr.plan_with_min_purchase(cands, total, a.start_fuel, a.reserve, 10)[0])
    for sc in (5, 10, 20, 40):
        run(f"stop cost ${sc}", lambda sc=sc: plan_with_stop_cost(cands, total, a.start_fuel, a.reserve, sc))
    run("fewest stops", lambda: plan_with_stop_cost(cands, total, a.start_fuel, a.reserve, 10_000))

    cheapest = min(r[3] for r in results if r[3] is not None)
    print(f"{'strategy':34s} {'stops':>5s} {'gallons':>8s} {'fuel cost':>10s} {'vs cheapest':>12s} {'time':>7s}")
    for label, n, g, c, dt, _ in results:
        if n is None:
            print(f"{label:34s}  -- not feasible --"); continue
        print(f"{label:34s} {n:5d} {g:8.1f} {c:10.2f} {c - cheapest:+12.2f} {dt * 1000:6.0f}ms")

    print("\nStops per strategy:")
    for label, n, g, c, dt, st in results:
        if st:
            print(f"  {label}: " + " → ".join(f"{x['name'][:18]} ({x['gallons']:.0f}g @ ${x['price']:.2f})" for x in st))


if __name__ == "__main__":
    main()
