#!/usr/bin/env python3
"""Charts for the tower-power POC. Reads out/downtime_samples.pkl (written by tower_power_poc.py).

fig1_exceedance.png : P(annual downtime > x) for baseline vs smart, one panel per site profile.
                      The SLA is where each curve crosses the dashed line at 438 h.
fig2_event_trace.png: one stress-site year where the smart layer avoids a long blackout:
                      battery SOC, DG running, grid down, unserved load - baseline vs smart.
"""
import pickle
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import tower_power_poc as t

BLUE, ORANGE = "#2a78d6", "#eb6834"      # validated categorical slots 1 and 2 (reference palette)
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({"font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                     "xtick.color": MUTED, "ytick.color": MUTED, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb"})

# ---------------------------------------------------------------- figure 1: exceedance curves
d = pickle.load(open("out/downtime_samples.pkl", "rb"))
names = ["urban", "semi_urban", "rural", "remote_stress"]
fig, axes = plt.subplots(1, 4, figsize=(15, 3.8), sharey=True)
for ax, n in zip(axes, names):
    for pol, col in (("baseline", BLUE), ("smart", ORANGE)):
        x = np.sort(np.array(d[n][pol]))
        y = 1 - np.arange(1, len(x) + 1) / len(x)            # P(downtime > x)
        ax.step(np.maximum(x, 0.1), np.maximum(y, 1e-4), where="post", color=col, lw=2, label=pol)
    ax.axvline(t.SLA_H, color=MUTED, ls="--", lw=1)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlim(0.1, 1500); ax.set_ylim(4e-4, 1.2)
    ax.set_title(n.replace("_", " "), color=INK, loc="left", fontsize=11)
    ax.set_xlabel("annual downtime (h)")
    ax.grid(color=GRID, lw=0.6)
    ax.text(t.SLA_H * 1.08, 0.5, "438 h SLA", color=MUTED, fontsize=8, rotation=90, va="center")
axes[0].set_ylabel("P(annual downtime > x)")
axes[0].legend(frameon=False, loc="lower left")
fig.suptitle("How often does a tower burn through its 438 h downtime budget? (2,000 simulated years per curve)",
             x=0.01, ha="left", color=INK, fontsize=12)
fig.tight_layout(rect=(0, 0, 1, 0.93))
fig.savefig("out/fig1_exceedance.png", dpi=150)

# ---------------------------------------------------------------- figure 2: event trace
site = t.PROFILES["remote_stress"]
best, best_seed = -1, None
for s in range(1000, 1200):                                   # search a few seeds for a clear example
    env = t.draw_env(site, s)
    b = t.simulate_year(site, t.BASELINE, env)["downtime_h"]
    m = t.simulate_year(site, t.SMART, env)["downtime_h"]
    if b - m > best and b < 300:
        best, best_seed = b - m, s
env = t.draw_env(site, best_seed)
rb = t.simulate_year(site, t.BASELINE, env, trace=True)
rs = t.simulate_year(site, t.SMART, env, trace=True)
ub = np.array(rb["trace"]["unserved"]); first = int(np.argmax(ub > 0.5))
w0, w1 = max(0, first - 48), min(t.H, first + 72)
hrs = np.arange(w0, w1)
fig, axes = plt.subplots(2, 1, figsize=(11, 5.6), sharex=True)
for ax, res, col, title in ((axes[0], rb, BLUE, "baseline controller"), (axes[1], rs, ORANGE, "smart layer")):
    tr = res["trace"]
    up = np.array(tr["up"])[w0:w1]; dg = np.array(tr["dg"])[w0:w1]; un = np.array(tr["unserved"])[w0:w1]
    ax.fill_between(hrs, 0, 1, where=~up, color="#eeeeea", step="mid", label="grid down")
    ax.fill_between(hrs, 0, 1, where=dg > 0, color=col, alpha=0.18, step="mid", label="DG running")
    ax.plot(hrs, np.array(tr["soc"])[w0:w1], color=col, lw=2, label="battery SOC")
    ax.fill_between(hrs, 0, 0.07, where=un > 0, color="#c0262d", step="mid", label="load unserved")
    ax.axhline(site.soc_min, color=MUTED, ls=":", lw=1)
    ax.set_ylim(0, 1); ax.set_ylabel("SOC")
    ax.set_title(f"{title}: {sum(tr['unserved']):.0f} h unserved over the year", loc="left", color=INK, fontsize=10)
    ax.grid(color=GRID, lw=0.6)
axes[1].set_xlabel(f"hour of year (seed {best_seed}, remote_stress profile)")
h, l = axes[1].get_legend_handles_labels()
fig.legend(h, l, frameon=False, ncol=4, loc="upper right", fontsize=8)
fig.tight_layout(rect=(0, 0, 1, 0.95))
fig.savefig("out/fig2_event_trace.png", dpi=150)
print("trace seed", best_seed, "baseline", round(rb["downtime_h"], 1), "smart", round(rs["downtime_h"], 1))
