#!/usr/bin/env python3
"""Render Story Brain v2 scene_plan_timed.json with Blender 3.6 in headless mode.

Expected input: the Story Brain schema:
  plan['shots'][i]['actors'] = [{model, action, start, end, height|size, heading, tint}]
  shot fields: id, title, env, duration/seconds, camera, card, prop

Example (GitHub Actions):
  blender -b -P movie_render_v2.py -- --plan scene_plan_timed.json \
      --assets assets --out out --fps 12 --quality balanced --shard 0 --shards 8

Frames are numbered locally inside each shard as frame_00000.png, frame_00001.png, ...
so FFmpeg can encode every shard with the same image-sequence command.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from typing import Any

import bpy
from mathutils import Vector

PREF = {".glb": 0, ".gltf": 1, ".fbx": 2}
SAMPLES = {"fast": 4, "balanced": 12, "high": 28}
FPS_DEFAULT = 12
MODEL_FORWARD_OFFSET = 90.0  # Story Brain v2 writes asset_facing=90.

INDEX: dict[str, str] = {}
MISSING: set[str] = set()
ANIM_PROBLEMS: set[str] = set()
ANIM_USED: dict[str, str] = {}

# Environment aliases from Story Brain v2 to rendered environment settings.
ENVIRONMENTS = {
    "meadow_day": "meadow",
    "town_day": "city",
    "forest_day": "forest",
    "forest_sunset": "sunset",
    "winter_night": "winter",
    "fire_red": "fire",
    # Backward-compatible aliases.
    "village": "meadow",
    "forest": "forest",
    "storm": "fire",
    "city": "city",
}

ENV_STYLE = {
    "meadow": {"ground": (0.24, 0.43, 0.15), "sky": (0.48, 0.70, 0.91), "sun": 3.0, "sun_col": (1.0, 0.91, 0.76)},
    "forest": {"ground": (0.13, 0.31, 0.11), "sky": (0.30, 0.52, 0.67), "sun": 2.4, "sun_col": (0.78, 0.90, 1.0)},
    "sunset": {"ground": (0.25, 0.24, 0.12), "sky": (0.72, 0.30, 0.16), "sun": 2.0, "sun_col": (1.0, 0.48, 0.20)},
    "winter": {"ground": (0.48, 0.57, 0.68), "sky": (0.035, 0.075, 0.17), "sun": 0.65, "sun_col": (0.42, 0.58, 1.0)},
    "fire": {"ground": (0.16, 0.055, 0.035), "sky": (0.24, 0.025, 0.018), "sun": 1.0, "sun_col": (1.0, 0.20, 0.06)},
    "city": {"ground": (0.34, 0.35, 0.38), "sky": (0.48, 0.64, 0.81), "sun": 3.2, "sun_col": (1.0, 0.95, 0.84)},
}


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description="Render one shard of a Story Brain v2 episode")
    parser.add_argument("--plan", required=True, help="scene_plan_timed.json")
    parser.add_argument("--assets", default="assets")
    parser.add_argument("--out", default="out")
    parser.add_argument("--fps", type=int, default=FPS_DEFAULT)
    parser.add_argument("--quality", choices=sorted(SAMPLES), default="balanced")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--sample-only", action="store_true")
    return parser.parse_args(argv)


def shot_duration(shot: dict[str, Any]) -> float:
    for key in ("duration", "seconds", "shot_seconds", "shot_sec"):
        try:
            value = float(shot.get(key))
            if math.isfinite(value) and value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return 3.0


def build_index(assets: str) -> None:
    for root, _dirs, files in os.walk(assets):
        for filename in files:
            stem, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext not in PREF:
                continue
            key = stem.lower()
            existing = INDEX.get(key)
            if existing is None or PREF[ext] < PREF[os.path.splitext(existing)[1].lower()]:
                INDEX[key] = os.path.join(root, filename)
    if not INDEX:
        raise SystemExit(f"No .glb, .gltf or .fbx models found under '{assets}'. Check the asset Release download.")


def link_object(obj: bpy.types.Object) -> None:
    bpy.context.scene.collection.objects.link(obj)


def load_model(name: str):
    path = INDEX.get(str(name).lower())
    if not path:
        MISSING.add(str(name))
        return None
    before_objects = set(bpy.data.objects.keys())
    before_actions = set(bpy.data.actions.keys())
    try:
        if path.lower().endswith(".fbx"):
            bpy.ops.import_scene.fbx(filepath=path)
        else:
            bpy.ops.import_scene.gltf(filepath=path)
    except Exception as exc:
        MISSING.add(f"{name} (import failed: {exc})")
        return None

    new_objects = [obj for obj in bpy.data.objects if obj.name not in before_objects]
    actions = [action for action in bpy.data.actions if action.name not in before_actions]
    if not new_objects:
        MISSING.add(f"{name} (import returned no objects)")
        return None

    wrapper = bpy.data.objects.new("WRAP_" + str(name), None)
    link_object(wrapper)
    new_set = set(new_objects)
    for obj in new_objects:
        if obj.parent is None or obj.parent not in new_set:
            obj.parent = wrapper
        if obj.name not in bpy.context.scene.collection.all_objects:
            link_object(obj)
    return wrapper, new_objects, actions


def normalize_animation(name: str) -> str:
    value = str(name).split("|")[-1]
    value = re.sub(r"\.\d+$", "", value)
    value = re.sub(r"_?(animal|character)?armature$", "", value, flags=re.I)
    return value.lower()


def add_cycles(action) -> None:
    try:
        for fcurve in action.fcurves:
            if not any(mod.type == "CYCLES" for mod in fcurve.modifiers):
                fcurve.modifiers.new("CYCLES")
    except Exception:
        # Some Blender action types do not expose editable F-curves.
        pass


def apply_animation(objects, actions, wanted: str, model: str):
    armatures = [obj for obj in objects if obj.type == "ARMATURE"]
    if not armatures or not actions:
        ANIM_PROBLEMS.add(f"{model}: no armature animation data found")
        return None
    wanted_key = normalize_animation(wanted)
    chosen = next((act for act in actions if normalize_animation(act.name) == wanted_key), None)
    idle = next((act for act in actions if normalize_animation(act.name) in ("idle_neutral", "idle")), None)
    if chosen is None:
        chosen = idle or actions[0]
        ANIM_PROBLEMS.add(f"{model}: '{wanted}' not found; used '{chosen.name}'")
    ANIM_USED[f"{model}:{wanted}"] = chosen.name
    for arm in armatures:
        data = arm.animation_data_create()
        for track in list(data.nla_tracks):
            data.nla_tracks.remove(track)
        data.action = chosen
    add_cycles(chosen)
    if idle:
        add_cycles(idle)
    return {"armatures": armatures, "move": chosen, "idle": idle}


def make_material(name: str, color: tuple[float, float, float], roughness: float = 0.9):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1.0)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*color, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
    return mat


def tint_model(objects, tint) -> None:
    if not isinstance(tint, (list, tuple)) or len(tint) < 3:
        return
    color = tuple(float(max(0.0, min(2.5, x))) for x in tint[:3])
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material:
                mat = slot.material
                mat.diffuse_color = (*color, 1.0)
                if mat.use_nodes and mat.node_tree and mat.node_tree.nodes.get("Principled BSDF"):
                    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*color, 1.0)


def actor_scale(spec: dict[str, Any]) -> float:
    try:
        if spec.get("height") is not None:
            return max(0.55, min(1.8, float(spec["height"]) / 1.8))
        if spec.get("size") is not None:
            return max(0.55, min(1.8, float(spec["size"]) / 2.0))
        return float(spec.get("scale", 1.0))
    except (TypeError, ValueError):
        return 1.0


def v2(value, fallback=(0.0, 0.0)) -> tuple[float, float, float]:
    if isinstance(value, (list, tuple)):
        x = float(value[0]) if len(value) > 0 else fallback[0]
        y = float(value[1]) if len(value) > 1 else fallback[1]
        z = float(value[2]) if len(value) > 2 else 0.0
        return x, y, z
    return float(fallback[0]), float(fallback[1]), 0.0


class Actor:
    def __init__(self, spec: dict[str, Any], asset_facing: float):
        self.spec = spec
        result = load_model(spec.get("model", ""))
        self.ok = result is not None
        if not self.ok:
            return
        self.wrapper, objects, actions = result
        self.objects = objects
        self.animations = apply_animation(objects, actions, str(spec.get("action", "Idle")), str(spec.get("model", "?")))
        self.start = v2(spec.get("start", [0, 0]))
        self.end = v2(spec.get("end", spec.get("start", [0, 0])))
        self.heading = float(spec.get("heading", 0.0))
        self.asset_facing = asset_facing
        self.scale = actor_scale(spec)
        self.is_moving = math.dist(self.start[:2], self.end[:2]) > 0.05
        self.wrapper.scale = (self.scale, self.scale, self.scale)
        tint_model(objects, spec.get("tint"))

    def update(self, progress: float) -> None:
        if not self.ok:
            return
        p = max(0.0, min(1.0, progress))
        x = self.start[0] + (self.end[0] - self.start[0]) * p
        y = self.start[1] + (self.end[1] - self.start[1]) * p
        z = self.start[2] + (self.end[2] - self.start[2]) * p
        self.wrapper.location = (x, y, z)
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        heading = self.heading
        if self.is_moving and (abs(dx) + abs(dy)) > 0.01:
            heading = math.degrees(math.atan2(dx, -dy))
        self.wrapper.rotation_euler.z = math.radians(heading + self.asset_facing)


def build_environment(env_name: str, seed: int) -> None:
    style_name = ENVIRONMENTS.get(env_name, "meadow")
    style = ENV_STYLE[style_name]
    scene = bpy.context.scene

    world = bpy.data.worlds.new("World_" + style_name)
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background:
        background.inputs[0].default_value = (*style["sky"], 1.0)
        background.inputs[1].default_value = 0.8
    scene.world = world

    bpy.ops.mesh.primitive_plane_add(size=120, location=(0, 0, -0.04))
    ground = bpy.context.object
    ground.name = "Ground"
    ground.data.materials.append(make_material("Ground_" + style_name, style["ground"]))

    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = style["sun"]
    sun_data.color = style["sun_col"]
    sun_data.angle = math.radians(10)
    sun = bpy.data.objects.new("Sun", sun_data)
    link_object(sun)
    sun.rotation_euler = (math.radians(45), math.radians(5), math.radians(35))

    # A soft fill light keeps faces readable in dark scenes.
    fill_data = bpy.data.lights.new("Fill", "AREA")
    fill_data.energy = 350 if style_name in ("winter", "fire") else 180
    fill_data.shape = "DISK"
    fill_data.size = 8
    fill = bpy.data.objects.new("Fill", fill_data)
    link_object(fill)
    fill.location = (-3, -4, 7)

    if style_name == "city":
        bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 7.5, 0.005))
        road = bpy.context.object
        road.name = "Road"
        road.scale = (120, 6, 1)
        road.data.materials.append(make_material("Road_Material", (0.075, 0.08, 0.09)))
        for i, model in enumerate(("Building_Large_2", "Building_Medium_2_001", "Building_Small_1")):
            result = load_model(model)
            if result:
                result[0].location = (-9 + i * 9, 14, 0)
                result[0].rotation_euler.z = math.radians(180)
        for i, model in enumerate(("NormalCar1", "Taxi", "SUV")):
            result = load_model(model)
            if result:
                result[0].location = (-7 + i * 7, 7.5, 0.02)
                result[0].rotation_euler.z = math.radians(90)
    else:
        # Background trees: import only models available in this particular asset pack.
        tree_names = ("Pine_1", "Pine_2", "CommonTree_1", "CommonTree_2", "CommonTree_3")
        rng = random.Random(seed * 7919)
        wanted = 8 if style_name in ("meadow", "sunset") else 12
        for i in range(wanted):
            name = tree_names[i % len(tree_names)]
            result = load_model(name)
            if not result:
                continue
            obj = result[0]
            angle = rng.uniform(0, math.tau)
            radius = rng.uniform(9, 20)
            obj.location = (radius * math.cos(angle), max(4.0, radius * math.sin(angle)), 0)
            obj.rotation_euler.z = rng.uniform(0, math.tau)
            scale = rng.uniform(0.8, 1.4)
            obj.scale = (scale, scale, scale)


def add_coin_prop(pos) -> list[bpy.types.Object]:
    x, y, z = v2(pos)
    gold = make_material("Gold_Coin", (0.95, 0.58, 0.06), 0.35)
    objects = []
    for stack in range(3):
        for layer in range(3 + stack):
            bpy.ops.mesh.primitive_cylinder_add(
                vertices=32, radius=0.16, depth=0.055,
                location=(x + (stack - 1) * 0.42, y, z + 0.04 + layer * 0.065),
            )
            coin = bpy.context.object
            coin.name = "GoldCoin"
            coin.data.materials.append(gold)
            objects.append(coin)
    return objects


def add_growing_tree_prop(pos) -> dict[str, Any]:
    x, y, z = v2(pos)
    result = load_model("CommonTree_1") or load_model("CommonTree_2")
    if result:
        wrapper = result[0]
        wrapper.location = (x, y, z)
        wrapper.scale = (0.15, 0.15, 0.15)
        return {"object": wrapper, "start": 0.15, "end": 1.25}
    # Procedural fallback if the nature pack does not contain a tree.
    trunk_mat = make_material("Tree_Trunk", (0.25, 0.12, 0.045))
    leaf_mat = make_material("Tree_Leaves", (0.08, 0.35, 0.08))
    bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=0.12, depth=1.2, location=(x, y, z + 0.6))
    trunk = bpy.context.object
    trunk.data.materials.append(trunk_mat)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.65, location=(x, y, z + 1.4))
    leaves = bpy.context.object
    leaves.data.materials.append(leaf_mat)
    return {"objects": [trunk, leaves], "start": 0.15, "end": 1.0}


def add_shot_props(shot: dict[str, Any]):
    prop = shot.get("prop")
    if not isinstance(prop, dict):
        return []
    kind = prop.get("type")
    pos = prop.get("pos", [0, 1])
    if kind == "coins":
        return [{"objects": add_coin_prop(pos), "start": 0.85, "end": 1.1}]
    if kind == "growing_tree":
        return [add_growing_tree_prop(pos)]
    return []


def update_props(props, progress: float) -> None:
    p = max(0.0, min(1.0, progress))
    for item in props:
        scale = item["start"] + (item["end"] - item["start"]) * (p * p * (3 - 2 * p))
        if "object" in item:
            obj = item["object"]
            obj.scale = (scale, scale, scale)
        else:
            for obj in item.get("objects", []):
                # Coin stacks gently grow during the saving example.
                obj.scale = (scale, scale, scale)


def add_card_overlay(camera: bpy.types.Object, text: str):
    if not text:
        return []
    objects = []
    # Camera-local position; this keeps the card readable regardless of camera movement.
    panel_mat = make_material("Card_Panel", (0.025, 0.035, 0.055), 1.0)
    text_mat = make_material("Card_Text", (1.0, 0.88, 0.34), 0.6)
    try:
        panel_mat.node_tree.nodes["Principled BSDF"].inputs["Emission Color"].default_value = (0.015, 0.02, 0.035, 1)
        panel_mat.node_tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 0.5
        text_mat.node_tree.nodes["Principled BSDF"].inputs["Emission Color"].default_value = (1.0, 0.78, 0.25, 1)
        text_mat.node_tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 1.2
    except Exception:
        pass

    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
    panel = bpy.context.object
    panel.name = "CardPanel"
    panel.parent = camera
    panel.location = (0, 1.75, -6.0)
    panel.rotation_euler = (0, 0, 0)
    panel.scale = (4.8, 0.62, 1)
    panel.data.materials.append(panel_mat)
    objects.append(panel)

    curve = bpy.data.curves.new("CardTextCurve", type="FONT")
    curve.body = str(text)[:100]
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.size = 0.27 if len(str(text)) > 28 else 0.32
    curve.extrude = 0.0
    txt = bpy.data.objects.new("CardText", curve)
    bpy.context.scene.collection.objects.link(txt)
    txt.parent = camera
    txt.location = (0, 1.75, -5.96)
    txt.rotation_euler = (0, 0, 0)
    txt.data.materials.append(text_mat)
    objects.append(txt)
    return objects


def setup_render(plan: dict[str, Any], fps: int, quality: str):
    scene = bpy.context.scene
    render = scene.render
    render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = SAMPLES[quality]
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.use_denoising = True
    try:
        scene.cycles.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass
    scene.cycles.max_bounces = 4
    scene.cycles.diffuse_bounces = 2
    scene.cycles.glossy_bounces = 2
    scene.cycles.transmission_bounces = 2
    scene.cycles.caustics_reflective = False
    scene.cycles.caustics_refractive = False
    render.resolution_x = int(plan.get("width", 720))
    render.resolution_y = int(plan.get("height", 1280))
    render.resolution_percentage = 100
    render.fps = 24  # Imported GLB actions are commonly authored at 24 FPS.
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.compression = 5
    render.threads_mode = "AUTO"
    try:
        scene.view_settings.view_transform = "AgX"
    except Exception:
        pass
    return scene


def camera_target(camera_spec: dict[str, Any], actors: list[Actor], progress: float) -> Vector:
    look = camera_spec.get("look_at", "all")
    if isinstance(look, (list, tuple)) and len(look) >= 3:
        return Vector((float(look[0]), float(look[1]), float(look[2])))
    valid = [a for a in actors if a.ok]
    if isinstance(look, str) and look != "all":
        named = next((a for a in valid if str(a.spec.get("name", "")).lower() == look.lower()), None)
        if named:
            return Vector((named.start[0] + (named.end[0] - named.start[0]) * progress,
                           named.start[1] + (named.end[1] - named.start[1]) * progress, 1.1))
    if not valid:
        return Vector((0, 0, 1.0))
    points = []
    for actor in valid:
        x = actor.start[0] + (actor.end[0] - actor.start[0]) * progress
        y = actor.start[1] + (actor.end[1] - actor.start[1]) * progress
        points.append(Vector((x, y, 1.0)))
    total = Vector((0, 0, 0))
    for point in points:
        total += point
    return total / len(points)


def build_shot(shot: dict[str, Any], plan: dict[str, Any], fps: int, quality: str):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = setup_render(plan, fps, quality)
    build_environment(str(shot.get("env", "meadow_day")), int(shot.get("id", 1)))

    asset_facing = float(plan.get("asset_facing", MODEL_FORWARD_OFFSET))
    actor_specs = [actor_spec for actor_spec in (shot.get("actors") or []) if isinstance(actor_spec, dict)]
    actors = [Actor(actor_spec, asset_facing) for actor_spec in actor_specs]
    if actor_specs and not any(actor.ok for actor in actors):
        missing_names = ", ".join(str(spec.get("model", "?")) for spec in actor_specs)
        raise RuntimeError(f"Shot {shot.get('id')}: none of its character models could be loaded: {missing_names}")
    props = add_shot_props(shot)

    camera_data = bpy.data.cameras.new("EpisodeCamera")
    camera_data.lens = float((shot.get("camera") or {}).get("lens", 30))
    camera = bpy.data.objects.new("EpisodeCamera", camera_data)
    link_object(camera)
    scene.camera = camera
    target = bpy.data.objects.new("CameraTarget", None)
    link_object(target)
    track = camera.constraints.new("TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    overlays = add_card_overlay(camera, str(shot.get("card") or ""))

    # Optional simple rock platform used by the Story Brain hero_rock shot type.
    if shot.get("rock"):
        rock_pos = v2(shot["rock"])
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1.0,
                                             location=(rock_pos[0], rock_pos[1], 0.35))
        rock = bpy.context.object
        rock.scale = (1.5, 1.0, 0.45)
        rock.data.materials.append(make_material("HeroRock", (0.25, 0.26, 0.29)))

    return scene, camera, target, actors, props, camera_data, overlays


def pose_frame(scene, camera, target, actors, props, shot, local_frame: int, shot_frames: int,
                fps: int, camera_data, overlays):
    progress = local_frame / max(1, shot_frames - 1)
    for actor in actors:
        actor.update(progress)
    update_props(props, progress)

    camera_spec = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
    look = camera_target(camera_spec, actors, progress)
    target.location = look

    if isinstance(camera_spec.get("offset"), (list, tuple)) and len(camera_spec["offset"]) >= 3:
        offset = Vector(tuple(float(v) for v in camera_spec["offset"][:3]))
        position = look + offset
    else:
        start = camera_spec.get("pos", [0, -7, 2.2])
        end = camera_spec.get("pos_end", start)
        p = progress * progress * (3 - 2 * progress)
        p0, p1 = v2(start), v2(end)
        position = Vector(tuple(p0[i] + (p1[i] - p0[i]) * p for i in range(3)))

    shake = float(camera_spec.get("shake", 0.0) or 0.0)
    if shake:
        t = local_frame / max(1, fps)
        position.x += shake * math.sin(t * 19.0)
        position.y += shake * math.cos(t * 17.0)
    camera.location = position

    # Sample the imported character animation at its authored 24 FPS while
    # emitting the movie at the requested 12 FPS (or another selected FPS).
    frame = 1.0 + local_frame * 24.0 / fps
    whole = int(frame)
    scene.frame_set(whole, subframe=frame - whole)


def build_frame_map(shots: list[dict[str, Any]], fps: int):
    entries = []
    elapsed = 0.0
    previous_end = 0
    for index, shot in enumerate(shots, start=1):
        duration = shot_duration(shot)
        start = max(previous_end, round(elapsed * fps))
        elapsed += duration
        end = max(start + 1, round(elapsed * fps))
        entries.append({"shot": shot, "start": start, "end": end, "frames": end - start, "duration": duration})
        previous_end = end
    return entries, previous_end, elapsed


def main() -> None:
    args = parse_args()
    if args.fps < 1 or args.fps > 60:
        sys.exit("FPS must be between 1 and 60.")
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        sys.exit("Invalid shard index/count.")
    if not os.path.isfile(args.plan):
        sys.exit(f"Plan file not found: {args.plan}")

    with open(args.plan, "r", encoding="utf-8") as handle:
        plan = json.load(handle)
    shots = plan.get("shots") if isinstance(plan, dict) else None
    if not isinstance(shots, list) or not shots:
        sys.exit("The plan must contain a non-empty shots list.")

    os.makedirs(args.out, exist_ok=True)
    build_index(args.assets)
    frame_map, total_frames, total_seconds = build_frame_map(shots, args.fps)
    shard_start = total_frames * args.shard // args.shards
    shard_end = total_frames * (args.shard + 1) // args.shards
    print(f"Movie: {total_seconds:.2f}s | {total_frames} frames | {args.fps} FPS")
    print(f"Shard {args.shard}/{args.shards}: global frames {shard_start}..{shard_end - 1}")

    started = time.time()
    done = 0
    sample_done = set()
    summary_lines = [
        f"## Render shard {args.shard}/{args.shards}", "",
        f"- Plan: `{args.plan}`", f"- Duration: {total_seconds:.2f}s",
        f"- Frames in this shard: {shard_end - shard_start}",
        f"- FPS: {args.fps}", f"- Quality: {args.quality} ({SAMPLES[args.quality]} Cycles samples)", "",
    ]

    for entry in frame_map:
        lo = max(shard_start, entry["start"])
        hi = min(shard_end, entry["end"])
        if lo >= hi:
            continue
        shot = entry["shot"]
        scene, camera, target, actors, props, camera_data, overlays = build_shot(shot, plan, args.fps, args.quality)
        shot_id = int(shot.get("id", 0))
        summary_lines.append(f"- Shot {shot_id}: {shot.get('title', 'Untitled')} ({hi - lo} frames)")

        if args.sample_only:
            global_frame = (lo + hi - 1) // 2
            local_frame = global_frame - entry["start"]
            pose_frame(scene, camera, target, actors, props, shot, local_frame, entry["frames"],
                       args.fps, camera_data, overlays)
            out_path = os.path.abspath(os.path.join(args.out, f"sample_shot_{shot_id:02d}.png"))
            scene.render.filepath = out_path
            bpy.ops.render.render(write_still=True)
            sample_done.add(shot_id)
            print(f"[sample] shot {shot_id} rendered")
            continue

        for global_frame in range(lo, hi):
            local_frame = global_frame - entry["start"]
            local_index = global_frame - shard_start
            out_path = os.path.abspath(os.path.join(args.out, f"frame_{local_index:05d}.png"))
            if os.path.exists(out_path):
                continue
            pose_frame(scene, camera, target, actors, props, shot, local_frame, entry["frames"],
                       args.fps, camera_data, overlays)
            scene.render.filepath = out_path
            bpy.ops.render.render(write_still=True)
            done += 1
            if done % 10 == 0:
                elapsed = time.time() - started
                print(f"  {done} frames rendered, {elapsed / done:.1f}s/frame", flush=True)

    elapsed = time.time() - started
    summary_lines += ["", f"Rendered {done} frame(s) in {elapsed / 60:.1f} minutes."]
    if ANIM_USED:
        summary_lines += ["", "### Animations used", ""]
        summary_lines += [f"- `{key}` -> `{value}`" for key, value in sorted(ANIM_USED.items())]
    if ANIM_PROBLEMS:
        summary_lines += ["", "### Animation problems", ""]
        summary_lines += [f"- {problem}" for problem in sorted(ANIM_PROBLEMS)]
    if MISSING:
        summary_lines += ["", "### Missing models/assets", ""]
        summary_lines += [f"- {name}" for name in sorted(MISSING)]
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(summary_lines) + "\n")
    print(f"Finished shard {args.shard}: {done} frames in {elapsed / 60:.1f} min")
    if ANIM_PROBLEMS or MISSING:
        print(f"Warnings: {len(ANIM_PROBLEMS)} animation issue(s), {len(MISSING)} missing model(s).")


if __name__ == "__main__":
    main()
