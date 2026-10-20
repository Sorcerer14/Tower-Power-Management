#!/usr/bin/env python3
"""
tower_power_poc.py - proof-of-concept Monte-Carlo model of telecom-tower power continuity.

IDEA
    Downtime only happens when the battery empties before an AC source (grid or diesel
    generator, "DG") is back. So we simulate the *energy reserve* hour by hour, for many
    random site-years, and measure   P(annual downtime > 438 h).

    The "smart" policy does NOT replace the site controller. It only changes the
    controller's setpoints (when to start the DG, when to reorder fuel, when to test the DG)
    using forecasts. That is the layer a real deployment would push to the existing controller.

MODEL (one DC bus, 1-hour steps)
    sources : solar (on the DC bus) + grid or DG (through a rectifier)
    sinks   : load, battery charging, rectifier/battery losses, curtailed solar
    The same rectifier carries the load AND recharges the battery, so the stages are coupled.
    Every kWh is counted once; simulate_year() audits that the balance closes.

HOW TO READ THE CODE
    Site          all physical / stochastic parameters (illustrative - replace with fleet data)
    Policy        which controller levers are switched on
    draw_env()    random year: weather, load, grid outages, DG faults, fuel-delivery draws
    simulate_year()  the hourly physics + controller loop
    main()        Monte Carlo over years, summary table, CSV, charts
"""
from __future__ import annotations

import argparse
import csv
import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace

import numpy as np

H = 8760                      # hours per year
SLA_H = 438.0                 # 5 % of a year: allowed downtime for 95 % availability
A_FUEL, B_FUEL = 0.08415, 0.246   # genset fuel curve: L/h per rated kW, plus L per kWh output
CO2_KG_PER_L = 2.68           # diesel


# ----------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class Site:
    name: str = "urban"
    # load: radio/transport base + traffic-driven part + cooling that grows with temperature
    base_kw: float = 1.3
    traffic_kw: float = 0.9          # extra at peak traffic (5G / more users => raise this)
    traffic_scale: float = 1.0
    t_mean: float = 26.0
    t_seas: float = 9.0
    t_diur: float = 6.0
    # renewables
    solar_kwp: float = 4.0
    # battery on the DC bus
    batt_kwh: float = 20.0           # nominal capacity
    soc_min: float = 0.20            # low-voltage disconnect
    eta_c: float = 0.95
    eta_d: float = 0.95
    c_rate: float = 0.30             # max charge power = c_rate * nominal kWh
    rect_kw: float = 8.0             # rectifier DC output limit (load + charging)
    eta_rect: float = 0.95
    # grid outages
    out_per_year: float = 30.0
    out_median_h: float = 1.5
    out_sigma: float = 0.9           # lognormal spread -> heavy tail
    cluster_prob: float = 0.15       # chance an outage is followed by another soon (flapping)
    storms_per_year: float = 1.0
    storm_median_h: float = 20.0
    storm_sigma: float = 0.7
    storm_detect: float = 0.7        # fraction of storms the weather forecast flags in advance
    # diesel generator
    dg_kw: float = 10.0
    dg_p_start: float = 0.92         # per-attempt start probability when healthy
    dg_attempt_h: float = 0.1        # time per start attempt
    dg_fault_per_year: float = 2.0   # latent faults: DG will not start until repaired
    p_minor: float = 0.5             # share of faults fixed quickly (reset, loose wire)
    minor_med_h: float = 3.0
    major_med_h: float = 48.0
    major_sigma: float = 0.8
    # fuel logistics
    tank_l: float = 200.0
    lead_med_h: float = 8.0
    lead_sigma: float = 0.6
    vendor_fail: float = 0.03        # chance an order is badly delayed
    vendor_delay_med_h: float = 96.0
    theft_per_year: float = 0.5      # each event removes half the tank


PROFILES = {
    "urban": Site(),
    "semi_urban": Site(name="semi_urban", out_per_year=150, out_median_h=2.0, out_sigma=1.0,
                       cluster_prob=0.25, storms_per_year=2, storm_median_h=24,
                       lead_med_h=14, vendor_fail=0.05, theft_per_year=1.0),
    "rural": Site(name="rural", out_per_year=300, out_median_h=3.0, out_sigma=1.0,
                  cluster_prob=0.30, storms_per_year=3, storm_median_h=30,
                  lead_med_h=36, lead_sigma=0.7, vendor_fail=0.08,
                  theft_per_year=1.5, dg_fault_per_year=2.5),
    # deliberately harsh: very poor grid, slow/unreliable fuel, slow DG repairs
    "remote_stress": Site(name="remote_stress", out_per_year=450, out_median_h=4.0, out_sigma=1.1,
                          cluster_prob=0.35, storms_per_year=4, storm_median_h=36, storm_sigma=0.8,
                          lead_med_h=60, lead_sigma=0.8, vendor_fail=0.20, vendor_delay_med_h=200,
                          theft_per_year=2.0, dg_fault_per_year=3.0, p_minor=0.4,
                          major_med_h=120, major_sigma=1.0),
}


@dataclass(frozen=True)
class Policy:
    """Each flag is one controller lever, so we can measure each one's contribution."""
    name: str
    # DG start/stop rule
    autonomy_start: bool = False   # start DG on "hours of battery left", not a fixed SOC
    start_soc: float = 0.40        # baseline: fixed SOC start threshold
    stop_soc: float = 0.80         # DG stops when battery reaches this
    a_probe_h: float = 3.0         # smart: autonomy needed before the FIRST DG start in an outage
    a_proven_h: float = 1.5        # smart: once DG has started fine in this outage
    storm_ready: bool = False      # storm alert -> call DG at outage start, test DG, top up fuel
    self_test_h: int = 0           # weekly DG self-test (0 = never; faults found only on demand)
    smart_fuel: bool = False       # reorder from burn-rate x lead-time; backup order if late


BASELINE = Policy("baseline")
SMART = Policy("smart", autonomy_start=True, stop_soc=0.90, storm_ready=True,
               self_test_h=168, smart_fuel=True)
ABLATION = [
    Policy("+autonomy start only", autonomy_start=True, stop_soc=0.90),
    Policy("+storm readiness only", storm_ready=True),
    Policy("+DG self-test only", self_test_h=168),
    Policy("+fuel logic only", smart_fuel=True),
]


# ----------------------------------------------------------------------------- random year
def draw_env(site: Site, seed: int) -> dict:
    """Everything random about one site-year. Same seed => same weather/outages for every
    policy (common random numbers), so policy comparisons are paired and low-noise."""
    rng = np.random.default_rng(seed)
    hrs = np.arange(H)
    hod, doy = hrs % 24, hrs // 24

    # temperature -> cooling -> load
    temp = (site.t_mean + site.t_seas * np.cos(2 * np.pi * (doy - 160) / 365)
            + site.t_diur * np.cos(2 * np.pi * (hod - 15) / 24) + rng.normal(0, 1.0, H))
    traffic = 0.65 + 0.35 * np.cos(2 * np.pi * (hod - 20) / 24)        # evening peak
    it_kw = site.base_kw + site.traffic_kw * site.traffic_scale * traffic
    cooling = np.clip(0.02 * (temp - 18), 0, 0.5)                       # fraction of IT load
    load = it_kw * (1 + cooling) * rng.lognormal(0, 0.05)

    # solar: clear-sky shape x daily cloudiness (AR(1)); weaker in monsoon months
    clear = np.clip(np.sin(np.pi * (hod - 6) / 12), 0, None)
    day_mean = np.where((np.arange(365) >= 181) & (np.arange(365) <= 273), 0.45, 0.85)
    cloud, x = np.empty(365), 0.0
    for d in range(365):
        x = 0.6 * x + rng.normal(0, 0.18)
        cloud[d] = min(max(day_mean[d] + x, 0.1), 1.0)
    solar = site.solar_kwp * 0.8 * clear * cloud[doy]

    # grid outages: Poisson arrivals, lognormal durations, optional follow-up outage (flapping)
    up = np.ones(H, bool)
    alert = np.zeros(H, bool)

    def block(start, dur):
        i0 = int(start)
        up[i0:min(H, i0 + max(1, int(round(dur))))] = False

    for s in rng.uniform(0, H, rng.poisson(site.out_per_year)):
        t = s
        while t < H:
            d = rng.lognormal(math.log(site.out_median_h), site.out_sigma)
            block(t, d)
            if rng.random() < site.cluster_prob:
                t = t + d + rng.uniform(0.5, 4.0)
            else:
                break
    # storms: long outages; a share is forecast 24 h ahead (alert stays on during the storm)
    for s in rng.uniform(0, H, rng.poisson(site.storms_per_year)):
        d = rng.lognormal(math.log(site.storm_median_h), site.storm_sigma)
        block(s, d)
        if rng.random() < site.storm_detect:
            alert[max(0, int(s) - 24):min(H, int(s + d) + 1)] = True

    fault_arr = np.zeros(H, bool)
    fault_arr[rng.integers(0, H, rng.poisson(site.dg_fault_per_year))] = True
    theft = np.zeros(H, bool)
    theft[rng.integers(0, H, rng.poisson(site.theft_per_year))] = True

    # per-hour random draws, consumed only if the event happens that hour
    u = rng.random(H)
    k_att = np.minimum(1 + np.floor(np.log(np.maximum(u, 1e-12)) / math.log(1 - site.dg_p_start)), 3)
    lead = np.exp(math.log(site.lead_med_h) + site.lead_sigma * rng.standard_normal(H))
    lead += (rng.random(H) < site.vendor_fail) * np.exp(
        math.log(site.vendor_delay_med_h) + 0.6 * rng.standard_normal(H))
    minor = rng.random(H) < site.p_minor
    repair = np.where(minor,
                      np.exp(math.log(site.minor_med_h) + 0.5 * rng.standard_normal(H)),
                      np.exp(math.log(site.major_med_h) + site.major_sigma * rng.standard_normal(H)))

    # controller's 4-hour-ahead planning load (net of solar), with a 15 % safety factor
    net = np.maximum(load - solar, 0)
    pad = np.concatenate([net, net[:3]])
    plan = np.maximum.reduce([pad[i:i + H] for i in range(4)]) * 1.15

    return dict(load=load.tolist(), solar=solar.tolist(), up=up.tolist(), alert=alert.tolist(),
                fault_arr=fault_arr.tolist(), theft=theft.tolist(), k_att=k_att.astype(int).tolist(),
                lead_h=np.ceil(lead).astype(int).tolist(), rep_h=np.ceil(repair).astype(int).tolist(),
                plan=plan.tolist(), soh=float(rng.uniform(0.7, 1.0)), fuel0=float(rng.uniform(0.5, 1.0)))


# ----------------------------------------------------------------------------- one year
def simulate_year(site: Site, pol: Policy, env: dict, trace: bool = False) -> dict:
    load, solar, up, alert = env["load"], env["solar"], env["up"], env["alert"]
    plan, k_att, lead_h, rep_h = env["plan"], env["k_att"], env["lead_h"], env["rep_h"]

    Cnom = site.batt_kwh
    Ceff = Cnom * env["soh"]                      # usable-capacity scale: C_nominal x SOH
    floor = site.soc_min * Ceff
    chg_pmax = site.c_rate * Cnom
    E = Ceff                                      # battery energy (kWh), starts full
    fuel = site.tank_l * env["fuel0"]
    burn_run = A_FUEL * site.dg_kw + B_FUEL * site.rect_kw / site.eta_rect
    lead_p90 = site.lead_med_h * math.exp(1.2816 * site.lead_sigma)

    orders: list[int] = []                        # due hours of outstanding fuel orders
    first_order_h = 0
    fault = detected = False
    repair_end = 0
    dg_on = dg_proven = storm_outage = False
    elapsed = 0                                   # hours since this grid outage began
    ema_duty = 0.2                                # rolling DG duty cycle (~7 days)
    prev_alert = False

    r = dict(downtime_h=0.0, cause_no_fuel=0.0, cause_dg_fault=0.0, cause_reserve=0.0,
             dg_hours=0.0, dg_starts=0, diesel_l=0.0, grid_out_h=0, orders=0,
             solar_gen=0.0, solar_curtailed=0.0, load_kwh=0.0, grid_kwh=0.0, dg_kwh=0.0,
             audit_max=0.0, longest_blackout_h=0, min_soc=1.0)
    run = 0
    tr = dict(soc=[], dg=[], up=[], unserved=[]) if trace else None

    def place_order(h):
        orders.append(h + lead_h[h])
        r["orders"] += 1

    for h in range(H):
        L, S, grid, alrt = load[h], solar[h], up[h], alert[h]
        fuel_start = fuel

        # ---- exogenous events -------------------------------------------------------------
        if env["theft"][h]:
            fuel *= 0.5
        if env["fault_arr"][h]:
            fault = True
            if dg_on:                                # tripped while running: obvious
                detected, repair_end, dg_on = True, h + rep_h[h], False
            else:
                detected = False
        if fault and detected and h >= repair_end:
            fault = detected = False
        if orders and min(orders) <= h:              # delivery arrives, tank refilled
            fuel, orders = site.tank_l, []
        if grid:
            elapsed, dg_proven, storm_outage = 0, False, False
        else:
            if elapsed == 0:
                storm_outage = alrt                  # outage began under a storm alert
            r["grid_out_h"] += 1

        # ---- controller (the part a smart layer would set) ----------------------------------
        soc = E / Ceff
        usable = max(E - floor, 0.0)
        autonomy = usable * site.eta_d / plan[h] if plan[h] > 0 else 1e9   # hours the battery can carry load

        if pol.self_test_h and grid and h % pol.self_test_h == 10 or (pol.storm_ready and alrt and not prev_alert and grid):
            fuel = max(fuel - 0.3, 0.0)              # 15-minute DG test run
            if fault and not detected:               # test reveals a latent fault early
                detected, repair_end = True, h + rep_h[h]

        want = False
        if not grid and fuel > 0 and not (fault and detected):
            if pol.autonomy_start:
                if dg_on:
                    want = soc < pol.stop_soc
                else:
                    want = autonomy < (pol.a_proven_h if dg_proven else pol.a_probe_h)
            else:
                want = (soc < pol.stop_soc) if dg_on else (soc <= pol.start_soc)
            if pol.storm_ready and storm_outage and not dg_proven:
                want = True                          # long outage expected: prove the DG now

        if pol.smart_fuel:
            duty = max(ema_duty, 0.15)
            if not grid:
                duty = max(duty, 0.40)
            reserve = min(max(burn_run * duty * (lead_p90 + 12), 0.25 * site.tank_l), 0.9 * site.tank_l)
            late = orders and (h - first_order_h) > 1.5 * site.lead_med_h
            if fuel < reserve and (not orders or (late and len(orders) < 2)):
                if not orders:
                    first_order_h = h
                place_order(h)
        elif fuel < 0.25 * site.tank_l and not orders:
            first_order_h = h
            place_order(h)
        if pol.storm_ready and alrt and fuel < 0.9 * site.tank_l and not orders:
            first_order_h = h
            place_order(h)

        # ---- physics: who supplies the AC side this hour? -----------------------------------
        f_ac = 1.0 if grid else 0.0                  # fraction of the hour an AC source is up
        dg_f = 0.0
        if want:
            if fault:                                # undetected fault: start attempts all fail
                detected, repair_end, dg_on = True, h + rep_h[h], False
            else:
                if dg_on:
                    dg_f = 1.0
                else:
                    dg_f = 1.0 - k_att[h] * site.dg_attempt_h   # battery covers start-up delay
                    r["dg_starts"] += 1
                need = dg_f * (A_FUEL * site.dg_kw + B_FUEL * site.rect_kw / site.eta_rect)
                if fuel < need:
                    dg_f *= fuel / need              # fuel runs out mid-hour
                f_ac = dg_f
        dg_on = dg_f > 0
        if dg_on:
            dg_proven = True

        rr, ss = max(L - S, 0.0), max(S - L, 0.0)    # net load / surplus solar (kW)
        t_bat = 1.0 - f_ac                           # part of the hour with no AC source
        avail = max(E - floor, 0.0)
        if rr > 0 and t_bat > 0 and rr * t_bat / site.eta_d > avail:
            f_b = avail * site.eta_d / rr            # battery carries load until it hits the floor
        else:
            f_b = t_bat
        u = t_bat - f_b                              # hours of unserved load (DOWNTIME)
        served = f_ac + f_b

        E0 = E
        dis_bus = rr * f_b
        E -= dis_bus / site.eta_d
        sol_direct = min(S, L) * served
        sol_offer = S - sol_direct                   # solar left over -> battery, else curtailed
        chg_sol = min(sol_offer, chg_pmax, (Ceff - E) / site.eta_c)
        chg_ac = 0.0
        if f_ac > 0:                                 # the same rectifier recharges the battery
            target = Ceff if grid else pol.stop_soc * Ceff
            room = max(target - E - chg_sol * site.eta_c, 0.0) / site.eta_c
            chg_ac = max(min(room, max(site.rect_kw - rr, 0.0) * f_ac, chg_pmax - chg_sol), 0.0)
        ac_bus = rr * f_ac + chg_ac
        ac_in = ac_bus / site.eta_rect               # energy drawn from grid or DG: counted ONCE
        E += (chg_sol + chg_ac) * site.eta_c
        curtail = sol_offer - chg_sol

        # audit: sources = sinks + change in storage (never count the same kWh twice)
        lost = (ac_in - ac_bus) + (chg_sol + chg_ac) * (1 - site.eta_c) + dis_bus * (1 / site.eta_d - 1)
        resid = (S + ac_in) - (L * served + curtail + lost + (E - E0))
        r["audit_max"] = max(r["audit_max"], abs(resid))

        # ---- bookkeeping --------------------------------------------------------------------
        if dg_f > 0:
            p_out = ac_in / dg_f
            burn = min(dg_f * (A_FUEL * site.dg_kw + B_FUEL * p_out), fuel)
            fuel -= burn
            r["diesel_l"] += burn
            r["dg_hours"] += dg_f
            r["dg_kwh"] += ac_in
        else:
            r["grid_kwh"] += ac_in
        if u > 1e-9:
            r["downtime_h"] += u
            key = "cause_no_fuel" if fuel_start <= 0 else ("cause_dg_fault" if fault else "cause_reserve")
            r[key] += u
            run += 1
            r["longest_blackout_h"] = max(r["longest_blackout_h"], run)
        else:
            run = 0
        r["solar_gen"] += S
        r["solar_curtailed"] += curtail
        r["load_kwh"] += L * served
        r["min_soc"] = min(r["min_soc"], E / Ceff)
        ema_duty += ((1.0 if dg_f > 0 else 0.0) - ema_duty) / 168
        if not grid:
            elapsed += 1
        prev_alert = alrt
        if trace:
            tr["soc"].append(E / Ceff); tr["dg"].append(dg_f); tr["up"].append(grid); tr["unserved"].append(u)

    r["fuel_end_l"] = fuel
    if trace:
        r["trace"] = tr
    return r


# ----------------------------------------------------------------------------- Monte Carlo
def run_seed(args):
    site, pols, seed = args
    env = draw_env(site, seed)
    return [simulate_year(site, p, env) for p in pols]


def wilson(k: int, n: int, z: float = 1.96):
    """95 % interval for a probability estimated from k breaches in n years (works for k = 0)."""
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - w), min(1.0, c + w)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    d = np.array([x["downtime_h"] for x in rows])
    k = int((d > SLA_H).sum())
    lo, hi = wilson(k, n)
    tot = d.sum()
    cause = {c: (sum(x["cause_" + c] for x in rows) / tot if tot > 0 else 0.0)
             for c in ("no_fuel", "dg_fault", "reserve")}
    return dict(years=n, breaches=k, p_breach=k / n, p_breach_hi95=hi,
                p_gt_100h=float((d > 100).mean()), p_gt_24h=float((d > 24).mean()),
                mean_downtime_h=float(d.mean()), p95_downtime_h=float(np.percentile(d, 95)),
                p99_downtime_h=float(np.percentile(d, 99)), max_downtime_h=float(d.max()),
                grid_avail=1 - float(np.mean([x["grid_out_h"] for x in rows])) / H,
                diesel_l=float(np.mean([x["diesel_l"] for x in rows])),
                co2_t=float(np.mean([x["diesel_l"] for x in rows])) * CO2_KG_PER_L / 1000,
                dg_hours=float(np.mean([x["dg_hours"] for x in rows])),
                orders=float(np.mean([x["orders"] for x in rows])),
                audit_max_kwh=float(max(x["audit_max"] for x in rows)),
                share_no_fuel=cause["no_fuel"], share_dg_fault=cause["dg_fault"],
                share_reserve=cause["reserve"])


def monte_carlo(site, pols, years, seed0, workers=None):
    jobs = [(site, pols, seed0 + i) for i in range(years)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(run_seed, jobs, chunksize=4))
    return {p.name: [res[i][j] for i in range(years)] for j, p in enumerate(pols)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=300)
    ap.add_argument("--profiles", default="urban,semi_urban,rural,remote_stress")
    ap.add_argument("--ablation", default="remote_stress", help="profile for lever ablation ('' = skip)")
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    table, raw = [], {}
    for name in a.profiles.split(","):
        site = PROFILES[name]
        pols = [BASELINE, SMART] + (ABLATION if name == a.ablation else [])
        out = monte_carlo(site, pols, a.years, a.seed0)
        raw[name] = out
        for p in pols:
            s = summarize(out[p.name])
            table.append(dict(profile=name, policy=p.name, **s))
            print(f"{name:14s} {p.name:22s} P(>438h)={s['p_breach']*100:5.2f}% "
                  f"(95% upper {s['p_breach_hi95']*100:5.2f}%)  P(>100h)={s['p_gt_100h']*100:5.1f}%  "
                  f"mean={s['mean_downtime_h']:6.1f}h  P99={s['p99_downtime_h']:7.1f}h  "
                  f"diesel={s['diesel_l']:7.0f}L", flush=True)

    with open(os.path.join(a.out, "results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)

    # annual downtime per year, for plotting
    import pickle
    with open(os.path.join(a.out, "downtime_samples.pkl"), "wb") as f:
        pickle.dump({k: {p: [x["downtime_h"] for x in rows] for p, rows in v.items()}
                     for k, v in raw.items()}, f)


if __name__ == "__main__":
    main()
