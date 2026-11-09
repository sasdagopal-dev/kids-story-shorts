"""shorts_render.py (v4) - renders one shard of the vertical animal Short with Blender (headless).

blender -b -P shorts_render.py -- --timeline timeline.json --story scene_plan_timed.json \
    --assets . --out out --shard 0 --shards 6 [--sample-only]

New in v4
* NOTHING hides an animal: trees, bushes, grass and boulders that stand between the camera and an animal
  (checked along the whole camera move) are switched off for that shot, and so is everything very near the camera
* animations are placed on the NLA timeline per beat: every clip starts exactly when its beat starts
  (hit-reactions play once, walk/run/idle loop), with soft cross-fades
* the animal that talks gets a small speaking nod, listeners stay calm
* a soft fill light so faces/bodies stay readable
Tuning (environment variables): MODEL_FORWARD_OFFSET (deg), WALK_ANIM_SCALE / RUN_ANIM_SCALE (>1 = slower legs),
TALK_CUE=0 to switch the speaking nod off.
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

MODEL_FORWARD_OFFSET = float(os.environ.get("MODEL_FORWARD_OFFSET", "0") or 0)
TALK_CUE = os.environ.get("TALK_CUE", "1") != "0"
CLIP_SCALE = {"walk": float(os.environ.get("WALK_ANIM_SCALE", "1") or 1), "run": float(os.environ.get("RUN_ANIM_SCALE", "1") or 1)}
OVERLAP = 4.0          # frames of cross-fade between two clips

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
    raise

TL = json.load(open(args.timeline, encoding="utf-8"))
STORY = json.load(open(args.story, encoding="utf-8"))
SHOTS = {s["id"]: s for s in STORY["shots"]}
FPS = TL["fps"]
W, H = TL["width"], TL["height"]
HFOV, VFOV = core.fovs(W, H)
os.makedirs(args.out, exist_ok=True)

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
ENV = []   # everything that could stand in the way of the camera: {"obj","x","y","r","kind"}
CLEARED = {"n": 0}


def norm_anim(n):
    n = n.split("|")[-1]
    n = re.sub(r"\.\d+$", "", n)
    n = re.sub(r"_?(animal|character)?armature$", "", n, flags=re.I)
    return n.lower()


def link(obj):
    bpy.context.scene.collection.objects.link(obj)


def register(obj, x, y, r, kind):
    ENV.append({"obj": obj, "x": x, "y": y, "r": r, "kind": kind})


def hide_item(obj):
    for o in [obj] + list(obj.children_recursive):
        o.hide_render = True
        o.hide_viewport = True


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


def apply_tint(objs, tint):
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


def make_material(color, rough=1.0):
    m = bpy.data.materials.new("mat")
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Roughness"].default_value = rough
    return m


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


DENSITY = float(os.environ.get("SCENE_DENSITY", "1.6") or 1.6)


def haze_of(look):
    """Colour that far things fade into (atmospheric depth)."""
    if look.get("haze"):
        return look["haze"]
    if look["night"]:
        return (0.05, 0.08, 0.17)
    if look["elev"] < 14 and look["sun_col"][2] < 0.5:
        return (0.85, 0.58, 0.42)
    return (0.68, 0.78, 0.90)


def lerp3(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def no_shadow(o):
    for attr in ("visible_shadow",):
        try:
            setattr(o, attr, False)
        except Exception:
            pass


def make_emissive(color, strength):
    m = bpy.data.materials.new("emit")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (*color, 1)
    em.inputs["Strength"].default_value = strength
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    return m


def ground_material(c0, c1, haze):
    """Patchy grass, dirt patches, and a fade into the haze colour far away."""
    m = bpy.data.materials.new("ground")
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.95
    try:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        n1 = nt.nodes.new("ShaderNodeTexNoise")
        n1.inputs["Scale"].default_value = 0.35
        n1.inputs["Detail"].default_value = 6.0
        r1 = nt.nodes.new("ShaderNodeValToRGB")
        r1.color_ramp.elements[0].color = (*c0, 1)
        r1.color_ramp.elements[1].color = (*c1, 1)
        nt.links.new(tc.outputs["Object"], n1.inputs["Vector"])
        nt.links.new(n1.outputs["Fac"], r1.inputs["Fac"])
        n2 = nt.nodes.new("ShaderNodeTexNoise")
        n2.inputs["Scale"].default_value = 0.11
        n2.inputs["Detail"].default_value = 3.0
        r2 = nt.nodes.new("ShaderNodeValToRGB")
        r2.color_ramp.elements[0].position = 0.58
        r2.color_ramp.elements[1].position = 0.68
        nt.links.new(tc.outputs["Object"], n2.inputs["Vector"])
        nt.links.new(n2.outputs["Fac"], r2.inputs["Fac"])
        dirt = tuple(0.55 * c0[i] + 0.45 * b for i, b in enumerate((0.40, 0.30, 0.20)))
        mixd = nt.nodes.new("ShaderNodeMixRGB")
        mixd.inputs["Color2"].default_value = (*dirt, 1)
        nt.links.new(r2.outputs["Color"], mixd.inputs["Fac"])
        nt.links.new(r1.outputs["Color"], mixd.inputs["Color1"])
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        mr = nt.nodes.new("ShaderNodeMapRange")
        mr.inputs["From Min"].default_value = 30.0
        mr.inputs["From Max"].default_value = 150.0
        nt.links.new(geo.outputs["Position"], sep.inputs["Vector"])
        nt.links.new(sep.outputs["Y"], mr.inputs["Value"])
        mixh = nt.nodes.new("ShaderNodeMixRGB")
        mixh.inputs["Color2"].default_value = (*haze, 1)
        nt.links.new(mr.outputs["Result"], mixh.inputs["Fac"])
        nt.links.new(mixd.outputs["Color"], mixh.inputs["Color1"])
        nt.links.new(mixh.outputs["Color"], bsdf.inputs["Base Color"])
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
    if look["night"]:
        bg.inputs["Color"].default_value = (0.045, 0.075, 0.17, 1)
        try:                                              # a field of stars
            tc = nt.nodes.new("ShaderNodeTexCoord")
            nz = nt.nodes.new("ShaderNodeTexNoise")
            nz.inputs["Scale"].default_value = 420.0
            nz.inputs["Detail"].default_value = 0.0
            rp = nt.nodes.new("ShaderNodeValToRGB")
            rp.color_ramp.elements[0].position = 0.80
            rp.color_ramp.elements[1].position = 0.83
            mix = nt.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "ADD"
            mix.inputs["Fac"].default_value = 1.0
            mix.inputs["Color1"].default_value = (0.045, 0.075, 0.17, 1)
            nt.links.new(tc.outputs["Generated"], nz.inputs["Vector"])
            nt.links.new(nz.outputs["Fac"], rp.inputs["Fac"])
            nt.links.new(rp.outputs["Color"], mix.inputs["Color2"])
            nt.links.new(mix.outputs["Color"], bg.inputs["Color"])
        except Exception:
            pass
    else:
        try:
            sky = nt.nodes.new("ShaderNodeTexSky")
            sky.sky_type = "NISHITA"
            sky.sun_elevation = math.radians(look["elev"])
            sky.sun_rotation = math.radians(look["az"])
            sky.sun_disc = False
            sky.dust_density = look["dust"]
            nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
        except Exception:
            bg.inputs["Color"].default_value = (0.55, 0.72, 0.95, 1)
    bg.inputs["Strength"].default_value = look["world"]
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    sc.world = w


def scatter(names, count, rng, kind, centers=None):
    """kind: tree / extra / decor. Items grow in groves and clumps (natural), and everything is registered
    so the camera can clear its view later."""
    decor = kind == "decor"
    base_r = {"tree": 1.0, "extra": 0.8, "decor": 0.3}[kind]
    count = int(round(count * DENSITY))
    cache, placed, tries = {}, 0, 0
    names = list(names)
    while placed < count and tries < count * 25 and names:
        tries += 1
        if decor:
            x, y = rng.uniform(-7, 7), rng.uniform(-3, 9)
        else:
            if centers and rng.random() < 0.75:
                cx, cy = rng.choice(centers)
                x, y = rng.gauss(cx, 4.5), rng.gauss(cy, 4.5)
            else:
                x, y = rng.uniform(-34, 34), 9 + (rng.random() ** 1.3) * 38
            x, y = max(-40, min(40, x)), max(-1.5, min(48, y))
            if -9.0 < x < 9.0 and -4.0 < y < 9.0:        # keep the action area free
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
        s = rng.uniform(0.8, 1.5) if not decor else rng.uniform(0.9, 1.25)
        w.scale = (s, s, s)
        low = name in core.SMALL_DECOR
        register(w, x, y, base_r * s, "low" if low else kind)
        placed += 1


def add_backdrop(look, rng, haze):
    """Far hills in three hazy layers, clouds, and a moon: the picture gets a real horizon."""
    base = look["ground"][0]
    for mixv, (ylo, yhi) in zip((0.55, 0.70, 0.82), ((110, 130), (135, 155), (158, 172))):
        col = lerp3(base, haze, mixv)
        for k in range(8):
            r = rng.uniform(28, 55)
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=r,
                                                  location=(-170 + k * 48 + rng.uniform(-20, 20), rng.uniform(ylo, yhi), 0))
            o = bpy.context.active_object
            o.scale = (1.6, 1.0, rng.uniform(0.28, 0.42))
            for pl in o.data.polygons:
                pl.use_smooth = True
            o.data.materials.append(make_material(col, 1.0))
            no_shadow(o)
    if look["night"]:
        for rad, strength, col, loc in ((5.0, 7.0, (1.0, 0.96, 0.82), (-8, 135, 36)), (9.0, 0.5, (0.55, 0.65, 1.0), (-8, 138, 36))):
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=rad, location=loc)
            o = bpy.context.active_object
            o.data.materials.append(make_emissive(col, strength))
            no_shadow(o)
        return
    stormy = look["dust"] >= 9 or look["sun"] < 1.0
    warm = look["elev"] < 14 and not stormy
    ccol = (0.38, 0.40, 0.46) if stormy else ((1.0, 0.78, 0.62) if warm else (1.0, 1.0, 1.0))
    cmat = make_emissive(ccol, 0.6 if stormy else 1.0)
    for _ in range(16 if stormy else 11):
        cx, cy, cz = rng.uniform(-130, 130), rng.uniform(120, 210), rng.uniform(38, 75)
        for _j in range(rng.randint(3, 5)):
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=rng.uniform(9, 17),
                                                  location=(cx + rng.uniform(-14, 14), cy + rng.uniform(-4, 4), cz + rng.uniform(-3, 3)))
            o = bpy.context.active_object
            o.scale = (1.6, 1.0, 0.6)
            o.data.materials.append(cmat)
            no_shadow(o)


def build_env(look_name):
    look = core.LOOKS.get(look_name, core.LOOKS["meadow_day"])
    haze = haze_of(look)
    build_world(look)
    bpy.ops.mesh.primitive_plane_add(size=320, location=(0, 20, 0))
    ground = bpy.context.active_object
    ground.data.materials.append(ground_material(*look["ground"], haze))
    if look["water"]:
        bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 10.5, 0.05))
        water = bpy.context.active_object
        water.scale = (320, 4.0, 1)
        water.data.materials.append(make_material((0.08, 0.28, 0.42), 0.05))
    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = look["sun"]
    sun_data.color = look["sun_col"]
    sun_data.angle = math.radians(6)
    sun = bpy.data.objects.new("Sun", sun_data)
    link(sun)
    sun.rotation_euler = (math.radians(90 - look["elev"]), 0, math.radians(look["az"]))
    fill_data = bpy.data.lights.new("Fill", "SUN")
    fill_data.energy = look["sun"] * 0.22
    fill_data.color = (0.85, 0.9, 1.0)
    fill_data.angle = math.radians(40)
    fill = bpy.data.objects.new("Fill", fill_data)
    link(fill)
    fill.rotation_euler = (math.radians(62), 0, math.radians(look["az"] + 180))
    if look["night"]:
        lamp = bpy.data.lights.new("Glow", "POINT")
        lamp.energy = 1800
        lamp.color = (1.0, 0.8, 0.5)
        lamp.shadow_soft_size = 1.5
        lo = bpy.data.objects.new("Glow", lamp)
        link(lo)
        lo.location = (0.5, -1.5, 3.0)
    rng = random.Random(sum(ord(c) for c in look_name) * 7919)
    centers = [(rng.choice([-1, 1]) * rng.uniform(9, 26), rng.uniform(9, 34)) for _ in range(5)]
    add_backdrop(look, rng, haze)
    scatter(look["trees"], look["n_trees"], rng, "tree", centers)
    scatter(look["extras"], look["n_extras"], rng, "extra", centers)
    scatter(look["decor"], look["n_decor"], rng, "decor")
    for k in range(10):
        x = rng.choice([-1, 1]) * rng.uniform(8, 26)
        size = rng.uniform(1.0, 2.6)
        y = rng.uniform(4, 36)
        register(make_rock(x, y, size, seed=k + 11), x, y, size * 0.9, "rock")


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
    if os.environ.get("VIGNETTE", "1") != "0":          # soft dark corners, cinematic look
        try:
            sc.use_nodes = True
            nt = sc.node_tree
            nt.nodes.clear()
            rl = nt.nodes.new("CompositorNodeRLayers")
            mask = nt.nodes.new("CompositorNodeEllipseMask")
            mask.width, mask.height = 0.95, 0.95
            blur = nt.nodes.new("CompositorNodeBlur")
            blur.use_relative = True
            blur.factor_x = blur.factor_y = 0.12
            mix = nt.nodes.new("CompositorNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.inputs[0].default_value = 0.45
            comp = nt.nodes.new("CompositorNodeComposite")
            nt.links.new(mask.outputs[0], blur.inputs[0])
            nt.links.new(rl.outputs["Image"], mix.inputs[1])
            nt.links.new(blur.outputs[0], mix.inputs[2])
            nt.links.new(mix.outputs[0], comp.inputs["Image"])
        except Exception:
            sc.use_nodes = False
    return sc


def is_one_shot(anim):
    a = anim.lower()
    return a.startswith(("idle_hitreact", "jump", "gallop_jump", "death"))


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

    def pick(self, anim):
        a = self.actions.get(anim.lower())
        if a is None:
            a = self.actions.get("idle") or (next(iter(self.actions.values())) if self.actions else None)
            ANIM_PROBLEMS.add(f"{self.model}: '{anim}' not found, used '{a.name if a else 'nothing'}'")
        ANIM_USED[f"{self.model}:{anim}"] = a.name if a else "-"
        return a

    def build_nla(self, segs):
        """One clip per beat, placed exactly when the beat starts. Two NLA tracks alternate so neighbours cross-fade."""
        if not self.ok:
            return
        for arm in self.arms:
            ad = arm.animation_data_create()
            ad.action = None
            for t in list(ad.nla_tracks):
                ad.nla_tracks.remove(t)
            tracks = [ad.nla_tracks.new(), ad.nla_tracks.new()]
            for i, s in enumerate(segs):
                act = self.pick(s["anim"])
                if act is None:
                    continue
                lo, hi = act.frame_range
                length = max(1.0, hi - lo)
                begin = 1.0 + s["t0"] * 24.0 - (OVERLAP if i > 0 else 0.0)
                end = 1.0 + s["t1"] * 24.0
                span = max(3.0, end - begin)
                try:
                    st = tracks[i % 2].strips.new(f"{i}_{act.name}", int(round(begin)), act)
                except Exception:
                    continue
                if st is None:
                    continue
                one = is_one_shot(s["anim"])
                sc_ = CLIP_SCALE.get(s["action"], 1.0)
                try:
                    st.scale = sc_
                    if not one:
                        st.repeat = max(0.05, span / (length * sc_))
                    slen = st.frame_end - st.frame_start
                    st.blend_in = min(OVERLAP, 0.4 * slen) if i > 0 else 0.0
                    st.blend_out = min(OVERLAP, 0.4 * slen) if i < len(segs) - 1 else 0.0
                    st.extrapolation = "HOLD" if i == 0 else ("HOLD_FORWARD" if one else "NOTHING")
                except Exception as e:
                    ANIM_PROBLEMS.add(f"{self.model}: NLA setting failed ({str(e)[:60]})")


def clear_sightlines(shot, chars, dur):
    """Switch off every tree, bush, grass tuft or boulder that would stand between the camera and an animal
    at any moment of the shot, and everything that is very close to the camera."""
    cdesc = [{"id": c["id"], "height": c["height"], "segs": c["segs"]} for c in chars]
    times = [dur * k / 6.0 for k in range(7)]
    times = sorted(set(times + [sp["at"] * dur + 0.1 for sp in shot.get("speakers", [])]))
    cams = [core.camera_state(core.cut_camera(shot, t, dur), cdesc, t, dur, HFOV, VFOV)["cam"] for t in times]
    targets = []
    for c in chars:
        hw = 0.4 * c["height"] + 0.25
        for t in times:
            p = core.state_at(c["segs"], t)[0]
            targets.append((p[0], p[1], hw))
    stand = [core.state_at(c["segs"], 0)[0] for c in chars]
    for it in ENV:
        if it["obj"].hide_render:
            continue
        x, y, r, kind = it["x"], it["y"], it["r"], it["kind"]
        # a prop boulder an animal stands on stays
        if kind == "prop" and any(math.hypot(p[0] - x, p[1] - y) < r for p in stand):
            continue
        hide = False
        for cam in cams:
            near = 2.5 if kind == "low" else 2.0 + r
            if math.hypot(x - cam[0], y - cam[1]) < near:
                hide = True
                break
            if kind == "low":
                continue                                     # small things on the ground do not hide an animal
            for tx, ty, hw in targets:
                vx, vy = tx - cam[0], ty - cam[1]
                L2 = vx * vx + vy * vy
                if L2 < 1e-6:
                    continue
                L = math.sqrt(L2)
                u = ((x - cam[0]) * vx + (y - cam[1]) * vy) / L2
                if u <= 0.0 or u * L > L + 0.3:
                    continue
                if math.hypot(cam[0] + vx * u - x, cam[1] + vy * u - y) < r + hw:
                    hide = True
                    break
            if hide:
                break
        if hide:
            hide_item(it["obj"])
            CLEARED["n"] += 1


def build_shot(shot, dur):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ENV.clear()
    sc = setup_render()
    build_env(shot.get("look", "meadow_day"))
    for pr in shot.get("props", []):
        if pr.get("type") == "rock":
            x, y, size = pr["pos"][0], pr["pos"][1], pr.get("size", 1.5)
            rock = make_rock(x, y, size, seed=int(x * 10 + y))
            register(rock, x, y, size * 0.9, "prop")
    chars = core.shot_setup(shot, dur)
    for c in chars:
        a = Actor(c)
        c["actor"] = a
        a.build_nla(c["segs"])
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 30
    cam_data.clip_end = 500
    cam_data.dof.use_dof = True
    cam_data.dof.aperture_fstop = 3.2
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
    clear_sightlines(shot, chars, dur)
    return sc, cam, target, chars


def pose_frame(sc, cam, target, chars, shot, t_sec, dur):
    for c in chars:
        a = c["actor"]
        if not a.ok:
            continue
        pos, heading, anim, action = core.state_at(c["segs"], t_sec)
        s = a.base_scale
        sx = sy = sz = s
        if TALK_CUE and action == "talk":                     # a small speaking nod, only for the animal that talks
            env = 0.5 * (math.sin(2 * math.pi * 3.1 * t_sec) + 0.6 * math.sin(2 * math.pi * 5.3 * t_sec + 1.0))
            bob = 0.03 * max(0.0, env)
            sx = sy = s * (1.0 - 0.4 * bob)
            sz = s * (1.0 + bob)
            heading += 2.0 * math.sin(2 * math.pi * 1.3 * t_sec)
        a.wrap.location = (pos[0], pos[1], a.z)
        a.wrap.rotation_euler.z = math.radians(heading + MODEL_FORWARD_OFFSET)
        a.wrap.scale = (sx, sy, sz)
    cs = core.camera_state(core.cut_camera(shot, t_sec, dur), [{"id": c["id"], "height": c["height"], "segs": c["segs"]} for c in chars],
                           t_sec, dur, HFOV, VFOV)
    cam.location = Vector(cs["cam"])
    target.location = Vector(cs["target"])
    cam.data.dof.focus_distance = cs["dist"]
    fr = 1 + t_sec * 24.0
    sc.frame_set(int(fr), subframe=fr - int(fr))


def render_to(sc, path):
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


def calibrate(models):
    """Measure the real nose-to-tail length of every animal, so animals never overlap."""
    for m in sorted(models):
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            res = load_model(m)
            if not res:
                continue
            bpy.context.view_layer.update()
            xs, ys, zs = [], [], []
            for o in res[1]:
                if o.type != "MESH":
                    continue
                for cc in o.bound_box:
                    v = o.matrix_world @ Vector(cc)
                    xs.append(v.x); ys.append(v.y); zs.append(v.z)
            h = max(zs) - min(zs)
            if h > 0.01:
                core.MEASURED_LEN[m] = round(max(max(xs) - min(xs), max(ys) - min(ys)) * core.TARGET_HEIGHT.get(m, 1.5) / h, 2)
        except Exception as e:
            print("calibrate failed for", m, str(e)[:80])
    print("LENGTHS (m):", "; ".join(f"{k} {v}" for k, v in sorted(core.MEASURED_LEN.items())), flush=True)


def main():
    calibrate({c["model"] for s in STORY["shots"] for c in s["characters"]})
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
    print(f"Objects switched off because they hid an animal or the camera: {CLEARED['n']}")
    if SCALE_REPORT:
        print("SCALES:", "; ".join(f"{k} {v}" for k, v in sorted(SCALE_REPORT.items())))
    if ANIM_USED:
        print("ANIMATIONS USED:", "; ".join(f"{k} -> {v}" for k, v in sorted(ANIM_USED.items())))
    if ANIM_PROBLEMS:
        print("ANIMATION PROBLEMS:", "; ".join(sorted(ANIM_PROBLEMS)))
    if MISSING:
        print("MISSING MODELS (not found in assets):", ", ".join(sorted(MISSING)))


main()
