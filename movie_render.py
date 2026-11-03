"""
Multi-shot movie renderer v2 (Quaternius glTF animals + Stylized Nature props).

Run (headless):
  blender -b --python-exit-code 1 -P movie_render.py -- --plan scene_plan_timed.json \
      --fps 12 --engine EEVEE --out out [--sample-only]

Per shot the plan can set:
  env      forest_day | forest_sunset | fire_red | winter_night   (sky, light, ground, props, snow)
  river    false to remove the frozen river / water
  rock     [x, y, top_z] a rock to stand on
  actors   animals / characters (model, action, size, start, end, heading, tint)
  camera   look_at, offset or pos/pos_end, lens, shake

Every decoration (props, sky, snow) is protected: if something fails the shot still renders
with simpler scenery and a note is written to the summary.
"""
import bpy
import sys
import os
import time
import math
import json
import random
import zlib
from mathutils import Vector, Euler

NOTES = []

# ---------------------------------------------------------------- look presets
ENV = {
    "forest_day": {
        "sky_top": (0.20, 0.50, 0.95), "sky_horizon": (0.80, 0.92, 1.0),
        "key": ((1.0, 0.96, 0.88), 4.2, (52, 10, 35)), "fill": ((0.6, 0.75, 1.0), 0.9),
        "ground": (0.30, 0.62, 0.20), "disc": ((1.0, 0.96, 0.75), 13.0, 3.0),
        "river": (0.22, 0.52, 0.85, 0.25),
        "trees": ["CommonTree_"], "bushes": ["Bush_Common"],
        "flowers": ["Flower_", "Clover_"], "grass": ["Grass_Common", "Grass_Wispy"],
        "extras": ["Mushroom_", "Pebble_Round"], "frost": None, "snow": 0,
    },
    "forest_sunset": {
        "sky_top": (0.22, 0.26, 0.62), "sky_horizon": (1.0, 0.60, 0.26),
        "key": ((1.0, 0.62, 0.34), 4.0, (74, 10, 60)), "fill": ((0.5, 0.5, 0.95), 0.8),
        "ground": (0.34, 0.52, 0.18), "disc": ((1.0, 0.78, 0.38), 17.0, 4.0),
        "river": (0.95, 0.55, 0.40, 0.2),
        "trees": ["CommonTree_"], "bushes": ["Bush_Common"],
        "flowers": ["Flower_", "Clover_"], "grass": ["Grass_Common", "Grass_Wispy"],
        "extras": ["Mushroom_", "Pebble_Round"], "frost": None, "snow": 0,
    },
    "fire_red": {
        "sky_top": (0.16, 0.03, 0.03), "sky_horizon": (1.0, 0.32, 0.06),
        "key": ((1.0, 0.42, 0.18), 3.8, (70, 10, 50)), "fill": ((0.9, 0.22, 0.1), 0.9),
        "ground": (0.17, 0.10, 0.07), "disc": ((1.0, 0.35, 0.10), 20.0, 5.0),
        "river": None,
        "trees": ["DeadTree_"], "bushes": [], "flowers": [],
        "grass": ["Grass_Wispy"], "extras": ["Pebble_Square"], "frost": None, "snow": 0,
    },
    "winter_night": {
        "sky_top": (0.01, 0.03, 0.12), "sky_horizon": (0.12, 0.22, 0.42),
        "key": ((0.72, 0.82, 1.0), 3.4, (55, 10, 35)), "fill": ((0.4, 0.5, 1.0), 0.9),
        "ground": (0.82, 0.88, 1.0), "disc": ((0.92, 0.96, 1.0), 11.0, 6.0),
        "river": (0.50, 0.72, 0.92, 0.12),
        "trees": ["DeadTree_", "CommonTree_"], "bushes": ["Bush_Common"], "flowers": [],
        "grass": ["Grass_Wispy"], "extras": ["Pebble_Round"],
        "frost": ((0.92, 0.96, 1.0), 0.7), "snow": 1,
    },
}


# ---------------------------------------------------------------- arguments
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts = {"plan": "scene_plan.json", "assets": ".", "fps": None,
            "engine": "EEVEE", "sample_only": False, "out": "out"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--plan":
            opts["plan"] = argv[i + 1]; i += 2
        elif a == "--assets":
            opts["assets"] = argv[i + 1]; i += 2
        elif a == "--fps":
            opts["fps"] = int(argv[i + 1]); i += 2
        elif a == "--engine":
            opts["engine"] = argv[i + 1].upper(); i += 2
        elif a == "--out":
            opts["out"] = argv[i + 1]; i += 2
        elif a == "--sample-only":
            opts["sample_only"] = True; i += 1
        else:
            i += 1
    return opts


# ---------------------------------------------------------------- small helpers
def make_material(name, color, roughness=0.8):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return m


def emission_material(name, color, strength):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (color[0], color[1], color[2], 1.0)
    em.inputs["Strength"].default_value = strength
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    try:
        m.shadow_method = 'NONE'
    except Exception:
        pass
    return m


def add_cylinder(name, loc, radius, depth, mat):
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=radius, depth=depth, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    return o


def add_cone(name, loc, radius, depth, mat):
    bpy.ops.mesh.primitive_cone_add(vertices=12, radius1=radius, radius2=0, depth=depth, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    return o


def add_sphere(name, loc, scale, mat, radius=1.0, segments=24, rings=12):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=radius, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = scale
    bpy.ops.object.shade_smooth()
    o.data.materials.append(mat)
    return o


def list_models(assets_dir):
    found = {}
    for root, dirs, files in os.walk(assets_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "out"]
        for f in sorted(files):
            if f.lower().endswith((".glb", ".gltf")):
                stem = os.path.splitext(f)[0]
                p = os.path.join(root, f)
                if stem not in found or len(p) < len(found[stem]):
                    found[stem] = p
    return found


def find_model(name, assets_dir):
    found = list_models(assets_dir)
    want = name.lower()
    exact = [p for s, p in found.items() if s.lower() == want]
    part = [p for s, p in found.items() if want in s.lower()]
    pick = exact or part
    if not pick:
        lines = ["MODEL '%s' NOT FOUND in '%s'." % (name, assets_dir), "Model files that were found:"]
        lines += ["  - " + p for p in sorted(found.values())] or ["  (none - upload the glTF files)"]
        lines.append("Fix: change the \"model\" name in the scene plan to one of the names above.")
        raise RuntimeError("\n".join(lines))
    pick.sort(key=len)
    return pick[0]


def world_bounds(objs):
    mins = [1e9, 1e9, 1e9]
    maxs = [-1e9, -1e9, -1e9]
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            for i in range(3):
                mins[i] = min(mins[i], w[i])
                maxs[i] = max(maxs[i], w[i])
    return Vector(mins), Vector(maxs)


def pick_action(actions, keyword, who):
    """Choose an animation by name; fall back to Idle, then the first one."""
    if not actions:
        NOTES.append("%s: this model has no animations." % who)
        return None
    kw = keyword.lower()

    def best(word):
        starts = [a for a in actions if a.name.lower().startswith(word)]
        part = [a for a in actions if word in a.name.lower()]
        pool = starts or part
        pool.sort(key=lambda a: len(a.name))
        return pool[0] if pool else None

    chosen = best(kw)
    if chosen is None:
        chosen = best("idle") or actions[0]
        NOTES.append("%s: animation '%s' not found, used '%s'. Available: %s"
                     % (who, keyword, chosen.name, ", ".join(a.name for a in actions)))
    return chosen


def tint_material(mat, tint):
    """Multiply the colour of a material (for example make a wolf dark)."""
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    if bsdf is None:
        return
    sock = bsdf.inputs["Base Color"]
    if sock.is_linked:
        src = sock.links[0].from_socket
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = 'MULTIPLY'
        mix.inputs[0].default_value = 1.0
        mix.inputs[2].default_value = (tint[0], tint[1], tint[2], 1.0)
        nt.links.new(src, mix.inputs[1])
        nt.links.new(mix.outputs[0], sock)
    else:
        c = sock.default_value
        sock.default_value = (c[0] * tint[0], c[1] * tint[1], c[2] * tint[2], 1.0)


def frost_material(mat, color, amount):
    """Blend a material towards a colour (for example white frost on trees)."""
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    if bsdf is None:
        return
    sock = bsdf.inputs["Base Color"]
    if sock.is_linked:
        src = sock.links[0].from_socket
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = 'MIX'
        mix.inputs[0].default_value = amount
        mix.inputs[2].default_value = (color[0], color[1], color[2], 1.0)
        nt.links.new(src, mix.inputs[1])
        nt.links.new(mix.outputs[0], sock)
    else:
        c = sock.default_value
        sock.default_value = tuple(c[i] * (1 - amount) + color[i] * amount for i in range(3)) + (1.0,)


def pos3(p):
    p = list(p)
    if len(p) == 2:
        p.append(0.0)
    return Vector((float(p[0]), float(p[1]), float(p[2])))


def lerp(a, b, u):
    return a + (b - a) * u


# ---------------------------------------------------------------- sky, light, ground
def add_sky(env, scene):
    """Gradient sky dome plus a sun or moon disc (no shadows, pure emission)."""
    bpy.ops.mesh.primitive_uv_sphere_add(radius=450, segments=32, ring_count=16, location=(0, 0, 0))
    dome = bpy.context.active_object
    dome.name = "SkyDome"
    m = bpy.data.materials.new("SkyGradient")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    mr = nt.nodes.new("ShaderNodeMapRange")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    nt.links.new(tc.outputs["Object"], sep.inputs["Vector"])
    nt.links.new(sep.outputs["Z"], mr.inputs["Value"])
    mr.inputs["From Min"].default_value = 0.0
    mr.inputs["From Max"].default_value = 260.0
    mr.inputs["To Min"].default_value = 0.0
    mr.inputs["To Max"].default_value = 1.0
    mr.clamp = True
    nt.links.new(mr.outputs["Result"], ramp.inputs["Fac"])
    h, t = env["sky_horizon"], env["sky_top"]
    ramp.color_ramp.elements[0].color = (h[0], h[1], h[2], 1.0)
    ramp.color_ramp.elements[1].color = (t[0], t[1], t[2], 1.0)
    nt.links.new(ramp.outputs["Color"], em.inputs["Color"])
    em.inputs["Strength"].default_value = 1.0
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    try:
        m.shadow_method = 'NONE'
    except Exception:
        pass
    dome.data.materials.append(m)

    # sun or moon disc in front of the camera
    dcol, drad, dstr = env["disc"]
    direction = Vector((-0.28, 1.0, 0.36)).normalized() * 400.0
    add_sphere("SkyDisc", direction, (1, 1, 1), emission_material("Disc", dcol, dstr),
               radius=drad, segments=24, rings=12)


def add_lights(scene, env):
    kcol, kenergy, krot = env["key"]
    key_data = bpy.data.lights.new("Key", 'SUN')
    key_data.energy = kenergy
    key_data.color = kcol
    key = bpy.data.objects.new("Key", key_data)
    scene.collection.objects.link(key)
    key.rotation_euler = Euler(tuple(math.radians(a) for a in krot), 'XYZ')

    fcol, fenergy = env["fill"]
    fill_data = bpy.data.lights.new("Fill", 'SUN')
    fill_data.energy = fenergy
    fill_data.color = fcol
    try:
        fill_data.use_shadow = False
    except Exception:
        pass
    fill = bpy.data.objects.new("Fill", fill_data)
    scene.collection.objects.link(fill)
    fill.rotation_euler = Euler((math.radians(60), 0, math.radians(215)), 'XYZ')


# ---------------------------------------------------------------- scenery props (Stylized Nature kit)
def stems_for(models, prefixes):
    out = []
    for stem in sorted(models):
        low = stem.lower()
        if any(low.startswith(p.lower()) for p in prefixes):
            out.append(stem)
    return out


def import_template(path):
    before = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o.name not in before]
    bpy.context.view_layer.update()
    meshes = [o for o in new if o.type == 'MESH']
    if not meshes:
        for o in new:
            bpy.data.objects.remove(o, do_unlink=True)
        return None
    mn, mx = world_bounds(meshes)
    return {"objs": new, "meshes": meshes, "zmin": mn.z, "height": max(mx.z - mn.z, 0.01)}


def make_instance(scene, tpl, loc, rot_z, scale):
    holder = bpy.data.objects.new("Prop", None)
    scene.collection.objects.link(holder)
    mapping = {}
    for o in tpl["objs"]:
        c = o.copy()
        scene.collection.objects.link(c)
        mapping[o.name] = c
    for o in tpl["objs"]:
        c = mapping[o.name]
        if o.parent is not None and o.parent.name in mapping:
            c.parent = mapping[o.parent.name]
            c.matrix_parent_inverse = o.matrix_parent_inverse.copy()
        else:
            c.parent = holder
    holder.location = (loc[0], loc[1], loc[2] - tpl["zmin"] * scale)
    holder.rotation_euler = (0, 0, rot_z)
    holder.scale = (scale, scale, scale)
    return holder


def pick_band(rng, bands):
    total = sum(b[2] for b in bands)
    r = rng.uniform(0, total)
    for lo, hi, w in bands:
        if r <= w:
            return rng.uniform(lo, hi)
        r -= w
    return rng.uniform(bands[-1][0], bands[-1][1])


def build_scenery(scene, env, models, region, rock, has_river, seed_name):
    rng = random.Random(zlib.crc32(seed_name.encode("utf-8")))
    xmin, xmax = region
    width = xmax - xmin

    if has_river:
        tree_bands = [(14, 38, 1.0), (4.8, 6.4, 0.18)]
        bush_bands = [(3.4, 6.6, 0.6), (14, 26, 0.4)]
        low_bands = [(-5, 6.5, 0.8), (14, 22, 0.2)]
    else:
        tree_bands = [(4.5, 38, 1.0)]
        bush_bands = [(3.4, 12, 0.6), (12, 26, 0.4)]
        low_bands = [(-5, 12, 0.7), (12, 24, 0.3)]

    groups = [
        # key, objects per metre, cap, variants to import, bands, scale range
        ("trees", 0.9, 70, 4, tree_bands, (0.85, 1.30)),
        ("bushes", 0.6, 40, 2, bush_bands, (0.80, 1.30)),
        ("flowers", 0.8, 40, 3, low_bands, (0.90, 1.30)),
        ("grass", 2.5, 110, 3, low_bands, (0.90, 1.40)),
        ("extras", 0.5, 25, 3, low_bands, (0.80, 1.40)),
    ]

    # import each variant once
    templates = {}
    for key, _d, _c, nvar, _b, _s in groups:
        stems = stems_for(models, env.get(key, []))
        if not stems:
            continue
        chosen = rng.sample(stems, min(nvar, len(stems)))
        tpls = []
        for stem in chosen:
            try:
                tpl = import_template(models[stem])
                if tpl is not None:
                    tpls.append(tpl)
            except Exception as ex:
                NOTES.append("scenery model %s could not be loaded: %s" % (stem, ex))
        if tpls:
            templates[key] = tpls

    if "trees" not in templates:
        raise RuntimeError("no tree models found (looked for %s)" % env.get("trees"))

    # one common scale so the whole kit keeps its relative sizes (trees about 7.5 units tall)
    kit = 7.5 / templates["trees"][0]["height"]
    kit = min(max(kit, 0.15), 8.0)

    frost = env.get("frost")
    if frost:
        for key in ("trees", "bushes"):
            for tpl in templates.get(key, []):
                for o in tpl["meshes"]:
                    for slot in o.material_slots:
                        if slot.material:
                            slot.material = slot.material.copy()
                            frost_material(slot.material, frost[0], frost[1])

    count = 0
    for key, per_m, cap, _n, bands, srange in groups:
        tpls = templates.get(key)
        if not tpls:
            continue
        n = min(cap, int(width * per_m))
        for _ in range(n):
            x = rng.uniform(xmin, xmax)
            y = pick_band(rng, bands)
            if rock and abs(x - rock[0]) < 3.2 and abs(y - rock[1]) < 2.8:
                continue
            tpl = rng.choice(tpls)
            make_instance(scene, tpl, (x, y, 0.0), rng.uniform(0, 6.283), kit * rng.uniform(*srange))
            count += 1

    # remove the template originals (the copies keep the shared mesh data)
    for tpls in templates.values():
        for tpl in tpls:
            for o in tpl["objs"]:
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass
    print("Scenery: %d props placed (kit scale %.2f)" % (count, kit), flush=True)


def build_simple_forest(env, region):
    """Fallback forest made of cones, used only when the nature kit cannot be loaded."""
    m_trunk = make_material("Trunk", (0.22, 0.14, 0.09))
    m_pine = make_material("Pine", (0.10, 0.40, 0.20))
    rng = random.Random(7)
    x = region[0]
    t = 0
    while x <= region[1]:
        for row_y in (15, 21, 28):
            sc = rng.uniform(0.8, 1.4)
            tx, ty = x + rng.uniform(-2, 2), row_y + rng.uniform(-1.5, 1.5)
            add_cylinder("Trunk%d" % t, (tx, ty, 0.75 * sc), 0.25 * sc, 1.5 * sc, m_trunk)
            add_cone("PineA%d" % t, (tx, ty, 3.1 * sc), 1.8 * sc, 3.2 * sc, m_pine)
            add_cone("PineB%d" % t, (tx, ty, 5.0 * sc), 1.3 * sc, 2.6 * sc, m_pine)
            t += 1
        x += 7


def add_snow(scene, region, n_frames, fps, seed_name, count):
    rng = random.Random(zlib.crc32(("snow" + seed_name).encode("utf-8")))
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.045, location=(0, -60, -60))
    tpl = bpy.context.active_object
    tpl.name = "SnowTpl"
    tpl.data.materials.append(emission_material("Flake", (1, 1, 1), 4.0))
    secs = n_frames / float(fps)
    fall = 1.1 * secs
    for _ in range(count):
        o = tpl.copy()
        scene.collection.objects.link(o)
        x = rng.uniform(region[0], region[1])
        y = rng.uniform(-6.5, 9.0)
        z0 = rng.uniform(0.2, 9.0)
        o.location = (x, y, z0)
        o.keyframe_insert(data_path="location", frame=1)
        o.location = (x + rng.uniform(-1.0, 1.0), y, z0 - fall)
        o.keyframe_insert(data_path="location", frame=n_frames)
        if o.animation_data and o.animation_data.action:
            for fc in o.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'
    bpy.data.objects.remove(tpl, do_unlink=True)


# ---------------------------------------------------------------- one shot
def shot_region(shot):
    xs = []
    for a in shot.get("actors", []):
        xs.append(float(a["start"][0]))
        xs.append(float(a.get("end", a["start"])[0]))
    cam = shot.get("camera", {})
    for key in ("pos", "pos_end"):
        if cam.get(key):
            xs.append(float(cam[key][0]))
    if isinstance(cam.get("look_at"), list):
        xs.append(float(cam["look_at"][0]))
    if not xs:
        xs = [0.0]
    return (min(xs) - 13.0, max(xs) + 13.0)


def build_shot(shot, plan, opts, fps, models):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
    except Exception:
        pass
    scene = bpy.context.scene
    n = max(2, int(round(float(shot["seconds"]) * fps)))
    scene.frame_start = 1
    scene.frame_end = n
    scene.render.fps = fps  # BEFORE import so animation timing is right
    scene.render.resolution_x = 720
    scene.render.resolution_y = 1280
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.view_settings.view_transform = 'Standard'

    if opts["engine"] == "CYCLES":
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 16
        scene.cycles.use_denoising = True
    else:
        scene.render.engine = 'BLENDER_EEVEE'
        scene.eevee.taa_render_samples = 16
        for attr, val in (("use_gtao", False), ("use_ssr", False), ("use_soft_shadows", True)):
            try:
                setattr(scene.eevee, attr, val)
            except Exception:
                pass

    # ---- look of this shot
    env_name = shot.get("env", "winter_night")
    env = dict(ENV.get(env_name, ENV["winter_night"]))
    if shot.get("sky"):                       # old plans: a single sky colour
        env["sky_top"] = env["sky_horizon"] = tuple(shot["sky"])
    has_river = bool(env.get("river")) and shot.get("river", True)
    region = shot_region(shot)

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    h = env["sky_horizon"]
    bg.inputs[0].default_value = (h[0], h[1], h[2], 1.0)
    bg.inputs[1].default_value = 1.0
    scene.world = world

    try:
        add_sky(env, scene)
    except Exception as ex:
        NOTES.append("Shot %s: sky gradient failed (%s), flat sky used." % (shot["id"], ex))
    add_lights(scene, env)

    gcol = env["ground"]
    bpy.ops.mesh.primitive_plane_add(size=400, location=(0, 0, 0))
    bpy.context.active_object.data.materials.append(make_material("Ground", gcol, 0.95))

    if has_river:
        rcol = env["river"]
        bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 10, 0.03))
        river = bpy.context.active_object
        river.scale = (300, 6, 1)
        river.data.materials.append(make_material("Water", rcol[:3], rcol[3]))

    rock = shot.get("rock")
    if rock:
        rx, ry, rtop = rock
        add_sphere("Rock", (rx, ry, 0), (2.2, 1.8, rtop), make_material("Rock", (0.38, 0.38, 0.42), 0.9))

    try:
        build_scenery(scene, env, models, region, rock, has_river, env_name)
    except Exception as ex:
        NOTES.append("Shot %s (%s): nature props failed (%s) - simple forest used." % (shot["id"], env_name, ex))
        print("WARNING scenery:", ex, flush=True)
        build_simple_forest(env, region)

    if env.get("snow"):
        try:
            add_snow(scene, region, n, fps, env_name, min(420, int((region[1] - region[0]) * 8)))
        except Exception as ex:
            NOTES.append("Shot %s: snowfall failed (%s)." % (shot["id"], ex))

    # ---- actors
    asset_facing = float(plan.get("asset_facing", 90))
    actors = {}
    for a in shot.get("actors", []):
        who = a["name"]
        model_path = find_model(a["model"], opts["assets"])
        before_objs = set(o.name for o in bpy.data.objects)
        before_acts = set(x.name for x in bpy.data.actions)
        bpy.ops.import_scene.gltf(filepath=model_path)
        new_objs = [o for o in bpy.data.objects if o.name not in before_objs]
        new_acts = [x for x in bpy.data.actions if x.name not in before_acts]
        if not new_acts:
            new_acts = list(bpy.data.actions)
        meshes = [o for o in new_objs if o.type == 'MESH']
        armatures = [o for o in new_objs if o.type == 'ARMATURE']
        bpy.context.view_layer.update()

        if meshes:
            mn, mx = world_bounds(meshes)
            dim = mx - mn
            length = max(dim.x, dim.y)
            floor = mn.z
        else:
            length, floor = 2.0, 0.0
        if length < 0.05:
            length = 2.0
        s = float(a.get("size", 2.0)) / length

        if a.get("tint"):
            for o in meshes:
                for slot in o.material_slots:
                    if slot.material:
                        slot.material = slot.material.copy()
                        tint_material(slot.material, a["tint"])

        action = pick_action(new_acts, a.get("action", "Idle"), who)
        if action is not None:
            for arm in armatures:
                ad = arm.animation_data or arm.animation_data_create()
                for track in list(ad.nla_tracks):
                    ad.nla_tracks.remove(track)
                ad.action = action
            for fc in action.fcurves:
                fc.modifiers.new('CYCLES')
        print("Shot %s | %s = %s | action: %s"
              % (shot["id"], who, os.path.basename(model_path), action.name if action else "none"), flush=True)

        bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))
        root = bpy.context.active_object
        root.name = "Root_" + who
        for o in new_objs:
            if o.parent is None:
                o.parent = root
        root.scale = (s, s, s)

        start = pos3(a["start"])
        end = pos3(a.get("end", a["start"]))
        if "heading" in a:
            heading = float(a["heading"])
        else:
            d = end - start
            heading = math.degrees(math.atan2(d.y, d.x)) if (d.x ** 2 + d.y ** 2) > 1e-4 else 0.0
        root.rotation_euler = (0, 0, math.radians(heading + asset_facing))
        actors[who] = {"root": root, "start": start, "end": end, "zoff": -floor * s}

    # ---- camera
    cam_spec = shot.get("camera", {})
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = float(cam_spec.get("lens", 30))
    cam_data.clip_end = 1000
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))
    target = bpy.context.active_object
    target.name = "CamTarget"
    con = cam.constraints.new('TRACK_TO')
    con.target = target
    con.track_axis = 'TRACK_NEGATIVE_Z'
    con.up_axis = 'UP_Y'

    look = cam_spec.get("look_at", "all")
    offset = cam_spec.get("offset")
    pos_a = pos3(cam_spec["pos"]) if cam_spec.get("pos") else None
    pos_b = pos3(cam_spec["pos_end"]) if cam_spec.get("pos_end") else pos_a
    shake = float(cam_spec.get("shake", 0.0))

    for f in range(1, n + 1):
        u = (f - 1) / float(n - 1)
        points = {}
        for who, d in actors.items():
            p = lerp(d["start"], d["end"], u)
            p = Vector((p.x, p.y, p.z + d["zoff"]))
            d["root"].location = p
            d["root"].keyframe_insert(data_path="location", frame=f)
            points[who] = p

        if isinstance(look, str):
            if look == "all":
                pts = list(points.values())
                c = (sum(pts, Vector((0, 0, 0))) / len(pts)) if pts else Vector((0, 0, 0))
            elif look in points:
                c = points[look].copy()
            else:
                raise RuntimeError("Shot %s: look_at '%s' is not an actor in this shot." % (shot["id"], look))
            c.z += 0.8
        else:
            c = pos3(look)
        target.location = c
        target.keyframe_insert(data_path="location", frame=f)

        if offset is not None:
            cp = c + pos3(offset)
        elif pos_a is not None:
            cp = lerp(pos_a, pos_b, u)
        else:
            cp = c + Vector((0, -9, 2))
        if shake > 0:
            cp = cp + Vector((shake * (math.sin(f * 1.9) + math.sin(f * 3.1 + 1.0)) * 0.5, 0,
                              shake * (math.sin(f * 2.3 + 2.0) + math.sin(f * 3.7)) * 0.5))
        cam.location = cp
        cam.keyframe_insert(data_path="location", frame=f)

    return scene, n


# ---------------------------------------------------------------- main
def main():
    opts = parse_args()
    with open(opts["plan"], "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    fps = opts["fps"] or int(plan.get("fps", 12))
    out = os.path.abspath(opts["out"])
    os.makedirs(out, exist_ok=True)
    models = list_models(opts["assets"])
    print("Models found in the repository: %d" % len(models), flush=True)

    shots = plan["shots"]
    total_frames = sum(max(2, int(round(float(s["seconds"]) * fps))) for s in shots)
    print("=" * 60)
    print("%s | %d shots | %d fps | %d total frames | engine %s"
          % (plan.get("title", "movie"), len(shots), fps, total_frames, opts["engine"]))
    print("=" * 60, flush=True)

    times, shot_rows = [], []
    global_index = 0
    run_start = time.time()
    for shot in shots:
        scene, n = build_shot(shot, plan, opts, fps, models)
        frames = [n // 2] if opts["sample_only"] else list(range(1, n + 1))
        shot_start = time.time()
        for f in frames:
            scene.frame_set(f)
            if opts["sample_only"]:
                name = "sample_shot%02d" % int(shot["id"])
            else:
                name = "frame_%05d" % (global_index + f)
            scene.render.filepath = os.path.join(out, name)
            t0 = time.time()
            bpy.ops.render.render(write_still=True)
            dt = time.time() - t0
            times.append(dt)
            print("Shot %s frame %d/%d took %.1f s" % (shot["id"], f, n, dt), flush=True)
        shot_rows.append((shot["id"], shot.get("title", ""), n, time.time() - shot_start))
        global_index += n
    run_total = time.time() - run_start

    steady = times[1:] if len(times) > 1 else times
    avg = sum(steady) / len(steady)
    est_min = avg * total_frames / 60.0
    if est_min <= 30:
        verdict = "GOOD: fast enough for a daily video."
    elif est_min <= 150:
        verdict = "OK: heavy but fine for one video per day."
    else:
        verdict = "TOO SLOW: simplify the scenes, lower fps, or use fewer frames."

    lines = [
        "## Movie render result", "",
        "| Item | Value |", "|---|---|",
        "| Story | %s |" % plan.get("title", ""),
        "| Shots | %d |" % len(shots),
        "| Engine | %s |" % opts["engine"],
        "| Video | %d fps, %d frames = %.0f s |" % (fps, total_frames, total_frames / float(fps)),
        "| Mode | %s |" % ("SAMPLE (1 frame per shot)" if opts["sample_only"] else "FULL"),
        "| Average time per frame | %.1f s |" % avg,
        "| Estimated time for the full video | %.1f minutes |" % est_min,
        "| Actual time of this run | %.1f minutes |" % (run_total / 60.0),
        "", "**Verdict:** " + verdict, "",
        "### Shots", "", "| Shot | Title | Frames | Render time (s) |", "|---|---|---|---|",
    ]
    for sid, title, n, secs in shot_rows:
        lines.append("| %s | %s | %d | %.0f |" % (sid, title, n, secs))
    if NOTES:
        lines += ["", "### Notes (please read)", ""] + ["- " + x for x in NOTES]

    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "timing.json"), "w") as fh:
        json.dump({"avg_s_per_frame": avg, "est_total_min": est_min,
                   "total_frames": total_frames, "notes": NOTES}, fh)
    print("\n".join(lines), flush=True)


main()
