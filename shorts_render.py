"""shorts_render.py - renders one shard of the vertical animal Short with Blender (headless).

blender -b -P shorts_render.py -- --timeline timeline.json --story scene_plan_timed.json \
    --assets . --out out --shard 0 --shards 6 [--sample-only]

* vertical 9:16 picture, natural looks only (meadow, forest, sunset, riverbank, storm, night)
* every animal is measured and scaled to a realistic height, so sizes always match
* each animal follows its beats (walk/run at natural speed, then idle), turns smoothly to face
  who it talks to, and plays the clip of the right kind of animal
* black / white wolves (cast tints), boulders to stand on, calmer light, lens 30
* the camera is DIRECTED per shot (framing, angle, movement, focus), all calculated by shorts_core.py
* --sample-only renders 3 pictures per shot (start, middle, end) for a quick check
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

_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
for _p in (_here, os.getcwd()):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import shorts_core as core  # noqa: E402

# If animals walk backwards or sideways in the preview pictures, change this (degrees), or set the
# environment variable MODEL_FORWARD_OFFSET (the preview workflow has a box for it).
MODEL_FORWARD_OFFSET = float(os.environ.get("MODEL_FORWARD_OFFSET", "0") or 0)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
ap = argparse.ArgumentParser(allow_abbrev=False)
ap.add_argument("--timeline", required=True)
ap.add_argument("--story", required=True)
ap.add_argument("--assets", default="assets")
ap.add_argument("--out", default="frames")
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--shards", type=int, default=1)
ap.add_argument("--sample-only", action="store_true")
try:
    args, _unused = ap.parse_known_args(argv)
except SystemExit:
    print("shorts_render.py did not receive --timeline and --story.")
    print("The command line Blender received was:", sys.argv)
    print("The workflow must call: blender -b -P shorts_render.py -- --timeline timeline.json "
          "--story scene_plan_timed.json --assets . --out out --shard N --shards 6")
    raise

TL = json.load(open(args.timeline, encoding="utf-8"))
STORY = json.load(open(args.story, encoding="utf-8"))
SHOTS = {s["id"]: s for s in STORY["shots"]}
FPS = TL["fps"]
W, H = TL["width"], TL["height"]
HFOV, VFOV = core.fovs(W, H)
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
    raise SystemExit(f"No 3D models found under '{args.assets}'. Download the asset packs first.")
MISSING, ANIM_PROBLEMS, ANIM_USED, SCALE_REPORT = set(), set(), {}, {}


def norm_anim(n):
    """'Walk_AnimalArmature' -> 'walk', 'CharacterArmature|Walk' -> 'walk', 'Walk.001' -> 'walk'"""
    n = n.split("|")[-1]
    n = re.sub(r"\.\d+$", "", n)
    n = re.sub(r"_?(animal|character)?armature$", "", n, flags=re.I)
    return n.lower()


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
    for o in new:
        if o.name not in bpy.context.scene.collection.all_objects:
            link(o)
    return wrap, new, acts


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


def measure_height(objs):
    bpy.context.view_layer.update()
    zmin, zmax = 1e9, -1e9
    for o in objs:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            z = (o.matrix_world @ Vector(c)).z
            zmin, zmax = min(zmin, z), max(zmax, z)
    return (zmax - zmin) if zmax > zmin else None


def add_cycles(action):
    for fc in action.fcurves:
        if not any(m.type == "CYCLES" for m in fc.modifiers):
            fc.modifiers.new("CYCLES")



def apply_tint(objs, tint):
    """Multiply the colours of an animal's materials (black wolf = [0.15,0.14,0.16], white = [2.3,2.3,2.5])."""
    copies = {}
    for o in objs:
        if o.type != "MESH":
            continue
        for slot in o.material_slots:
            m = slot.material
            if m is None:
                continue
            if m.name not in copies:
                m2 = m.copy()
                copies[m.name] = m2
                if m2.use_nodes:
                    for node in m2.node_tree.nodes:
                        if node.type != "BSDF_PRINCIPLED":
                            continue
                        inp = node.inputs["Base Color"]
                        if inp.is_linked:
                            src = inp.links[0].from_socket
                            mix = m2.node_tree.nodes.new("ShaderNodeMixRGB")
                            mix.blend_type = "MULTIPLY"
                            mix.inputs["Fac"].default_value = 1.0
                            mix.inputs["Color2"].default_value = (tint[0], tint[1], tint[2], 1.0)
                            m2.node_tree.links.new(src, mix.inputs["Color1"])
                            m2.node_tree.links.new(mix.outputs["Color"], inp)
                        else:
                            c = inp.default_value
                            inp.default_value = (c[0] * tint[0], c[1] * tint[1], c[2] * tint[2], 1.0)
            slot.material = copies[m.name]


def make_rock(x, y, size, seed=1):
    """A lumpy boulder. Its top is at about 0.7 * size, so an animal can stand on it."""
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=size, location=(x, y, 0))
    r = bpy.context.active_object
    rng = random.Random(seed)
    for v in r.data.vertices:
        k = 1.0 + rng.uniform(-0.12, 0.12)
        v.co.x *= k
        v.co.y *= k
        v.co.z *= k
    r.scale = (1.0, 0.85, 0.7)
    for pl in r.data.polygons:
        pl.use_smooth = True
    r.data.materials.append(make_material((0.42, 0.40, 0.38), 0.95))
    return r

# ---------------------------------------------------------------- natural looks
def make_material(color, rough=1.0):
    m = bpy.data.materials.new("mat")
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Roughness"].default_value = rough
    return m


def ground_material(c0, c1):
    """Grass colour that varies in patches (noise between two greens)."""
    m = bpy.data.materials.new("ground")
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.95
    try:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 0.35
        noise.inputs["Detail"].default_value = 6.0
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = (*c0, 1)
        ramp.color_ramp.elements[1].color = (*c1, 1)
        nt.links.new(tc.outputs["Object"], noise.inputs["Vector"])
        nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    except Exception:
        bsdf.inputs["Base Color"].default_value = (*c0, 1)
    return m


def build_world(look):
    sc = bpy.context.scene
    w = bpy.data.worlds.new("World")
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    try:
        sky = nt.nodes.new("ShaderNodeTexSky")
        sky.sky_type = "NISHITA"
        sky.sun_elevation = math.radians(-6 if look["night"] else look["elev"])
        sky.sun_rotation = math.radians(look["az"])
        sky.sun_disc = False
        sky.dust_density = look["dust"]
        nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
    except Exception:
        bg.inputs["Color"].default_value = (0.12, 0.16, 0.30, 1) if look["night"] else (0.55, 0.72, 0.95, 1)
    bg.inputs["Strength"].default_value = look["world"]
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    sc.world = w


def scatter(names, count, rng, decor=False):
    cache, placed, tries = {}, 0, 0
    names = list(names)
    while placed < count and tries < count * 25 and names:
        tries += 1
        if decor:
            x, y = rng.uniform(-7, 7), rng.uniform(-3, 9)
        else:
            x, y = rng.uniform(-32, 32), rng.uniform(-1.5, 42)
            if -6.0 < x < 6.0 and -3.5 < y < 8.0:        # keep the action area free
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
        s = rng.uniform(0.85, 1.35) if not decor else rng.uniform(0.9, 1.2)
        w.scale = (s, s, s)
        placed += 1


def build_env(look_name):
    look = core.LOOKS.get(look_name, core.LOOKS["meadow_day"])
    sc = bpy.context.scene
    build_world(look)
    bpy.ops.mesh.primitive_plane_add(size=320, location=(0, 20, 0))
    ground = bpy.context.active_object
    ground.data.materials.append(ground_material(*look["ground"]))
    if look["water"]:
        bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 10.5, 0.05))
        water = bpy.context.active_object
        water.scale = (320, 4.0, 1)
        mat = make_material((0.08, 0.28, 0.42), 0.05)
        water.data.materials.append(mat)
    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = look["sun"]
    sun_data.color = look["sun_col"]
    sun_data.angle = math.radians(6)
    sun = bpy.data.objects.new("Sun", sun_data)
    link(sun)
    sun.rotation_euler = (math.radians(90 - look["elev"]), 0, math.radians(look["az"]))
    if look["night"]:                                    # warm firefly glow so the animals stay readable
        lamp = bpy.data.lights.new("Glow", "POINT")
        lamp.energy = 900
        lamp.color = (1.0, 0.8, 0.5)
        lamp.shadow_soft_size = 1.5
        lo = bpy.data.objects.new("Glow", lamp)
        link(lo)
        lo.location = (0.5, -1.5, 3.0)
    rng = random.Random(sum(ord(c) for c in look_name) * 7919)   # same place for every shot of this look
    scatter(look["trees"], look["n_trees"], rng)
    scatter(look["extras"], look["n_extras"], rng)
    scatter(look["decor"], look["n_decor"], rng, decor=True)
    for k in range(7):                                   # a few big boulders far from the action
        x = rng.choice([-1, 1]) * rng.uniform(8, 26)
        make_rock(x, rng.uniform(4, 36), rng.uniform(1.0, 2.6), seed=k + 11)


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
    r.resolution_x, r.resolution_y, r.resolution_percentage = W, H, 100
    r.fps = 24
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.compression = 5
    r.threads_mode = "AUTO"
    try:
        sc.view_settings.view_transform = "Filmic"
        sc.view_settings.look = "Medium High Contrast"
        sc.view_settings.exposure = -0.3
    except Exception:
        pass
    return sc


class Actor:
    def __init__(self, ch):
        self.id = ch["id"]
        self.model = ch["model"]
        res = load_model(self.model)
        self.ok = res is not None
        if not self.ok:
            return
        self.wrap, new, acts = res
        self.arms = [o for o in new if o.type == "ARMATURE"]
        self.actions = {}
        for a in acts:
            self.actions.setdefault(norm_anim(a.name), a)
        self.base_scale = 1.0
        self.z = float(ch.get("z") or 0.0)
        if ch.get("tint"):
            apply_tint(new, ch["tint"])
        h = measure_height(new)
        want = core.TARGET_HEIGHT.get(self.model, 1.5)
        if h and h > 0.01:
            self.base_scale = max(0.05, min(20.0, want / h))
            SCALE_REPORT[self.model] = f"{h:.2f} m -> {want:.2f} m (x{self.base_scale:.2f})"
        for arm in self.arms:
            ad = arm.animation_data_create()
            for t in list(ad.nla_tracks):
                ad.nla_tracks.remove(t)
        self.used = set()

    def pick(self, anim):
        a = self.actions.get(anim.lower())
        if a is None:
            a = self.actions.get("idle") or (next(iter(self.actions.values())) if self.actions else None)
            ANIM_PROBLEMS.add(f"{self.model}: '{anim}' not found, used '{a.name if a else 'nothing'}'")
        if a is not None and a.name not in self.used:
            add_cycles(a)
            self.used.add(a.name)
        ANIM_USED[f"{self.model}:{anim}"] = a.name if a else "-"
        return a

    def update(self, segs):
        if not self.ok:
            return
        pass


def build_shot(shot, dur):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = setup_render()
    build_env(shot.get("look", "meadow_day"))
    for pr in shot.get("props", []):
        if pr.get("type") == "rock":
            make_rock(pr["pos"][0], pr["pos"][1], pr.get("size", 1.5), seed=int(pr["pos"][0] * 10 + pr["pos"][1]))
    chars = core.shot_setup(shot, dur)
    actors = {}
    for c in chars:
        a = Actor(c)
        actors[c["id"]] = a
        c["actor"] = a
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 30
    cam_data.dof.use_dof = True
    cam_data.dof.aperture_fstop = 4.0
    cam = bpy.data.objects.new("Cam", cam_data)
    link(cam)
    sc.camera = cam
    target = bpy.data.objects.new("Target", None)
    link(target)
    cam_data.dof.focus_object = target
    con = cam.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return sc, cam, target, chars


def pose_frame(sc, cam, target, chars, shot, t_sec, dur):
    for c in chars:
        a = c["actor"]
        if not a.ok:
            continue
        pos, heading, anim, _action = core.state_at(c["segs"], t_sec)
        a.wrap.location = (pos[0], pos[1], a.z)
        a.wrap.rotation_euler.z = math.radians(heading + MODEL_FORWARD_OFFSET)
        s = a.base_scale
        a.wrap.scale = (s, s, s)
        act = a.pick(anim)
        if act is not None:
            for arm in a.arms:
                arm.animation_data.action = act
    cs = core.camera_state(shot.get("camera", {}), [{"id": c["id"], "height": c["height"], "segs": c["segs"]} for c in chars],
                           t_sec, dur, HFOV, VFOV)
    cam.location = Vector(cs["cam"])
    target.location = Vector(cs["target"])
    cam.data.dof.focus_distance = cs["dist"]
    fr = 1 + t_sec * 24.0
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
            shot, dur = SHOTS[ts["id"]], ts["duration"]
            sc, cam, target, chars = build_shot(shot, dur)
            for letter, frac in (("a", 0.10), ("b", 0.50), ("c", 0.92)):
                pose_frame(sc, cam, target, chars, shot, frac * dur, dur)
                render_to(sc, os.path.abspath(os.path.join(args.out, f"shot_{ts['id']:02d}_{letter}.png")))
                done += 1
            print(f"[sample] shot {ts['id']} done", flush=True)
    else:
        a = total * args.shard // args.shards
        b = total * (args.shard + 1) // args.shards
        print(f"Shard {args.shard}/{args.shards}: frames {a}..{b - 1}", flush=True)
        for ts in TL["shots"]:
            lo, hi = max(a, ts["start_frame"]), min(b, ts["end_frame"])
            if lo >= hi:
                continue
            shot, dur = SHOTS[ts["id"]], ts["duration"]
            sc, cam, target, chars = build_shot(shot, dur)
            for g in range(lo, hi):
                out = os.path.abspath(os.path.join(args.out, f"f_{g:06d}.png"))
                if os.path.exists(out):
                    continue
                pose_frame(sc, cam, target, chars, shot, (g - ts["start_frame"]) / FPS, dur)
                render_to(sc, out)
                done += 1
                if done % 10 == 0:
                    el = time.time() - t_start
                    print(f"  {done}/{b - a} frames, {el / done:.1f}s per frame", flush=True)
    print(f"Finished {done} frames in {(time.time() - t_start) / 60:.1f} min")
    if SCALE_REPORT:
        print("SCALES:", "; ".join(f"{k} {v}" for k, v in sorted(SCALE_REPORT.items())))
    if ANIM_USED:
        print("ANIMATIONS USED:", "; ".join(f"{k} -> {v}" for k, v in sorted(ANIM_USED.items())))
    if ANIM_PROBLEMS:
        print("ANIMATION PROBLEMS:", "; ".join(sorted(ANIM_PROBLEMS)))
    if MISSING:
        print("MISSING MODELS (not found in assets):", ", ".join(sorted(MISSING)))


main()
