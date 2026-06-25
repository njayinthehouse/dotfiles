#!/usr/bin/env python3
"""sweettalker v2 tests — color core, decode, reward model, policy, levers.

Run:  python3 test_st.py     (pure stdlib; no terminal, no state touched)
"""

import random
import sweettalker as st


def test_oklab_roundtrip():
    for hx in ("#1d2021", "#ebdbb2", "#7aa2f7", "#ff5555", "#000000",
               "#ffffff", "#458588"):
        L, a, b = st.hex_to_oklab(hx)
        back = st.oklab_to_hex(L, a, b)
        err = max(abs(int(hx[i:i + 2], 16) - int(back[i:i + 2], 16))
                  for i in (1, 3, 5))
        assert err <= 1, (hx, back, err)


def test_decode_valid_and_legible():
    random.seed(7)
    mn = 99.0
    for _ in range(400):
        look = st.decode(st.random_genome())
        assert len(look["ansi"]) == 16
        for h in [look["fg"], look["bg"]] + look["ansi"]:
            assert len(h) == 7 and h[0] == "#"
        mn = min(mn, st.contrast(look["fg"], look["bg"]))
    assert mn >= st.MIN_CONTRAST - 0.3, mn        # contrast solved, not luck


def test_feature_vector_stable():
    random.seed(1)
    for _ in range(50):
        assert len(st.feature_vector(st.decode(st.random_genome()))) == st.N_FEATURES


def test_reward_model_recovers_taste():
    random.seed(0)

    def truth(look):                              # likes dark bg + ligatures
        L = st.hex_to_oklch(look["bg"])[0]
        return -3.0 * L + 1.5 * st._font_attrs(look["font"])["ligatures"]

    comps = []
    for _ in range(120):
        ga, gb = st.random_genome(), st.random_genome()
        win = "a" if truth(st.decode(ga)) > truth(st.decode(gb)) else "b"
        comps.append({"a": ga, "b": gb, "winner": win})
    m = st.fit_model(comps)
    w = dict(zip(st.FEATURE_NAMES, m["w"]))
    assert w["bg.L"] < -0.5, w["bg.L"]
    assert w["font.ligatures"] > 0.5, w["font.ligatures"]


def test_policy_explore_exploit_flip():
    random.seed(0)

    def truth(look):
        return -3.0 * st.hex_to_oklch(look["bg"])[0]

    comps = [{"a": (a := st.random_genome()), "b": (b := st.random_genome()),
              "winner": "a" if truth(st.decode(a)) > truth(st.decode(b)) else "b"}
             for _ in range(120)]
    m = st.fit_model(comps)
    state = {"comparisons": comps, "genome": st.random_genome()}
    pop = [st.utility(m, st.random_genome()) for _ in range(300)]

    def mean(forced, n=120):
        rs = [st.predict_rating(m, st.policy_roll(state, m, forced=forced)[0], pop)[0]
              for _ in range(n)]
        return sum(rs) / len(rs)

    hi, lo = mean("exploit"), mean("explore")
    assert hi > lo + 3.0, (hi, lo)               # exploit high, explore flips low


def test_lever_mutation_isolated():
    random.seed(2)
    g = st.random_genome()
    assert "size" not in st.LEVERS                 # size is the terminal's job now
    assert "size" not in g["font"]                 # not a genome knob
    for lev, key in (("foreground", "fg"), ("background", "bg"),
                     ("prompt", "prompt"), ("palette", "palette")):
        m = st.mutate_genome(g, lev)
        assert m[key] != g[key]
        for other in ("prompt", "palette"):      # spot-check others held
            if other != key:
                assert m[other] == g[other]


def test_informative_opponent_is_a_coin_flip():
    random.seed(0)

    def truth(look):
        return -3.0 * st.hex_to_oklch(look["bg"])[0]

    comps = [{"a": (a := st.random_genome()), "b": (b := st.random_genome()),
              "winner": "a" if truth(st.decode(a)) > truth(st.decode(b)) else "b"}
             for _ in range(120)]
    m = st.fit_model(comps)
    base = st.random_genome()
    state = {"comparisons": comps, "genome": base}
    u_a = st.utility(m, base)
    # the chosen opponent's utility should sit closer to the current look's than a
    # random opponent's does (P(win) nearer 0.5 = harder to predict = informative)
    chosen_gap = sum(abs(u_a - st.utility(m, st.informative_opponent(state, m)))
                     for _ in range(30)) / 30
    random_gap = sum(abs(u_a - st.utility(m, st.random_genome()))
                     for _ in range(200)) / 200
    assert chosen_gap < random_gap, (chosen_gap, random_gap)


def test_refine_is_local():
    random.seed(4)
    g = st.random_genome()
    # a refine tweak keeps the look close: small contrast/lightness moves, font
    # family preserved in whole-look refine.
    for _ in range(20):
        t = st.perturb_genome(g)
        assert t["font"]["family"] == g["font"]["family"]      # font held in whole refine
        assert abs(t["bg"]["L"] - g["bg"]["L"]) < 0.25
    # lever refine touches only that lever's knobs
    assert st.perturb_genome(g, "background")["fg"] == g["fg"]
    assert st.perturb_genome(g, "foreground")["bg"] == g["bg"]


def test_exploration_rate_controls():
    assert st.exploration_rate({"explore_rate": 0.42}) == 0.42
    auto_empty = st.exploration_rate({"comparisons": []})
    auto_many = st.exploration_rate({"comparisons": [0] * 300})
    assert auto_empty > auto_many                # decays with data
    assert auto_many >= st.EXPLORE_MIN


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
