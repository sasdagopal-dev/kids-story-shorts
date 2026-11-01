"""
Multi-shot movie renderer (Quaternius glTF animals) driven by a JSON scene plan.

Run (headless):
  blender -b --python-exit-code 1 -P movie_render.py -- --plan scene_plan.json \
      --fps 12 --engine EEVEE --sample-only --out out

Options:
  --plan FILE      scene plan JSON (default scene_plan.json)
  --assets DIR     folder searched for .glb/.gltf files (default ".")
  --fps N          frames per second (overrides the plan)
  --engine NAME    EEVEE or CYCLES
  --sample-only    render ONE middle frame per shot, then estimate the full time
  --out DIR        output folder
Frames are saved as out/frame_00001.png, out/frame_00002.png ... for all shots in order.
"""
import bpy
import sys
import os
import time
import math
import json
import random
from mathutils import Vector

NOTES = []


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


# ---------------------------------------------------------------- helpers
def make_material(name, color, roughness=0.8):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return m


def add_cylinder(name, loc, radius, depth, mat):
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=radius,
                                        depth=depth, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    return o


def add_cone(name, loc, radius, depth, mat):
    bpy.ops.mesh.primitive_cone_add(vertices=12, radius1=radius, radius2=0,
                                    depth=depth, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    return o


def add_sphere(name, loc, scale, mat):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12,
                                         radius=1.0, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = scale
    bpy.ops.object.shade_smooth()
    o.data.materials.append(mat)
    return o


def find_model(name, assets_dir):
    found = []
    for root, dirs, files in os.walk(assets_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith((".glb", ".gltf")):
                found.append(os.path.join(root, f))
    want = name.lower()
    exact = [p for p in found
             if os.path.splitext(os.path.basename(p))[0].lower() == want]
    part = [p for p in found if want in os.path.basename(p).lower()]
    pick = exact or part
    if not pick:
        lines = ["MODEL '%s' NOT FOUND in '%s'." % (name, assets_dir),
                 "Model files that were found:"]
        lines += ["  - " + p for p in sorted(found)] or ["  (none - upload the glTF files)"]
        lines.append("Fix: change the \"model\" name in scene_plan.json to one of the names above.")
        raise RuntimeError("\n".join(lines))
    pick.sort(key=lambda p: (not p.lower().endswith(".glb"), len(p)))
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
                     % (who, keyword, chosen.name,
                        ", ".join(a.name for a in actions)))
    return chosen


def tint_material(mat, tint):
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


def pos3(p):
    p = list(p)
    if len(p) == 2:
        p.append(0.0)
    return Vector((float(p[0]), float(p[1]), float(p[2])))


def lerp(a, b, u):
    return a + (b - a) * u


# ---------------------------------------------------------------- one shot
def build_shot(shot, plan, opts, fps):
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
        for attr, val in (("use_gtao", False), ("use_ssr", False),
                          ("use_soft_shadows", True)):
            try:
                setattr(scene.eevee, attr, val)
            except Exception:
                pass

    # ---- winter night: dark blue sky, cold moonlight
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.04, 0.07, 0.16, 1.0)
    bg.inputs[1].default_value = 1.0
    scene.world = world
    sun_data = bpy.data.lights.new("Moon", 'SUN')
    sun_data.energy = 3.0
    sun_data.color = (0.75, 0.82, 1.0)
    sun = bpy.data.objects.new("Moon", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(55), math.radians(10), math.radians(35))

    m_snow = make_material("Snow", (0.80, 0.86, 1.0), 0.9)
    m_ice = make_material("Ice", (0.45, 0.68, 0.88), 0.15)
    m_trunk = make_material("Trunk", (0.22, 0.14, 0.09))
    m_pine = make_material("Pine", (0.05, 0.22, 0.15))
    m_rock = make_material("Rock", (0.35, 0.36, 0.40), 0.9)

    bpy.ops.mesh.primitive_plane_add(size=400, location=(0, 0, 0))
    bpy.context.active_object.data.materials.append(m_snow)

    # frozen river behind the action
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 10, 0.03))
    river = bpy.context.active_object
    river.scale = (300, 6, 1)
    river.data.materials.append(m_ice)

    # forest behind the river
    rng = random.Random(7)
    t = 0
    for row_y in (15, 19, 24, 30):
        for x in range(-56, 57, 7):
            sc = rng.uniform(0.8, 1.4)
            tx = x + rng.uniform(-2, 2)
            ty = row_y + rng.uniform(-1.5, 1.5)
            add_cylinder("Trunk%d" % t, (tx, ty, 0.75 * sc), 0.25 * sc, 1.5 * sc, m_trunk)
            add_cone("PineA%d" % t, (tx, ty, 3.1 * sc), 1.8 * sc, 3.2 * sc, m_pine)
            add_cone("PineB%d" % t, (tx, ty, 5.0 * sc), 1.3 * sc, 2.6 * sc, m_pine)
            t += 1

    if shot.get("rock"):
        rx, ry, rtop = shot["rock"]
        add_sphere("Rock", (rx, ry, 0), (2.2, 1.8, rtop), m_rock)

    # ---- actors
    asset_facing = float(plan.get("asset_facing", 90))
    actors = {}
    for a in shot["actors"]:
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
              % (shot["id"], who, os.path.basename(model_path),
                 action.name if action else "none"), flush=True)

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
        actors[who] = {"root": root, "start": start, "end": end,
                       "zoff": -floor * s}

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
                c = sum(pts, Vector((0, 0, 0))) / len(pts)
            elif look in points:
                c = points[look].copy()
            else:
                raise RuntimeError("Shot %s: look_at '%s' is not an actor in this shot."
                                   % (shot["id"], look))
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

    shots = plan["shots"]
    total_frames = sum(max(2, int(round(float(s["seconds"]) * fps))) for s in shots)
    print("=" * 60)
    print("%s | %d shots | %d fps | %d total frames | engine %s"
          % (plan.get("title", "movie"), len(shots), fps, total_frames, opts["engine"]))
    print("=" * 60, flush=True)

    times = []
    shot_rows = []
    global_index = 0
    run_start = time.time()
    for shot in shots:
        scene, n = build_shot(shot, plan, opts, fps)
        if opts["sample_only"]:
            frames = [n // 2]
        else:
            frames = list(range(1, n + 1))
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
        shot_rows.append((shot["id"], shot.get("title", ""), n,
                          time.time() - shot_start))
        global_index += n
    run_total = time.time() - run_start

    steady = times[1:] if len(times) > 1 else times
    avg = sum(steady) / len(steady)
    est_min = avg * total_frames / 60.0
    if est_min <= 30:
        verdict = "GOOD: fast enough for a daily video."
    elif est_min <= 90:
        verdict = "OK but heavy: reduce fps, samples or scene detail if possible."
    else:
        verdict = "TOO SLOW: simplify the scene, lower fps, or use fewer frames."

    lines = [
        "## Movie render test result",
        "",
        "| Item | Value |",
        "|---|---|",
        "| Story | %s |" % plan.get("title", ""),
        "| Shots | %d |" % len(shots),
        "| Engine | %s |" % opts["engine"],
        "| Video | %d fps, %d frames = %.0f s |" % (fps, total_frames, total_frames / float(fps)),
        "| Mode | %s |" % ("SAMPLE (1 frame per shot)" if opts["sample_only"] else "FULL"),
        "| Average time per frame | %.1f s |" % avg,
        "| Estimated time for the full video | %.1f minutes |" % est_min,
        "| Actual time of this run | %.1f minutes |" % (run_total / 60.0),
        "",
        "**Verdict:** " + verdict,
        "",
        "### Shots",
        "",
        "| Shot | Title | Frames | Render time (s) |",
        "|---|---|---|---|",
    ]
    for sid, title, n, secs in shot_rows:
        lines.append("| %s | %s | %d | %.0f |" % (sid, title, n, secs))
    if NOTES:
        lines += ["", "### Notes (please read)", ""]
        lines += ["- " + x for x in NOTES]

    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "timing.json"), "w") as fh:
        json.dump({"avg_s_per_frame": avg, "est_total_min": est_min,
                   "total_frames": total_frames, "notes": NOTES}, fh)
    print("\n".join(lines), flush=True)


main()
