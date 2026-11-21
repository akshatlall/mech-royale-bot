"""Survivor bot: placement > kills. Stay in the zone, keep away from enemies, use walls, shoot when safe."""
import json, math, os
import mechfight as mf

CELL = 3.0


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


class Bot:
    name = "survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.reach = self.cfg["loot"]["pickup_radius"]
        self.solid = set()
        try:
            d = hello["map"]["dir"]
            w = json.load(open(os.path.join(d, "v2/world.json")))
            for p in w["prisms"]:
                if p["base"] > 3 and p["kind"] == "bridge":
                    continue  # walk under bridges
                r = p["ring"]
                xs, ys = r[0::2], r[1::2]
                for cx in range(int(min(xs) // CELL), int(max(xs) // CELL) + 1):
                    for cy in range(int(min(ys) // CELL), int(max(ys) // CELL) + 1):
                        px, py = (cx + .5) * CELL, (cy + .5) * CELL
                        if pip(px, py, r) and not any(pip(px, py, h) for h in (p.get("holes") or [])):
                            self.solid.add((cx, cy))
        except Exception as e:
            import sys
            print("map load failed", e, file=sys.stderr)
        self.last_move = (1.0, 0.0)

    def blocked(self, x, y):
        return (int(x // CELL), int(y // CELL)) in self.solid

    def clear_dist(self, x, y, dx, dy, dist=18.0):
        s = 0.0
        while s < dist:
            s += 3.0
            if s > dist:
                break
            if self.blocked(x + dx * s, y + dy * s):
                return s - 3.0
        return dist

    def act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        act = {"tick": obs["tick"]}
        if obs["offer"]:
            ch = [c["id"] for c in obs["offer"]["choices"]]
            order = ["plating", "armor", "nanites", "servos", "gun_damage", "laser_damage", "gun_mag",
                     "capacitor", "jets", "gyros", "gun_rate", "lightning_efficiency", "reactor"]
            pick = 0
            bestr = 99
            for i, c in enumerate(ch):
                r = order.index(c) if c in order else 50
                if r < bestr:
                    bestr, pick = r, i
            act["pick"] = pick

        zone = obs["zone"]
        t = obs["t"]
        # target circle: next circle once shrink is near/underway, else current
        if t >= zone["shrink_start"] - 15:
            zc, zr = zone["next_center"], zone["next_radius"]
        else:
            zc, zr = zone["center"], zone["radius"]
        dzx, dzy = zc[0] - x, zc[1] - y
        dz = math.hypot(dzx, dzy) or 1e-6
        tgt_r = max(zr * 0.45, 8.0)

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs:
            m["_d"] = math.dist(m["pos"][:2], (x, y))
        mechs.sort(key=lambda m: m["_d"])

        # --- desired direction: sum of attractions/repulsions
        vx = vy = 0.0
        if dz > tgt_r:
            k = min(1.0, (dz - tgt_r) / 40.0 + 0.3)
            vx += dzx / dz * k
            vy += dzy / dz * k
        danger = False
        threats = [m for m in mechs if m["_d"] < 130]
        hpf = me["hp"] / me["max_hp"]
        engage = False
        if len(threats) == 1:
            e = threats[0]
            ehp = e.get("hp", 1400)
            engage = hpf > 0.4 and (ehp < me["hp"] * 1.3 or hpf > 0.8)
        self.engage = engage
        if engage:
            danger = True
            e = threats[0]
            d = e["_d"]
            kinds = [w["kind"] for w in me["weapons"] if w]
            want = 28 if "lightning" in kinds else (60 if "laser" in kinds else 48)
            rad = max(-1.0, min(1.0, (d - want) / want))
            vx += (e["pos"][0] - x) / max(d, 1) * rad * 1.3
            vy += (e["pos"][1] - y) / max(d, 1) * rad * 1.3
        else:
            for m in threats:
                d = m["_d"]
                danger = True
                w = (130 - d) / 130 * 1.6
                vx += (x - m["pos"][0]) / max(d, 1) * w
                vy += (y - m["pos"][1]) / max(d, 1) * w
        for n in npcs:
            d = math.dist(n["pos"][:2], (x, y))
            if d < 50:
                vx += (x - n["pos"][0]) / max(d, 1) * 0.5
                vy += (y - n["pos"][1]) / max(d, 1) * 0.5
        # strafe around the nearest threat so shots miss
        if mechs and mechs[0]["_d"] < 110:
            m0 = mechs[0]
            ex, ey = x - m0["pos"][0], y - m0["pos"][1]
            en = math.hypot(ex, ey) or 1
            sgn = 1 if (obs["tick"] // 18) % 2 == 0 else -1
            vx += -ey / en * sgn * 0.9
            vy += ex / en * sgn * 0.9
        # incoming sounds: step away from heard fire
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "laser", "mortar", "explosion", "lightning") and s["dist"] != "far":
                b = s["bearing"]
                vx -= math.cos(b) * 0.4
                vy -= math.sin(b) * 0.4
        # incoming projectiles: sidestep
        for p in obs["projectiles"]:
            px, py = p["pos"][0] - x, p["pos"][1] - y
            sp = math.hypot(p["vel"][0], p["vel"][1]) or 1
            ux, uy = p["vel"][0] / sp, p["vel"][1] / sp
            along = -(px * ux + py * uy)
            if 0 < along < 60 and abs(px * uy - py * ux) < 8:
                side = 1 if (px * uy - py * ux) > 0 else -1
                vx += -uy * side * 1.5
                vy += ux * side * 1.5
        # nearby wanted loot when calm
        if not danger:
            want = me["weapons"][1] is None
            goal = None
            best = 1e9
            for p in obs["pickups"]:
                if p["kind"] != "gun" and (want or p["kind"] in ("laser",)):
                    d = math.dist(p["pos"][:2], (x, y))
                    if d < best and abs(p["pos"][2] - z) < 6:
                        best, goal = d, p
            for c in obs["cubes"]:
                d = math.dist(c["pos"][:2], (x, y))
                if d < best and d < 40 and abs(c["pos"][2] - z) < 6:
                    best, goal = d, c
            if goal and best < 60:
                gx, gy = goal["pos"][0] - x, goal["pos"][1] - y
                n = math.hypot(gx, gy) or 1
                vx += gx / n * 1.2
                vy += gy / n * 1.2
            for p in obs["pickups"]:
                if p["kind"] != "gun" and math.dist(p["pos"][:2], (x, y)) < self.reach - 0.5 and abs(p["pos"][2] - z) < 4:
                    if want and not any(w and w["kind"] == p["kind"] for w in me["weapons"] if w):
                        act["take"] = p["id"]
                    break
        # supply drop: go if on ground and nobody likely near
        for dr in obs["drops"]:
            if dr["lands_in"] < 6 and not danger:
                dx, dy = dr["pos"][0] - x, dr["pos"][1] - y
                d = math.hypot(dx, dy)
                if d < 120 and d > 3:
                    vx += dx / d * 1.0
                    vy += dy / d * 1.0

        # --- pick a walkable direction near desired
        n = math.hypot(vx, vy)
        if n < 1e-3:
            vx, vy = self.last_move
            n = 1.0
        vx, vy = vx / n, vy / n
        base = math.atan2(vy, vx)
        best_s, best_a = -1e9, base
        for i in range(-6, 7):
            a = base + i * math.pi / 8
            ux, uy = math.cos(a), math.sin(a)
            cd = self.clear_dist(x, y, ux, uy)
            s = cd - abs(i) * 1.2
            if cd < 18:
                s -= 10
            # prefer continuing the previous heading a bit (avoid jitter)
            s += 2.0 * (ux * self.last_move[0] + uy * self.last_move[1])
            # stay in zone
            nx, ny = x + ux * 25, y + uy * 25
            if math.hypot(nx - zc[0], ny - zc[1]) > zr:
                s -= 8
            if s > best_s:
                best_s, best_a = s, a
        mx, my = math.cos(best_a), math.sin(best_a)
        self.last_move = (mx, my)
        stuck = math.hypot(me["vel"][0], me["vel"][1]) < 1.5 and obs["tick"] > 20
        calm = (not danger) and me["in_zone"] and dz < tgt_r * 1.3 and not obs["sounds"]
        spd = 0.4 if calm else 1.0  # < step_speed: silent
        act["move"] = [mx * spd, my * spd]
        out = not me["in_zone"]
        act["boost"] = bool((out or (danger and not engage and mechs and mechs[0]["_d"] < 70)) and me["energy"] > 25 and not stuck)
        if stuck and me["grounded"] and obs["tick"] % 6 == 0:
            act["jump"] = True

        # --- aim & fire
        yaw = base
        pitch = 0.0
        fire = [False, False]
        tm = None
        for m in mechs:
            tm = m
            break
        if tm is None and npcs and any(math.dist(n["pos"][:2], (x, y)) < 35 for n in npcs):
            pass
        if tm is not None and tm["_d"] < 210:
            tx, ty, tz = tm["pos"]
            vxe, vye = tm["vel"][0], tm["vel"][1]
            spd = me["weapons"][0]["stats"]["speed"] if me["weapons"][0] else 250
            tt = tm["_d"] / spd
            for _ in range(2):
                px, py = tx + vxe * tt, ty + vye * tt
                tt = math.hypot(px - x, py - y) / spd
            px, py = tx + vxe * tt, ty + vye * tt
            yaw = math.atan2(py - y, px - x)
            dd = math.hypot(px - x, py - y)
            pitch = math.atan2((tz + 4.5) - (z + 7), dd)
            los = self.clear_dist(x, y, math.cos(yaw), math.sin(yaw), min(dd, 200.0)) >= min(dd, 200.0) - 3
            diff = abs((yaw - me["aim"][0] + math.pi) % (2 * math.pi) - math.pi)
            for i, w in enumerate(me["weapons"]):
                if not w:
                    continue
                k = w["kind"]
                if k == "gun":
                    fire[i] = dd < 190 and diff < max(0.04, min(0.25, 2.2 / max(dd, 1))) and w.get("ammo", 1) > 0 and los
                elif k == "lightning":
                    fire[i] = dd < 42 and me["energy"] > 8 and los
                elif k == "laser":
                    fire[i] = dd < 200 and diff < max(0.03, min(0.2, 2.0 / max(dd, 1))) and los
                elif k == "mortar":
                    g = self.cfg["weapons"]["mortar"]["gravity"]
                    v = self.cfg["weapons"]["mortar"]["speed"]
                    r = min(dd, v * v / g * 0.98)
                    if 25 < dd < 200:
                        pitch = 0.5 * math.asin(max(-1, min(1, g * r / (v * v))))
                        fire[i] = diff < 0.2
        elif me["weapons"][0] and me["weapons"][0].get("ammo", 12) < 6:
            act["reload"] = True
        act["aim"] = [yaw, pitch]
        act["fire"] = fire
        return act


mf.run(Bot)
