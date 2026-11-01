"""
Blender render-speed test for the kids story Shorts project.

Run (headless):
  blender -b --python-exit-code 1 -P render_test.py -- --seconds 30 --fps 12 --engine EEVEE --sample-only --out out

Options:
  --seconds N      video length in seconds (default 30)
  --fps N          frames per second (default 12)
  --engine NAME    EEVEE or CYCLES (default EEVEE)
  --sample-only    render only 6 spread-out frames, then estimate the full time
  --out DIR        output folder (default out)
"""
import bpy
import sys
import os
import time
import math
import json


# ---------------------------------------------------------------- arguments
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts = {"seconds": 30.0, "fps": 12, "engine": "EEVEE",
            "sample_only": False, "out": "out"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--seconds":
            opts["seconds"] = float(argv[i + 1]); i += 2
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
def make_material(name, color, roughness=0.7):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return m


def add_sphere(name, loc, scale, mat, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16,
                                         radius=1.0, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = scale
    bpy.ops.object.shade_smooth()
    o.data.materials.append(mat)
    if parent is not None:
        o.parent = parent  # location now means "relative to parent"
    return o


def add_cylinder(name, loc, radius, depth, mat):
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=radius,
                                        depth=depth, location=loc)
    o = bpy.context.active_object
    o.name = name
    bpy.ops.object.shade_smooth()
    o.data.materials.append(mat)
    return o


def add_empty(name, loc, parent=None):
    bpy.ops.object.empty_add(type='PLAIN_AXES', location=loc)
    o = bpy.context.active_object
    o.name = name
    if parent is not None:
        o.parent = parent
    return o


# ---------------------------------------------------------------- scene
def build_scene(opts):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    fps = opts["fps"]
    total = int(round(opts["seconds"] * fps))
    scene.frame_start = 1
    scene.frame_end = total
    scene.render.fps = fps
    scene.render.resolution_x = 720
    scene.render.resolution_y = 1280
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.view_settings.view_transform = 'Standard'

    # engine
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

    # sky
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.55, 0.80, 1.0, 1.0)
    bg.inputs[1].default_value = 1.0
    scene.world = world

    # sun
    sun_data = bpy.data.lights.new("Sun", 'SUN')
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), math.radians(10), math.radians(30))

    # materials
    m_grass = make_material("Grass", (0.25, 0.65, 0.2))
    m_trunk = make_material("Trunk", (0.40, 0.25, 0.12))
    m_leaf = make_material("Leaf", (0.12, 0.50, 0.15))
    m_fur = make_material("Fur", (0.95, 0.93, 0.90))
    m_pink = make_material("Pink", (1.0, 0.65, 0.72))
    m_black = make_material("Black", (0.02, 0.02, 0.02), 0.3)

    # ground
    bpy.ops.mesh.primitive_plane_add(size=120, location=(0, 0, 0))
    ground = bpy.context.active_object
    ground.data.materials.append(m_grass)

    # trees along the path
    for i, x in enumerate(range(-40, 41, 4)):
        y = 3.0 if i % 2 == 0 else 5.5
        add_cylinder("Trunk%d" % i, (x, y, 0.6), 0.22, 1.2, m_trunk)
        add_sphere("Leaf%d" % i, (x, y, 2.2), (1.1, 1.1, 1.3), m_leaf)

    # character: a rabbit (everything faces +X)
    root = add_empty("Rabbit", (0, 0, 0))
    add_sphere("Body", (0, 0, 0.9), (0.55, 0.45, 0.65), m_fur, root)
    add_sphere("Head", (0.15, 0, 1.8), (0.45, 0.40, 0.40), m_fur, root)
    add_sphere("EarL", (0.05, 0.15, 2.5), (0.10, 0.08, 0.40), m_pink, root)
    add_sphere("EarR", (0.05, -0.15, 2.5), (0.10, 0.08, 0.40), m_pink, root)
    add_sphere("EyeL", (0.52, 0.17, 1.9), (0.07, 0.07, 0.07), m_black, root)
    add_sphere("EyeR", (0.52, -0.17, 1.9), (0.07, 0.07, 0.07), m_black, root)
    add_sphere("Nose", (0.58, 0, 1.75), (0.06, 0.06, 0.06), m_pink, root)
    add_sphere("Tail", (-0.5, 0, 0.8), (0.18, 0.18, 0.18), m_fur, root)

    armL = add_empty("ArmL", (0.05, 0.50, 1.3), root)
    armR = add_empty("ArmR", (0.05, -0.50, 1.3), root)
    add_sphere("ArmLMesh", (0, 0, -0.35), (0.12, 0.12, 0.38), m_fur, armL)
    add_sphere("ArmRMesh", (0, 0, -0.35), (0.12, 0.12, 0.38), m_fur, armR)

    legL = add_empty("LegL", (0, 0.22, 0.45), root)
    legR = add_empty("LegR", (0, -0.22, 0.45), root)
    add_sphere("LegLMesh", (0.05, 0, -0.20), (0.20, 0.14, 0.27), m_fur, legL)
    add_sphere("LegRMesh", (0.05, 0, -0.20), (0.20, 0.14, 0.27), m_fur, legR)

    # camera
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 35
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.rotation_euler = (math.radians(87), 0, 0)

    # animation: walk, bob, swing arms and legs, camera follows
    speed = 1.0  # units per second
    start_x = -opts["seconds"] * speed / 2.0
    for f in range(1, total + 1):
        t = (f - 1) / float(fps)
        phase = t * 2.0 * math.pi * 1.0
        x = start_x + t * speed

        root.location = (x, 0, abs(math.sin(phase)) * 0.12)
        root.keyframe_insert(data_path="location", frame=f)

        swing = math.sin(phase) * 0.6
        armL.rotation_euler = (0, swing, 0)
        armR.rotation_euler = (0, -swing, 0)
        legL.rotation_euler = (0, -swing, 0)
        legR.rotation_euler = (0, swing, 0)
        for o in (armL, armR, legL, legR):
            o.keyframe_insert(data_path="rotation_euler", frame=f)

        cam.location = (x, -8.0, 2.2)
        cam.keyframe_insert(data_path="location", frame=f)

    return scene, total


# ---------------------------------------------------------------- render
def main():
    opts = parse_args()
    out = os.path.abspath(opts["out"])
    os.makedirs(out, exist_ok=True)

    scene, total = build_scene(opts)

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
        "## Blender render test result",
        "",
        "| Item | Value |",
        "|---|---|",
        "| Engine | %s |" % opts["engine"],
        "| Resolution | 720 x 1280 |",
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
                   "frames_total": total, "per_frame_s": times}, fh)

    print("\n".join(lines), flush=True)


main()
