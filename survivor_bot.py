"""Survivor bot v3: duel lone hunters, dodge lasers/bullets, hide behind buildings to regen, stay in zone."""
import json, math, os, sys
import mechfight as mf

CELL = 3.0
TAU = 2 * math.pi


def pip(x, y, ring):
    ins = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = ring[2 * i], ring[2 * i + 1], ring[2 * j], ring[2 * j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            ins = not ins
        j = i
    return ins


def angdiff(a, b):
    return abs((a - b + math.pi) % TAU - math.pi)


UPG = ["plating", "armor", "nanites", "servos", "gun_damage", "laser_damage", "gun_mag", "capacitor",
       "jets", "gyros", "gun_rate", "lightning_efficiency", "reactor", "sensors"]


class Bot:
    name = "survivor3"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.reach = self.cfg["loot"]["pickup_radius"]
        self.solid = set()
        try:
            w = json.load(open(os.path.join(hello["map"]["dir"], "v2/world.json")))
            for p in w["prisms"]:
                if p["kind"] == "bridge" and p["base"] > 3:
                    continue
                r = p["ring"]
                xs, ys = r[0::2], r[1::2]
                holes = p.get("holes") or []
                for cx in range(int(min(xs) // CELL), int(max(xs) // CELL) + 1):
                    for cy in range(int(min(ys) // CELL), int(max(ys) // CELL) + 1):
                        px, py = (cx + .5) * CELL, (cy + .5) * CELL
                        if pip(px, py, r) and not any(pip(px, py, h) for h in holes):
                            self.solid.add((cx, cy))
        except Exception as e:
            print("map load failed", e, file=sys.stderr)
        self.last_move = (1.0, 0.0)
        self.dmg = None  # (bearing, t)

    # ---------- map helpers
    def blocked(self, x, y):
        return (int(x // CELL), int(y // CELL)) in self.solid

    def clear_dist(self, x, y, dx, dy, dist=18.0):
        s = 0.0
        while s < dist:
            s += CELL
            if s > dist:
                break
            if self.blocked(x + dx * s, y + dy * s):
                return s - CELL
        return dist

    def los(self, ax, ay, bx, by):
        d = math.hypot(bx - ax, by - ay)
        if d < 4:
            return True
        return self.clear_dist(ax, ay, (bx - ax) / d, (by - ay) / d, d) >= d - CELL

    # ---------- main
    def act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        t = obs["t"]
        tick = obs["tick"]
        act = {"tick": tick}

        if obs["offer"]:
            ch = [c["id"] for c in obs["offer"]["choices"]]
            pick, br = 0, 99
            for i, c in enumerate(ch):
                r = UPG.index(c) if c in UPG else 50
                if r < br:
                    br, pick = r, i
            act["pick"] = pick

        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.dmg = (e["bearing"], t)

        zone = obs["zone"]
        if t >= zone["shrink_start"] - 15:
            zc, zr = zone["next_center"], zone["next_radius"]
        else:
            zc, zr = zone["center"], zone["radius"]
        dzx, dzy = zc[0] - x, zc[1] - y
        dz = math.hypot(dzx, dzy) or 1e-6
        tgt_r = max(zr * 0.4, 8.0)

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs:
            m["_d"] = math.hypot(m["pos"][0] - x, m["pos"][1] - y)
        mechs.sort(key=lambda m: m["_d"])
        hpf = me["hp"] / me["max_hp"]
        kinds = [w["kind"] for w in me["weapons"] if w]

        # threat points for cover logic
        tp = [(m["pos"][0], m["pos"][1]) for m in mechs if m["_d"] < 190][:3]
        recent_dmg = self.dmg is not None and t - self.dmg[1] < 2.5
        if not tp and recent_dmg:
            b = self.dmg[0]
            tp = [(x + 90 * math.cos(b), y + 90 * math.sin(b))]

        vx = vy = 0.0
        if dz > tgt_r:
            k = min(1.0, (dz - tgt_r) / 40.0 + 0.3)
            if not me["in_zone"]:
                k = 2.5
            vx += dzx / dz * k
            vy += dzy / dz * k

        # ---- pick a target: lowest-HP / closest enemy mech we can shoot; else NPC / crate for cubes
        tm, tkind = None, None
        best_sc = 1e9
        for m in mechs:
            if m["_d"] > 210:
                continue
            sc = m.get("hp", 1400) + m["_d"] * 3
            if not self.los(x, y, m["pos"][0], m["pos"][1]):
                sc += 600
            if sc < best_sc:
                best_sc, tm, tkind = sc, m, "mech"
        close_mech = bool(mechs) and mechs[0]["_d"] < 160
        if tm is None and not close_mech and (me["in_zone"] or t < zone["shrink_start"]):
            for n in npcs:
                d = math.hypot(n["pos"][0] - x, n["pos"][1] - y)
                if d < 70 and abs(n["pos"][2] - z) < 8 and d < best_sc:
                    best_sc, tm, tkind = d, n, "npc"
                    tm["_d"] = d
            if tm is None:
                for c in obs["crates"]:
                    d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                    if d < 50 and abs(c["pos"][2] - z) < 8 and d < best_sc and self.los(x, y, c["pos"][0], c["pos"][1]):
                        best_sc, tm, tkind = d, {"pos": c["pos"], "vel": [0, 0, 0], "_d": d, "aim": [0, 0]}, "crate"

        # fight unless badly hurt and out-gunned
        threats = [m for m in mechs if m["_d"] < 160]
        flee = False
        if threats:
            ehp = min(m.get("hp", 1400) for m in threats)
            flee = hpf < 0.28 and ehp > me["hp"] * 0.8
        engage = bool(tm is not None and tkind == "mech" and not flee)
        danger = bool(threats)
        wk = "lightning" if "lightning" in kinds else ("laser" if "laser" in kinds else ("mortar" if "mortar" in kinds else "gun"))
        want = {"lightning": 24, "laser": 55, "mortar": 55, "gun": 42}[wk]
        if tm is not None and not flee:
            d = tm["_d"]
            rad = max(-1.0, min(1.0, (d - want) / want))
            if tkind != "mech":
                want_n = 22 if wk == "lightning" else 35
                rad = max(-1.0, min(1.0, (d - want_n) / want_n))
            vx += (tm["pos"][0] - x) / max(d, 1) * rad * 1.4
            vy += (tm["pos"][1] - y) / max(d, 1) * rad * 1.4
            if tkind == "mech" and d < 150:
                ex, ey = x - tm["pos"][0], y - tm["pos"][1]
                en = math.hypot(ex, ey) or 1
                ph = (tick // 11) * 2654435761 % 7
                sgn = 1 if ph < 3 or (ph == 3 and tick % 2) else -1
                vx += -ey / en * sgn * 1.0
                vy += ex / en * sgn * 1.0
            for m in mechs:  # keep clear of the others
                if m is not tm and m["_d"] < 30:
                    vx += (x - m["pos"][0]) / max(m["_d"], 1) * 0.6
                    vy += (y - m["pos"][1]) / max(m["_d"], 1) * 0.6
        elif flee:
            for m in threats:
                d = m["_d"]
                w = (160 - d) / 160 * 1.8
                vx += (x - m["pos"][0]) / max(d, 1) * w
                vy += (y - m["pos"][1]) / max(d, 1) * w
        if tkind != "mech" or tm is None:
            for n in npcs:
                if n is tm:
                    continue
                d = math.hypot(n["pos"][0] - x, n["pos"][1] - y)
                if d < 40:
                    vx += (x - n["pos"][0]) / max(d, 1) * 0.4
                    vy += (y - n["pos"][1]) / max(d, 1) * 0.4
        if tm is None:
            for s in obs["sounds"]:
                if s["kind"] in ("gun", "laser", "mortar", "explosion", "lightning") and s["dist"] != "far":
                    vx += math.cos(s["bearing"]) * 0.25  # investigate: fights are where the points are
                    vy += math.sin(s["bearing"]) * 0.25

        # --- dodge: bullets and charging lasers
        dodge = False
        for p in obs["projectiles"]:
            px, py = p["pos"][0] - x, p["pos"][1] - y
            sp = math.hypot(p["vel"][0], p["vel"][1]) or 1
            ux, uy = p["vel"][0] / sp, p["vel"][1] / sp
            along = -(px * ux + py * uy)
            cr = px * uy - py * ux
            if 0 < along < 70 and abs(cr) < 9:
                side = 1 if cr > 0 else -1
                vx += -uy * side * 1.5
                vy += ux * side * 1.5
        for m in mechs:
            if m.get("charging") and m["_d"] < 290:
                bx, by = x - m["pos"][0], y - m["pos"][1]
                bd = math.hypot(bx, by) or 1
                off = angdiff(m["aim"][0], math.atan2(by, bx))
                if off < 0.12 + 5.0 / bd:
                    cr = math.cos(m["aim"][0]) * by - math.sin(m["aim"][0]) * bx
                    side = 1 if cr > 0 else -1
                    px_, py_ = math.sin(m["aim"][0]) * side, -math.cos(m["aim"][0]) * side
                    if self.clear_dist(x, y, px_, py_, 9) < 6:
                        px_, py_ = -px_, -py_
                    vx += px_ * 4.0
                    vy += py_ * 4.0
                    dodge = True

        # --- loot: better weapon (lightning > laser > mortar), cubes, supply drops
        RK = {"lightning": 3, "laser": 2, "mortar": 1}
        cur = me["weapons"][1]
        cur_rank = RK.get(cur["kind"], 0) if cur else 0
        near_enemy = bool(mechs) and mechs[0]["_d"] < 70
        if not near_enemy and not dodge:
            goal, best = None, 1e9
            for p in obs["pickups"]:
                rk = RK.get(p["kind"], 0)
                if rk > cur_rank and p["kind"] not in kinds and abs(p["pos"][2] - z) < 6:
                    d = math.hypot(p["pos"][0] - x, p["pos"][1] - y) - rk * 8
                    if d < best:
                        best, goal = d, p
            for c in obs["cubes"]:
                d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if d < best and d < 45 and abs(c["pos"][2] - z) < 6:
                    best, goal = d, c
            if goal is not None and best < 80:
                gx, gy = goal["pos"][0] - x, goal["pos"][1] - y
                n = math.hypot(gx, gy) or 1
                vx += gx / n * 1.6
                vy += gy / n * 1.6
            for p in obs["pickups"]:
                rk = RK.get(p["kind"], 0)
                if rk > cur_rank and p["kind"] not in kinds and math.hypot(p["pos"][0] - x, p["pos"][1] - y) < self.reach - 0.5 \
                        and abs(p["pos"][2] - z) < 4:
                    act["take"] = p["id"]
                    if cur is not None and not (cur["kind"] == "laser" and cur.get("charge", 0) > 0):
                        act["drop"] = 1
                    break
            if not threats:
                for dr in obs["drops"]:
                    if dr["lands_in"] < 6:
                        dx, dy = dr["pos"][0] - x, dr["pos"][1] - y
                        d = math.hypot(dx, dy)
                        if 3 < d < 120:
                            vx += dx / d
                            vy += dy / d

        # --- choose direction: walkable, in zone, hidden from threats
        n = math.hypot(vx, vy)
        if n < 1e-3:
            vx, vy = self.last_move
            n = 1.0
        vx, vy = vx / n, vy / n
        base = math.atan2(vy, vx)
        use_cover = bool(tp) and flee
        exp_now = sum(1 for (tx, ty) in tp if self.los(tx, ty, x, y)) if use_cover else 0
        best_s, best_a = -1e9, base
        for i in range(-6, 7):
            a = base + i * math.pi / 8
            ux, uy = math.cos(a), math.sin(a)
            cd = self.clear_dist(x, y, ux, uy)
            s = cd - abs(i) * 1.2
            if cd < 18:
                s -= 10
            s += 2.0 * (ux * self.last_move[0] + uy * self.last_move[1])
            if math.hypot(x + ux * 25 - zc[0], y + uy * 25 - zc[1]) > zr:
                s -= 8
            if use_cover and not dodge:
                cx, cy = x + ux * 14, y + uy * 14
                s -= 7 * sum(1 for (tx, ty) in tp if self.los(tx, ty, cx, cy))
            if s > best_s:
                best_s, best_a = s, a
        mx, my = math.cos(best_a), math.sin(best_a)
        self.last_move = (mx, my)

        stuck = math.hypot(me["vel"][0], me["vel"][1]) < 1.5 and tick > 20 and not dodge
        out = not me["in_zone"]
        recent1 = self.dmg is not None and t - self.dmg[1] < 1.5
        hide = (not mechs and not recent1 and tm is None and hpf < 0.55 and not out and dz < zr * 0.9
                and not (t > zone["shrink_start"] - 8 and dz > tgt_r * 1.5))
        calm = (not danger) and not out and dz < tgt_r * 1.3 and not obs["sounds"] and not tp and tm is None
        spd = 0.0 if hide else (0.4 if calm else 1.0)
        act["move"] = [mx * spd, my * spd]
        act["boost"] = bool((out or dodge or (flee and mechs and mechs[0]["_d"] < 90))
                            and me["energy"] > 20 and not stuck and not hide)
        if (dodge and me["grounded"] and me["energy"] < 20) or (stuck and me["grounded"] and tick % 6 == 0):
            act["jump"] = True

        # --- aim & fire
        yaw, pitch = base, 0.0
        fire = [False, False]
        if tm is not None and tm["_d"] < 260:
            tx, ty, tz = tm["pos"]
            vxe, vye = tm["vel"][0], tm["vel"][1]
            g0 = me["weapons"][0]
            spd_b = g0["stats"]["speed"] if g0 and "stats" in g0 else 250
            tt = tm["_d"] / spd_b
            for _ in range(2):
                tt = math.hypot(tx + vxe * tt - x, ty + vye * tt - y) / spd_b
            px, py = tx + vxe * tt, ty + vye * tt
            yaw = math.atan2(py - y, px - x)
            dd = math.hypot(px - x, py - y)
            tz_c = tz + (4.5 if tkind == "mech" else 1.5)
            pitch = math.atan2(tz_c - (z + 7), dd)
            lo = min(dd, 200.0)
            los = self.clear_dist(x, y, math.cos(yaw), math.sin(yaw), lo) >= lo - CELL
            diff = angdiff(yaw, me["aim"][0])
            for i, w in enumerate(me["weapons"]):
                if not w:
                    continue
                k = w["kind"]
                if k == "gun":
                    fire[i] = dd < 190 and diff < max(0.04, min(0.25, 2.2 / max(dd, 1))) \
                        and w.get("ammo", 1) > 0 and w.get("reload", 0) == 0 and los
                elif k == "lightning":
                    fire[i] = dd < 42 and me["energy"] > 6 and los
                elif k == "laser" and tkind == "mech":
                    fire[i] = dd < 220 and diff < max(0.03, min(0.2, 2.0 / max(dd, 1))) and los and not dodge
                elif k == "mortar" and tkind == "mech":
                    gg = self.cfg["weapons"]["mortar"]["gravity"]
                    v = self.cfg["weapons"]["mortar"]["speed"]
                    if 25 < dd < 200:
                        tt2 = dd / (v * 0.8)
                        for _ in range(2):
                            px2, py2 = tx + vxe * tt2, ty + vye * tt2
                            dd2 = math.hypot(px2 - x, py2 - y)
                            r = min(dd2, v * v / gg * 0.98)
                            pitch = 0.5 * math.asin(max(-1, min(1, gg * r / (v * v))))
                            tt2 = dd2 / (v * math.cos(pitch))
                        yaw = math.atan2(py2 - y, px2 - x)
                        fire[i] = diff < 0.2
        else:
            if self.dmg is not None and t - self.dmg[1] < 1.5:
                yaw = self.dmg[0]  # turn to face whoever is hitting us
            g0 = me["weapons"][0]
            if g0 and g0.get("ammo", 12) < 8 and g0.get("reload", 0) == 0:
                act["reload"] = True
        act["aim"] = [yaw, pitch]
        act["fire"] = fire
        return act


mf.run(Bot)
