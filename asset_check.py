"""
Asset check (v2) - catalogue of ALL glTF/GLB models in the repository and in
downloaded asset packs.

* Reads each model's file header (fast, no Blender import) to find out whether it
  is animated, how many animations it has and what they are called.
* Animated models: imported in Blender, scaled to the same length, rendered as a
  thumbnail, and shown together in lineup pictures (12 per picture).
* Static models (trees, rocks, houses...): listed only.

Run (headless):
  blender -b --python-exit-code 1 -P asset_check.py -- --assets . --out out

Outputs in --out:
  summary.md            readable list of everything found
  asset_catalog.json    same information for later steps
  thumb_<Name>.png      one picture per animated model
  lineup_01.png ...     animated models in groups of 12
"""
import bpy
import sys
import os
import math
import json
import struct
from mathutils import Vector

ASSET_FACING = 90   # rotation that makes the animal face +X (same as the movie test)
TARGET_SIZE = 2.0   # animated models are scaled to this length for the pictures
LINEUP_SIZE = 12    # models per lineup picture


# ---------------------------------------------------------------- arguments
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts = {"assets": ".", "out": "out"}
    i = 0
    while i < len(argv):
        if argv[i] == "--assets":
            opts["assets"] = argv[i + 1]; i += 2
        elif argv[i] == "--out":
            opts["out"] = argv[i + 1]; i += 2
        else:
            i += 1
    return opts


# ---------------------------------------------------------------- finding models
def read_gltf_json(path):
    """Read the JSON part of a .gltf or .glb file without importing it."""
    try:
        if path.lower().endswith(".gltf"):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        with open(path, "rb") as fh:
            head = fh.read(12)
            if head[:4] != b"glTF":
                return None
            clen, _ctype = struct.unpack("<II", fh.read(8))
            return json.loads(fh.read(clen).decode("utf-8"))
    except Exception:
        return None


def short(name):
    n = name.split(".")[0]
    return n.replace("_AnimalArmature", "").replace("AnimalArmature", "")


def find_models(assets_dir):
    by_stem = {}
    for root, dirs, files in os.walk(assets_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "out"]
        for f in files:
            if f.lower().endswith((".glb", ".gltf")):
                by_stem.setdefault(os.path.splitext(f)[0], []).append(
                    os.path.join(root, f))
    models = {}
    for stem, paths in by_stem.items():
        paths.sort(key=lambda p: (not p.lower().endswith(".glb"), len(p)))
        uniq, seen = [], set()
        for p in paths:               # same folder, .glb + .gltf -> keep one
            d = os.path.dirname(p)
            if d in seen:
                continue
            seen.add(d)
            uniq.append(p)
        if len(uniq) == 1:
            models[stem] = uniq[0]
        else:                         # same name in different packs -> add folder name
            for p in uniq:
                rel = os.path.relpath(os.path.dirname(p), assets_dir)
                tag = rel.replace(os.sep, "_").strip("._") or "root"
                models["%s__%s" % (stem, tag)] = p
    return dict(sorted(models.items(), key=lambda kv: kv[0].lower()))


# ---------------------------------------------------------------- Blender helpers
def make_material(name, color, roughness=0.9):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes.get("Principled BSDF")
    b.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
    b.inputs["Roughness"].default_value = roughness
    return m


def setup_scene(width, height):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
    except Exception:
        pass
    scene = bpy.context.scene
    scene.render.fps = 12
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.view_settings.view_transform = 'Standard'
    scene.render.engine = 'BLENDER_EEVEE'
    scene.eevee.taa_render_samples = 16
    for attr, val in (("use_gtao", False), ("use_ssr", False),
                      ("use_soft_shadows", True)):
        try:
            setattr(scene.eevee, attr, val)
        except Exception:
            pass

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.70, 0.84, 1.0, 1.0)
    bg.inputs[1].default_value = 1.0
    scene.world = world

    sun_data = bpy.data.lights.new("Sun", 'SUN')
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), math.radians(10), math.radians(30))

    bpy.ops.mesh.primitive_plane_add(size=300, location=(0, 0, 0))
    bpy.context.active_object.data.materials.append(
        make_material("Ground", (0.45, 0.72, 0.35)))
    return scene


def make_camera(scene, loc, look, lens=35):
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = lens
    cam_data.clip_end = 1000
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.location = loc
    bpy.ops.object.empty_add(type='PLAIN_AXES', location=look)
    target = bpy.context.active_object
    con = cam.constraints.new('TRACK_TO')
    con.target = target
    con.track_axis = 'TRACK_NEGATIVE_Z'
    con.up_axis = 'UP_Y'


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


def idle_action(acts):
    hits = [a for a in acts if a.name.lower().startswith("idle")]
    hits.sort(key=lambda a: len(a.name))
    if hits:
        return hits[0]
    return acts[0] if acts else None


def load_animal(path, loc):
    """Import one animated model, scale it, face +X, set an idle pose."""
    before_objs = set(o.name for o in bpy.data.objects)
    before_acts = set(a.name for a in bpy.data.actions)
    bpy.ops.import_scene.gltf(filepath=path)
    new_objs = [o for o in bpy.data.objects if o.name not in before_objs]
    new_acts = [a for a in bpy.data.actions if a.name not in before_acts]
    meshes = [o for o in new_objs if o.type == 'MESH']
    armatures = [o for o in new_objs if o.type == 'ARMATURE']
    bpy.context.view_layer.update()

    if not meshes:
        raise RuntimeError("no mesh found in model")
    mn, mx = world_bounds(meshes)
    dim = mx - mn
    length = max(dim.x, dim.y)
    if length < 0.05:
        raise RuntimeError("model is empty or has zero size")
    s = TARGET_SIZE / length

    action = idle_action(new_acts)
    if action is not None:
        for arm in armatures:
            ad = arm.animation_data or arm.animation_data_create()
            for track in list(ad.nla_tracks):
                ad.nla_tracks.remove(track)
            ad.action = action

    bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))
    root = bpy.context.active_object
    for o in new_objs:
        if o.parent is None:
            o.parent = root
    root.scale = (s, s, s)
    root.rotation_euler = (0, 0, math.radians(ASSET_FACING))
    root.location = (loc[0], loc[1], loc[2] - mn.z * s)
    return {"length": length, "height": dim.z, "height_scaled": dim.z * s}


# ---------------------------------------------------------------- main
def main():
    opts = parse_args()
    out = os.path.abspath(opts["out"])
    os.makedirs(out, exist_ok=True)

    models = find_models(opts["assets"])
    if not models:
        raise RuntimeError("No .glb/.gltf files found. Upload the glTF folders of the "
                           "asset packs (see the instructions) and run again.")
    print("Found %d model files." % len(models), flush=True)

    catalog, animated, rigged, static, errors = [], [], [], [], []
    for name, path in models.items():
        j = read_gltf_json(path)
        if j is None:
            errors.append("%s: could not read the file header" % name)
            continue
        anims = [short(a.get("name", "")) for a in j.get("animations", [])]
        entry = {"model": name, "file": os.path.relpath(path),
                 "folder": os.path.relpath(os.path.dirname(path)),
                 "animations": sorted(set(anims)),
                 "meshes": len(j.get("meshes", [])),
                 "skinned": bool(j.get("skins"))}
        if anims and entry["skinned"]:
            entry["category"] = "animated"
            animated.append(entry)
        elif entry["skinned"]:
            entry["category"] = "rigged"      # has a skeleton but no animation inside
            rigged.append(entry)
        else:
            entry["category"] = "static"
            static.append(entry)
        catalog.append(entry)
    print("Animated: %d | Rigged (no animation): %d | Static: %d"
          % (len(animated), len(rigged), len(static)), flush=True)

    # ---- thumbnails of animated models
    good = []
    for e in animated:
        try:
            scene = setup_scene(640, 640)
            info = load_animal(models[e["model"]], (0, 0, 0))
            h = info["height_scaled"]
            make_camera(scene, (0, -7.0, h * 0.6 + 0.4), (0, 0, h * 0.5))
            scene.frame_set(1)
            scene.render.filepath = os.path.join(
                out, "thumb_%s" % e["model"].replace(" ", "_"))
            bpy.ops.render.render(write_still=True)
            e["length_raw"] = round(info["length"], 3)
            e["height_raw"] = round(info["height"], 3)
            good.append(e)
            print("OK   %s | %d animations" % (e["model"], len(e["animations"])), flush=True)
        except Exception as ex:
            errors.append("%s: %s" % (e["model"], ex))
            print("FAIL %s: %s" % (e["model"], ex), flush=True)

    # ---- lineup pictures, 12 animated models each
    for part in range(0, len(good), LINEUP_SIZE):
        group = good[part:part + LINEUP_SIZE]
        try:
            scene = setup_scene(1920, 1080)
            for i, e in enumerate(group):
                load_animal(models[e["model"]], ((i % 6 - 2.5) * 3.4, (i // 6) * 6.0, 0))
            make_camera(scene, (0, -24, 7.5), (0, 3, 1.2))
            scene.frame_set(1)
            scene.render.filepath = os.path.join(out, "lineup_%02d" % (part // LINEUP_SIZE + 1))
            bpy.ops.render.render(write_still=True)
            print("Lineup %d done" % (part // LINEUP_SIZE + 1), flush=True)
        except Exception as ex:
            errors.append("lineup %d: %s" % (part // LINEUP_SIZE + 1, ex))

    # ---- summary
    lines = ["## Asset check", "",
             "Model files found: **%d** (animated: **%d**, rigged parts without animation: **%d**, static: **%d**)"
             % (len(catalog), len(animated), len(rigged), len(static)), ""]

    lines += ["### Animated models (animals and characters)", "",
              "| # | Model name (use in scene_plan.json) | Folder | Animations |",
              "|---|---|---|---|"]
    for i, e in enumerate(animated, 1):
        lines.append("| %d | %s | %s | %d |" % (i, e["model"], e["folder"], len(e["animations"])))

    common = None
    for e in animated:
        s = set(e["animations"])
        common = s if common is None else (common & s)
    lines += ["", "**Animations that every animated model has:** "
              + (", ".join(sorted(common)) if common else "(none in common)"), ""]

    lines += ["### Animations per model", ""]
    for e in animated:
        lines.append("- **%s**: %s" % (e["model"], ", ".join(e["animations"])))

    def group_lines(items):
        by_folder = {}
        for e in items:
            by_folder.setdefault(e["folder"], []).append(e["model"])
        out_lines = []
        for folder, names in sorted(by_folder.items()):
            shown = ", ".join(names[:40]) + (" ... (+%d more)" % (len(names) - 40) if len(names) > 40 else "")
            out_lines.append("- **%s** (%d models): %s" % (folder, len(names), shown))
        return out_lines

    lines += ["", "### Rigged models without animation (base bodies, outfits, character parts)", "",
              "These need an animation library (for example Universal Animation Library) to move.", ""]
    lines += group_lines(rigged)
    lines += ["", "### Static models (trees, rocks, houses, props, food)", ""]
    lines += group_lines(static)

    if errors:
        lines += ["", "### Problems", ""] + ["- " + x for x in errors]
    lines.append("")

    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "asset_catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)
    print("\n".join(lines), flush=True)


main()
