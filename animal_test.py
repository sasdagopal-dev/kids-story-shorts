"""
Animal render test (Quaternius glTF animals) for the kids story Shorts project.

Run (headless):
  blender -b --python-exit-code 1 -P animal_test.py -- --model Horse --action Walk \
      --rotate-z 90 --seconds 30 --fps 12 --engine EEVEE --sample-only --out out

Options:
  --model NAME     animal file name without extension, e.g. Horse (searched in --assets)
  --assets DIR     folder to search for .glb/.gltf files (default ".")
  --action WORD    animation to play, any part of its name, e.g. Walk, Gallop, Idle
  --rotate-z DEG   turn the animal so it faces the walking direction (0/90/180/270)
  --seconds N      video length in seconds
  --fps N          frames per second
  --engine NAME    EEVEE or CYCLES
  --sample-only    render only 6 spread-out frames, then estimate the full time
  --out DIR        output folder
"""
import bpy
import sys
import os
import time
import math
import json
from mathutils import Vector


# ---------------------------------------------------------------- arguments
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts = {"model": "Horse", "assets": ".", "action": "Walk", "rotate_z": 90.0,
            "seconds": 30.0, "fps": 12, "engine": "EEVEE",
            "sample_only": False, "out": "out", "speed": 1.4}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--model":
            opts["model"] = argv[i + 1]; i += 2
        elif a == "--assets":
            opts["assets"] = argv[i + 1]; i += 2
        elif a == "--action":
            opts["action"] = argv[i + 1]; i += 2
        elif a == "--rotate-z":
            opts["rotate_z"] = float(argv[i + 1]); i += 2
        elif a == "--seconds":
            opts["seconds"] = float(argv[i + 1]); i += 2
        elif a == "--fps":
            opts["fps"] = int(argv[i + 1]); i += 2
        elif a == "--engine":
            opts["engine"] = argv[i + 1].upper(); i += 2
        elif a == "--speed":
            opts["speed"] = float(argv[i + 1]); i += 2
        elif a == "--out":
            opts["out"] = argv[i + 1]; i += 2
        elif a == "--sample-only":
            opts["sample_only"] = True; i += 1
        else:
            i += 1
    return opts


# ---------------------------------------------------------------- helpers
def make_material(name, color, roughness=0.7):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return m


def add_sphere(name, loc, scale, mat):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12,
                                         radius=1.0, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = scale
    bpy.ops.object.shade_smooth()
    o.data.materials.append(mat)
    return o


def add_cylinder(name, loc, radius, depth, mat):
    bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=radius,
                                        depth=depth, location=loc)
    o = bpy.context.active_object
    o.name = name
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


def pick_action(keyword):
    acts = list(bpy.data.actions)
    if not acts:
        return None, []
    names = [a.name for a in acts]
    kw = keyword.lower()
    hits = [a for a in acts if kw in a.name.lower()]
    hits.sort(key=lambda a: len(a.name))
    return (hits[0] if hits else None), names


# ---------------------------------------------------------------- scene
def build_scene(opts):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
    except Exception:
        pass
    scene = bpy.context.scene

    fps = opts["fps"]
    total = int(round(opts["seconds"] * fps))
    scene.frame_start = 1
    scene.frame_end = total
    scene.render.fps = fps  # set BEFORE import so animation timing is right
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

    # sky + sun
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.55, 0.80, 1.0, 1.0)
    bg.inputs[1].default_value = 1.0
    scene.world = world
    sun_data = bpy.data.lights.new("Sun", 'SUN')
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), math.radians(10), math.radians(30))

    # ---- import the animal
    model_path = find_model(opts["model"], opts["assets"])
    print("Using model file:", model_path, flush=True)
    before = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=model_path)
    new_objs = [o for o in bpy.data.objects if o.name not in before]
    meshes = [o for o in new_objs if o.type == 'MESH']
    armatures = [o for o in new_objs if o.type == 'ARMATURE']
    bpy.context.view_layer.update()

    if meshes:
        mn, mx = world_bounds(meshes)
        dim = mx - mn
        length = max(dim.x, dim.y)
        height = dim.z
    else:
        length, height = 2.0, 1.5
    if height < 0.05 or length < 0.05:
        length, height = 2.0, 1.5
    print("Animal size: length %.2f, height %.2f" % (length, height), flush=True)

    # ---- choose and loop the animation
    action, all_names = pick_action(opts["action"])
    print("ACTIONS IN THIS MODEL:", all_names, flush=True)
    chosen_name = "none (no animation found)"
    if action is None and all_names:
        action = bpy.data.actions[0]
        print("WARNING: action '%s' not found, using '%s'"
              % (opts["action"], action.name), flush=True)
    if action is not None:
        chosen_name = action.name
        for arm in armatures:
            ad = arm.animation_data or arm.animation_data_create()
            for track in list(ad.nla_tracks):
                ad.nla_tracks.remove(track)
            ad.action = action
        for fc in action.fcurves:
            fc.modifiers.new('CYCLES')
    print("Playing action:", chosen_name, flush=True)

    # ---- root empty that carries the animal across the scene
    bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))
    root = bpy.context.active_object
    root.name = "AnimalRoot"
    for o in new_objs:
        if o.parent is None:
            o.parent = root
    root.rotation_euler = (0, 0, math.radians(opts["rotate_z"]))

    # ---- ground and trees (simple, replace with a nature kit later)
    m_grass = make_material("Grass", (0.25, 0.65, 0.2))
    m_trunk = make_material("Trunk", (0.40, 0.25, 0.12))
    m_leaf = make_material("Leaf", (0.12, 0.50, 0.15))
    bpy.ops.mesh.primitive_plane_add(size=400, location=(0, 0, 0))
    ground = bpy.context.active_object
    ground.data.materials.append(m_grass)

    speed = opts["speed"]
    half = opts["seconds"] * speed / 2.0 + 15.0
    tree_h = max(height, 1.0) * 2.6
    step = max(length, 1.5) * 2.2
    x = -half
    i = 0
    while x <= half:
        y = length * (2.5 if i % 2 == 0 else 4.0)
        add_cylinder("Trunk%d" % i, (x, y, tree_h * 0.22), tree_h * 0.06,
                     tree_h * 0.44, m_trunk)
        add_sphere("Leaf%d" % i, (x, y, tree_h * 0.65),
                   (tree_h * 0.30, tree_h * 0.30, tree_h * 0.36), m_leaf)
        x += step
        i += 1

    # ---- camera follows the animal
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 35
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

    cam_dist = max(length, 1.0) * 3.8
    start_x = -opts["seconds"] * speed / 2.0
    for f in range(1, total + 1):
        t = (f - 1) / float(fps)
        px = start_x + t * speed
        root.location = (px, 0, 0)
        root.keyframe_insert(data_path="location", frame=f)
        target.location = (px, 0, height * 0.5)
        target.keyframe_insert(data_path="location", frame=f)
        cam.location = (px, -cam_dist, height * 0.9)
        cam.keyframe_insert(data_path="location", frame=f)

    info = {"model_file": model_path, "action": chosen_name,
            "all_actions": all_names, "length": length, "height": height}
    return scene, total, info


# ---------------------------------------------------------------- render
def main():
    opts = parse_args()
    out = os.path.abspath(opts["out"])
    os.makedirs(out, exist_ok=True)

    scene, total, info = build_scene(opts)

    if opts["sample_only"]:
        frames = sorted(set(int(1 + i * (total - 1) / 5.0) for i in range(6)))
    else:
        frames = list(range(1, total + 1))

    print("=" * 60)
    print("Engine: %s | %d fps | %.0f s | total video frames: %d"
          % (opts["engine"], opts["fps"], opts["seconds"], total))
    print("Frames to render now: %d" % len(frames))
    print("=" * 60, flush=True)

    times = []
    run_start = time.time()
    for n, f in enumerate(frames):
        scene.frame_set(f)
        prefix = "sample_" if opts["sample_only"] else "frame_"
        scene.render.filepath = os.path.join(out, "%s%04d" % (prefix, f))
        t0 = time.time()
        bpy.ops.render.render(write_still=True)
        dt = time.time() - t0
        times.append(dt)
        print("FRAME %d (%d/%d) took %.1f s" % (f, n + 1, len(frames), dt),
              flush=True)
    run_total = time.time() - run_start

    steady = times[1:] if len(times) > 1 else times
    avg = sum(steady) / len(steady)
    est_min = avg * total / 60.0
    if est_min <= 30:
        verdict = "GOOD: fast enough for a daily video."
    elif est_min <= 90:
        verdict = "OK but heavy: reduce fps, samples or scene detail if possible."
    else:
        verdict = "TOO SLOW: simplify the scene, lower fps, or use fewer frames."

    lines = [
        "## Animal render test result",
        "",
        "| Item | Value |",
        "|---|---|",
        "| Model file | %s |" % os.path.basename(info["model_file"]),
        "| Animation played | %s |" % info["action"],
        "| All animations in this model | %s |" % ", ".join(info["all_actions"]),
        "| Engine | %s |" % opts["engine"],
        "| Video | %.0f s at %d fps = %d frames |"
        % (opts["seconds"], opts["fps"], total),
        "| Frames rendered in this run | %d |" % len(frames),
        "| Average time per frame | %.1f s |" % avg,
        "| First frame (includes warm-up) | %.1f s |" % times[0],
        "| Estimated time for the full video | %.1f minutes |" % est_min,
        "| Actual time of this run | %.1f minutes |" % (run_total / 60.0),
        "",
        "**Verdict:** " + verdict,
    ]
    with open(os.path.join(out, "summary.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "timing.json"), "w") as fh:
        json.dump({"avg_s_per_frame": avg, "est_total_min": est_min,
                   "frames_total": total, "per_frame_s": times,
                   "info": info}, fh)
    print("\n".join(lines), flush=True)


main()
