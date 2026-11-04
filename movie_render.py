"""Step 2: render one shard of the movie with Blender (run headless).

blender -b -P scripts/blender_render.py -- --timeline out/timeline.json \
    --story story/finance_story.json --assets assets --out frames \
    --shard 0 --shards 6 [--sample-only]

Every shard renders a contiguous slice of the global frame list, so all
shards finish at about the same time.
Models are found BY NAME anywhere under --assets (.glb/.gltf preferred, .fbx fallback).
"""
import argparse
import json
import math
import os
import random
import re
import sys
import time

import bpy
from mathutils import Vector

# If characters walk backwards / sideways in the sample images, change this (degrees).
MODEL_FORWARD_OFFSET = 0.0

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--timeline", required=True)
ap.add_argument("--story", required=True)
ap.add_argument("--assets", default="assets")
ap.add_argument("--out", default="frames")
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--shards", type=int, default=1)
ap.add_argument("--sample-only", action="store_true")
args = ap.parse_args(argv)

TL = json.load(open(args.timeline, encoding="utf-8"))
STORY = json.load(open(args.story, encoding="utf-8"))
SHOTS = {s["id"]: s for s in STORY["shots"]}
FPS = TL["fps"]
os.makedirs(args.out, exist_ok=True)

# ---------------------------------------------------------------- asset index
PREF = {".glb": 0, ".gltf": 1, ".fbx": 2}
INDEX = {}
for dp, _dn, fns in os.walk(args.assets):
    for fn in fns:
        stem, ext = os.path.splitext(fn)
        ext = ext.lower()
        if ext not in PREF:
            continue
        k = stem.lower()
        old = INDEX.get(k)
        if old is None or PREF[ext] < PREF[os.path.splitext(old)[1].lower()]:
            INDEX[k] = os.path.join(dp, fn)
if not INDEX:
    raise SystemExit(f"No 3D models found under '{args.assets}'. Upload your Quaternius files first.")
MISSING = set()


def norm_anim(n):
    return re.sub(r"\.\d+$", "", n.split("|")[-1]).lower()


def link(obj):
    bpy.context.scene.collection.objects.link(obj)


def load_model(name):
    path = INDEX.get(name.lower())
    if not path:
        MISSING.add(name)
        return None
    before_o = set(bpy.data.objects.keys())
    before_a = set(bpy.data.actions.keys())
    if path.lower().endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o.name not in before_o]
    acts = [a for a in bpy.data.actions if a.name not in before_a]
    wrap = bpy.data.objects.new("W_" + name, None)
    link(wrap)
    newset = set(new)
    for o in new:
        if o.parent is None or o.parent not in newset:
            o.parent = wrap
    # make sure imported objects are in the scene collection we render
    for o in new:
        if o.name not in bpy.context.scene.collection.all_objects:
            link(o)
    return wrap, new, acts


def apply_anim(new, acts, anim):
    arms = [o for o in new if o.type == "ARMATURE"]
    if not arms or not acts:
        return
    pick = next((a for a in acts if norm_anim(a.name) == anim.lower()), None)
    if pick is None:
        pick = next((a for a in acts if norm_anim(a.name) == "idle"), acts[0])
        print(f"  [warn] animation '{anim}' not found, using '{pick.name}'")
    for arm in arms:
        ad = arm.animation_data_create()
        for t in list(ad.nla_tracks):
            ad.nla_tracks.remove(t)
        ad.action = pick
    for fc in pick.fcurves:
        if not any(m.type == "CYCLES" for m in fc.modifiers):
            fc.modifiers.new("CYCLES")


def clone_hierarchy(wrap, name):
    w2 = bpy.data.objects.new(name, None)
    link(w2)

    def rec(src, parent):
        for ch in src.children:
            c = ch.copy()
            link(c)
            c.parent = parent
            c.matrix_parent_inverse = ch.matrix_parent_inverse.copy()
            rec(ch, c)
    rec(wrap, w2)
    return w2


# ---------------------------------------------------------------- environments
ENVS = {
    "village": dict(ground=(0.30, 0.55, 0.20), sky=(0.55, 0.75, 0.95), sun=4.0, sun_col=(1, 0.95, 0.85),
                    trees=["CommonTree_1", "CommonTree_2", "CommonTree_3", "CommonTree_4"], n_trees=9,
                    extras=["Bush_Common", "Bush_Common_Flowers", "Flower_3_Group", "Flower_4_Group", "Grass_Common_Tall"], n_extras=14),
    "forest": dict(ground=(0.20, 0.42, 0.16), sky=(0.50, 0.70, 0.85), sun=3.2, sun_col=(1, 0.95, 0.8),
                   trees=["Pine_1", "Pine_2", "Pine_3", "CommonTree_2", "CommonTree_3"], n_trees=16,
                   extras=["Bush_Common", "Fern_1", "Mushroom_Common", "Grass_Common_Tall"], n_extras=12),
    "storm": dict(ground=(0.15, 0.25, 0.15), sky=(0.22, 0.25, 0.32), sun=1.1, sun_col=(0.65, 0.75, 1.0),
                  trees=["CommonTree_1", "DeadTree_1", "CommonTree_3"], n_trees=8,
                  extras=["Bush_Common", "Grass_Wispy_Tall"], n_extras=8),
    "city": dict(ground=(0.22, 0.22, 0.24), sky=(0.60, 0.72, 0.88), sun=4.0, sun_col=(1, 0.97, 0.9),
                 trees=[], n_trees=0, extras=[], n_extras=0,
                 buildings=["Building_Large_2", "Building_Medium_2_001", "Building_Small_1"],
                 cars=["NormalCar1", "SportsCar", "Taxi", "SUV"]),
}


def make_material(color, rough=1.0):
    m = bpy.data.materials.new("mat")
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Roughness"].default_value = rough
    return m


def scatter(names, count, rng, rmin, rmax, ymin, scale=(0.9, 1.3)):
    cache = {}
    placed = 0
    tries = 0
    while placed < count and tries < count * 20 and names:
        tries += 1
        ang = rng.uniform(0, math.tau)
        r = rng.uniform(rmin, rmax)
        x, y = r * math.cos(ang), r * math.sin(ang)
        if y < ymin:
            continue
        name = rng.choice(names)
        if name.lower() not in INDEX:
            MISSING.add(name)
            names = [n for n in names if n != name]
            continue
        if name not in cache:
            res = load_model(name)
            if res is None:
                continue
            cache[name] = res[0]
            w = res[0]
        else:
            w = clone_hierarchy(cache[name], f"C_{name}_{placed}")
        w.location = (x, y, 0)
        w.rotation_euler.z = rng.uniform(0, math.tau)
        s = rng.uniform(*scale)
        w.scale = (s, s, s)
        placed += 1


def build_env(env_name, seed):
    e = ENVS.get(env_name, ENVS["village"])
    sc = bpy.context.scene
    w = bpy.data.worlds.new("World")
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (*e["sky"], 1)
    bg.inputs[1].default_value = 1.0
    sc.world = w

    bpy.ops.mesh.primitive_plane_add(size=120, location=(0, 0, 0))
    ground = bpy.context.active_object
    ground.data.materials.append(make_material(e["ground"]))

    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = e["sun"]
    sun_data.color = e["sun_col"]
    sun_data.angle = math.radians(8)
    sun = bpy.data.objects.new("Sun", sun_data)
    link(sun)
    sun.rotation_euler = (math.radians(50), math.radians(5), math.radians(35))

    rng = random.Random(seed * 7919)
    if env_name == "city":
        x = -24
        i = 0
        while x < 24:
            res = load_model(rng.choice(e["buildings"]))
            if res:
                res[0].location = (x, 12, 0)
                res[0].rotation_euler.z = math.radians(180)
            x += 9
            i += 1
        for k in range(3):
            res = load_model(rng.choice(e["cars"]))
            if res:
                res[0].location = (-8 + k * 8, 5, 0)
                res[0].rotation_euler.z = math.radians(90)
    else:
        scatter(e["trees"], e["n_trees"], rng, 7, 22, -1.5, scale=(0.9, 1.4))
        scatter(e["extras"], e["n_extras"], rng, 4.5, 14, -2.0, scale=(0.9, 1.4))


# ---------------------------------------------------------------- render setup
def setup_render():
    sc = bpy.context.scene
    r = sc.render
    r.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = TL["samples"]
    sc.cycles.use_adaptive_sampling = True
    sc.cycles.use_denoising = True
    try:
        sc.cycles.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass
    sc.cycles.max_bounces = 4
    sc.cycles.diffuse_bounces = 2
    sc.cycles.glossy_bounces = 2
    sc.cycles.transmission_bounces = 2
    sc.cycles.caustics_reflective = False
    sc.cycles.caustics_refractive = False
    r.resolution_x = TL["width"]
    r.resolution_y = TL["height"]
    r.resolution_percentage = 100
    r.fps = 24
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.compression = 5
    r.threads_mode = "AUTO"
    return sc


def ease(p):
    return p * p * (3 - 2 * p)


def lerp(a, b, p):
    return a + (b - a) * p


class Actor:
    def __init__(self, spec, is_prop=False):
        self.spec = spec
        res = load_model(spec["model"])
        self.ok = res is not None
        if not self.ok:
            return
        self.wrap, new, acts = res
        if not is_prop:
            apply_anim(new, acts, spec.get("anim", "Idle"))
        self.pos = spec.get("pos", [0, 0])
        self.move = spec.get("move", [0, 0])
        if "heading" in spec:
            self.heading = spec["heading"]
        elif abs(self.move[0]) + abs(self.move[1]) > 0.01:
            self.heading = math.degrees(math.atan2(self.move[0], -self.move[1]))
        else:
            self.heading = 0
        self.scale = spec.get("scale", 1.0)
        self.grow = spec.get("grow")

    def update(self, p):
        if not self.ok:
            return
        q = p
        self.wrap.location = (self.pos[0] + self.move[0] * q, self.pos[1] + self.move[1] * q, 0)
        self.wrap.rotation_euler.z = math.radians(self.heading + MODEL_FORWARD_OFFSET)
        s = self.scale
        if self.grow:
            s = lerp(self.grow[0], self.grow[1], ease(p))
        self.wrap.scale = (s, s, s)


def build_shot(shot):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = setup_render()
    build_env(shot.get("env", "village"), shot["id"])
    actors = [Actor(c) for c in shot.get("characters", [])]
    props = [Actor(c, is_prop=True) for c in shot.get("props", [])]

    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 35
    cam = bpy.data.objects.new("Cam", cam_data)
    link(cam)
    sc.camera = cam
    target = bpy.data.objects.new("Target", None)
    link(target)
    con = cam.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return sc, cam, target, actors, props


def pose_frame(sc, cam, target, actors, props, shot, t, n):
    p = t / max(1, n - 1)
    for a in actors + props:
        a.update(p)
    c = shot.get("camera", {})
    tgt = Vector(c.get("target", [0, 0, 1.2]))
    if c.get("follow"):
        movers = [a for a in actors if a.ok]
        if movers:
            tgt = Vector(movers[0].wrap.location) + Vector((0, 0, 1.2))
    dist = lerp(c.get("dist", 7.0), c.get("dist_end", c.get("dist", 7.0)), ease(p))
    ang = math.radians(c.get("angle", 0) + c.get("sweep", 0) * ease(p))
    h = c.get("height", 2.0)
    cam.location = tgt + Vector((dist * math.sin(ang), -dist * math.cos(ang), h))
    target.location = tgt
    fr = 1 + t * 24.0 / FPS
    sc.frame_set(int(fr), subframe=fr - int(fr))


def render_to(sc, path):
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main():
    t_start = time.time()
    total = TL["total_frames"]
    done = 0
    if args.sample_only:
        for k, ts in enumerate(TL["shots"]):
            if k % args.shards != args.shard:
                continue
            shot = SHOTS[ts["id"]]
            n = ts["end_frame"] - ts["start_frame"]
            sc, cam, target, actors, props = build_shot(shot)
            pose_frame(sc, cam, target, actors, props, shot, n // 2, n)
            render_to(sc, os.path.abspath(os.path.join(args.out, f"shot_{ts['id']:02d}.png")))
            done += 1
            print(f"[sample] shot {ts['id']} done")
    else:
        a = total * args.shard // args.shards
        b = total * (args.shard + 1) // args.shards
        print(f"Shard {args.shard}/{args.shards}: frames {a}..{b - 1}")
        for ts in TL["shots"]:
            lo = max(a, ts["start_frame"])
            hi = min(b, ts["end_frame"])
            if lo >= hi:
                continue
            shot = SHOTS[ts["id"]]
            n = ts["end_frame"] - ts["start_frame"]
            sc, cam, target, actors, props = build_shot(shot)
            for g in range(lo, hi):
                out = os.path.abspath(os.path.join(args.out, f"f_{g:06d}.png"))
                if os.path.exists(out):
                    continue
                pose_frame(sc, cam, target, actors, props, shot, g - ts["start_frame"], n)
                render_to(sc, out)
                done += 1
                if done % 10 == 0:
                    el = time.time() - t_start
                    print(f"  {done}/{b - a} frames, {el / done:.1f}s per frame", flush=True)
    print(f"Finished {done} frames in {(time.time() - t_start) / 60:.1f} min")
    if MISSING:
        print("MISSING MODELS (not found in assets):", ", ".join(sorted(MISSING)))


main()
