"""
Asset check v3 - catalogue of EVERY model in the repository and in the downloaded asset packs
(animals, humans, monsters, buildings, trees, cars, food, props ... glTF, GLB and FBX).

* Reads glTF/GLB file headers (fast): animations, skeleton, size.
* FBX files are listed and previewed too (Blender opens them).
* Renders one small picture of EVERY model (up to --max-per-pack per pack). The workflow then
  puts the pictures of each pack on labelled sheets.
* Writes a summary, a full list with sizes (all_models.md) and a catalogue (asset_catalog.json).

Run (headless):
  blender -b --python-exit-code 1 -P asset_check.py -- --assets . --out out
"""
import bpy
import sys
import os
import re
import math
import json
import struct
from mathutils import Vector

ASSET_FACING = 90     # rotation that makes an animal face +X (same as the movie)
TARGET_SIZE = 2.0     # animated models are scaled to this length for the pictures
STATIC_FIT = 4.5      # static models are fitted into a box of about this size
KEYWORDS = ["robot", "car", "truck", "tree", "bush", "grass", "flower", "building", "house",
            "road", "food", "monster", "dragon", "king", "queen", "knight", "tent", "fire", "boat"]


# ---------------------------------------------------------------- arguments
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts = {"assets": ".", "out": "out", "max_per_pack": 120}
    i = 0
    while i < len(argv):
        if argv[i] == "--assets":
            opts["assets"] = argv[i + 1]; i += 2
        elif argv[i] == "--out":
            opts["out"] = argv[i + 1]; i += 2
        elif argv[i] == "--max-per-pack":
            opts["max_per_pack"] = int(argv[i + 1]); i += 2
        else:
            i += 1
    return opts


# ---------------------------------------------------------------- reading model files
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


def gltf_size(j):
    """Width, height, depth of a model from the file header (glTF axes: x, y = up, z)."""
    try:
        mins, maxs, found = [1e9] * 3, [-1e9] * 3, False
        for mesh in j.get("meshes", []):
            for prim in mesh.get("primitives", []):
                idx = prim.get("attributes", {}).get("POSITION")
                if idx is None:
                    continue
                acc = j["accessors"][idx]
                if "min" in acc and "max" in acc:
                    found = True
                    for i in range(3):
                        mins[i] = min(mins[i], acc["min"][i])
                        maxs[i] = max(maxs[i], acc["max"][i])
        return [round(maxs[i] - mins[i], 2) for i in range(3)] if found else None
    except Exception:
        return None


def short(name):
    n = name.split(".")[0]
    return n.replace("_AnimalArmature", "").replace("AnimalArmature", "")


def pack_of(path, assets_dir):
    """Name of the asset pack a file belongs to (first folder under 'assets/')."""
    rel = os.path.relpath(path, assets_dir)
    parts = [p for p in rel.split(os.sep) if p not in (".", "")]
    key = parts[0]
    if key == "assets" and len(parts) > 2:
        key = parts[1]
    key = re.sub(r"(\.zip)+$", "", key, flags=re.I)
    key = re.sub(r"[^A-Za-z0-9-]+", "-", key).strip("-")
    return key or "root"


def find_models(assets_dir):
    by_stem = {}
    for root, dirs, files in os.walk(assets_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "out"]
        for f in files:
            if f.lower().endswith((".glb", ".gltf", ".fbx")):
                by_stem.setdefault(os.path.splitext(f)[0], []).append(os.path.join(root, f))
    models = {}
    for stem, paths in by_stem.items():
        if any(p.lower().endswith((".glb", ".gltf")) for p in paths):
            # packs ship the same model as FBX and glTF: keep only the glTF
            paths = [p for p in paths if not p.lower().endswith(".fbx")]
        paths.sort(key=lambda p: (not p.lower().endswith(".glb"), p.lower().endswith(".fbx"), len(p)))
        uniq, seen = [], set()
        for p in paths:                       # same folder, two formats -> keep one
            d = os.path.dirname(p)
            if d in seen:
                continue
            seen.add(d)
            uniq.append(p)
        if len(uniq) == 1:
            models[stem] = uniq[0]
        else:                                 # same name in different packs -> add the pack name
            for p in uniq:
                models["%s__%s" % (stem, pack_of(p, assets_dir))] = p
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
    scene.eevee.taa_render_samples = 12
    for attr, val in (("use_gtao", False), ("use_ssr", False), ("use_soft_shadows", True)):
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
    bpy.context.active_object.data.materials.append(make_material("Ground", (0.45, 0.72, 0.35)))
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


def import_model(path):
    if path.lower().endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=path)
        try:   # FBX textures often sit in a sibling "Textures" folder
            bpy.ops.file.find_missing_files(directory=os.path.dirname(os.path.dirname(path)))
        except Exception:
            pass
    else:
        bpy.ops.import_scene.gltf(filepath=path)


def load_animal(path):
    """Import an animated model, scale it to TARGET_SIZE, face +X, idle pose. Returns its height."""
    before_objs = set(o.name for o in bpy.data.objects)
    before_acts = set(a.name for a in bpy.data.actions)
    import_model(path)
    new_objs = [o for o in bpy.data.objects if o.name not in before_objs]
    new_acts = [a for a in bpy.data.actions if a.name not in before_acts]
    meshes = [o for o in new_objs if o.type == 'MESH']
    armatures = [o for o in new_objs if o.type == 'ARMATURE']
    bpy.context.view_layer.update()
    if not meshes:
        raise RuntimeError("no mesh found in model")
    mn, mx = world_bounds(meshes)
    dim = mx - mn
    length = max(dim.x, dim.y, dim.z * 0.5)
    if length < 1e-6:
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
    root.location = (0, 0, -mn.z * s)
    return dim.z * s


def load_static(path):
    """Import a static model (building, prop, car ...), fit it into a box, centre it."""
    before = set(o.name for o in bpy.data.objects)
    import_model(path)
    new = [o for o in bpy.data.objects if o.name not in before]
    meshes = [o for o in new if o.type == 'MESH']
    bpy.context.view_layer.update()
    if not meshes:
        raise RuntimeError("no mesh found in model")
    mn, mx = world_bounds(meshes)
    dim = mx - mn
    big = max(dim.x, dim.y, dim.z)
    if big < 1e-6:
        raise RuntimeError("model is empty or has zero size")
    s = STATIC_FIT / big
    bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))
    root = bpy.context.active_object
    for o in new:
        if o.parent is None:
            o.parent = root
    root.scale = (s, s, s)
    root.location = (-(mn.x + mx.x) / 2 * s, -(mn.y + mx.y) / 2 * s, -mn.z * s)


def render_preview(entry, path, out):
    scene = setup_scene(480, 480)
    if entry["category"] == "animated":
        h = load_animal(path)
        make_camera(scene, (0, -7.0, h * 0.6 + 0.4), (0, 0, h * 0.5))
    else:
        load_static(path)
        make_camera(scene, (0, -10, 3.2), (0, 0, 2.2))
    scene.frame_set(1)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", entry["model"])
    scene.render.filepath = os.path.join(out, "pv@@%s@@%s" % (entry["pack"], safe))
    bpy.ops.render.render(write_still=True)


# ---------------------------------------------------------------- main
def main():
    opts = parse_args()
    out = os.path.abspath(opts["out"])
    os.makedirs(out, exist_ok=True)

    models = find_models(opts["assets"])
    if not models:
        raise RuntimeError("No .glb/.gltf/.fbx files found. Upload the asset packs to the Release "
                           "(see the instructions) and run again.")
    print("Found %d models." % len(models), flush=True)

    catalog, errors = [], []
    for name, path in models.items():
        entry = {"model": name, "file": os.path.relpath(path), "pack": pack_of(path, opts["assets"]),
                 "folder": os.path.relpath(os.path.dirname(path)), "animations": [],
                 "skinned": False, "size": None,
                 "format": os.path.splitext(path)[1].lower().lstrip(".")}
        if path.lower().endswith(".fbx"):
            entry["category"] = "static"       # FBX files are listed and previewed, not opened for animations
        else:
            j = read_gltf_json(path)
            if j is None:
                errors.append("%s: could not read the file header" % name)
                continue
            anims = [short(a.get("name", "")) for a in j.get("animations", [])]
            entry["animations"] = sorted(set(anims))
            entry["skinned"] = bool(j.get("skins"))
            entry["size"] = gltf_size(j)
            if anims and entry["skinned"]:
                entry["category"] = "animated"
            elif entry["skinned"]:
                entry["category"] = "rigged"
            else:
                entry["category"] = "static"
        catalog.append(entry)

    packs = {}
    for e in catalog:
        packs.setdefault(e["pack"], []).append(e)
    print("Packs: %s" % ", ".join("%s (%d)" % (k, len(v)) for k, v in sorted(packs.items())), flush=True)

    # ---- one picture per model
    rendered = 0
    for pack, items in sorted(packs.items()):
        for e in items[:opts["max_per_pack"]]:
            try:
                render_preview(e, models[e["model"]], out)
                rendered += 1
            except Exception as ex:
                errors.append("%s / %s: %s" % (pack, e["model"], ex))
                print("FAIL %s: %s" % (e["model"], ex), flush=True)
        print("Pictures done for pack %s" % pack, flush=True)

    # ---- summary
    animated = [e for e in catalog if e["category"] == "animated"]
    rigged = [e for e in catalog if e["category"] == "rigged"]
    static = [e for e in catalog if e["category"] == "static"]
    lines = ["## Asset check - all packs", "",
             "Models found: **%d** (animated: **%d**, rigged without animation: **%d**, static: **%d**). "
             "Pictures made: **%d**." % (len(catalog), len(animated), len(rigged), len(static), rendered), "",
             "### Packs", "", "| Pack | Models | Animated | Rigged | Static | File types |", "|---|---|---|---|---|---|"]
    for pack, items in sorted(packs.items()):
        fmts = ", ".join(sorted(set(e["format"] for e in items)))
        lines.append("| %s | %d | %d | %d | %d | %s |" % (
            pack, len(items), sum(1 for e in items if e["category"] == "animated"),
            sum(1 for e in items if e["category"] == "rigged"),
            sum(1 for e in items if e["category"] == "static"), fmts))

    lines += ["", "### Quick finder (do I have it?)", "",
              "| Word | Models with this word in the name |", "|---|---|"]
    for kw in KEYWORDS:
        hits = [e["model"] for e in catalog if kw in e["model"].lower()]
        shown = ", ".join(hits[:6]) + (" ... (+%d more)" % (len(hits) - 6) if len(hits) > 6 else "")
        lines.append("| %s | %s |" % (kw, shown if hits else "none"))

    lines += ["", "### Animated models (animals, humans, monsters)", "",
              "| # | Model name (use in the scene plan) | Pack | Animations |", "|---|---|---|---|"]
    for i, e in enumerate(animated, 1):
        lines.append("| %d | %s | %s | %d |" % (i, e["model"], e["pack"], len(e["animations"])))
    common = None
    for e in animated:
        s = set(e["animations"])
        common = s if common is None else (common & s)
    lines += ["", "**Animations every animated model has:** "
              + (", ".join(sorted(common)) if common else "(none in common)"), "",
              "### Animations per model", ""]
    for e in animated:
        lines.append("- **%s**: %s" % (e["model"], ", ".join(e["animations"])))

    def group(items, title):
        res = ["", title, ""]
        by_pack = {}
        for e in items:
            by_pack.setdefault(e["pack"], []).append(e["model"])
        for pack, names in sorted(by_pack.items()):
            shown = ", ".join(names[:50]) + (" ... (+%d more, see all_models.md)" % (len(names) - 50) if len(names) > 50 else "")
            res.append("- **%s** (%d): %s" % (pack, len(names), shown))
        return res

    lines += group(rigged, "### Rigged models without animation (bodies, outfits, monsters)")
    lines += group(static, "### Static models (buildings, trees, cars, food, props)")
    if errors:
        lines += ["", "### Problems", ""] + ["- " + x for x in errors]
    lines.append("")

    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "asset_catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)
    with open(os.path.join(out, "all_models.md"), "w", encoding="utf-8") as fh:
        fh.write("| Pack | Model | Type | Width | Height | Depth | Animations |\n|---|---|---|---|---|---|---|\n")
        for e in sorted(catalog, key=lambda x: (x["pack"], x["model"])):
            sz = e.get("size") or ["?", "?", "?"]
            fh.write("| %s | %s | %s | %s | %s | %s | %d |\n"
                     % (e["pack"], e["model"], e["category"], sz[0], sz[1], sz[2], len(e["animations"])))
    print("\n".join(lines), flush=True)


main()
