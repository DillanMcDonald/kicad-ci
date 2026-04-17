#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# blender_render.py — Blender Python script for photorealistic PCB renders.
#
# ── GPL BOUNDARY NOTE ──────────────────────────────────────────────────────────
# This file is MIT-licensed SOURCE CODE. When executed via:
#   blender --background --python scripts/blender_render.py -- <args>
# it runs inside the Blender GPL runtime and accesses `bpy`. The Blender
# process is invoked as an external subprocess by render_dispatch.sh — our
# pipeline code never imports bpy directly. This mirrors the standard approach
# used by projects like KiCad, FreeCAD, and Blender Studio tools: the script
# file carries MIT authorship; the GPL obligation attaches to the Blender
# binary itself, not to the text of this script. Users who modify and
# redistribute ONLY this script file (not the Blender binary) are not bound by
# GPL. See: https://www.gnu.org/licenses/gpl-faq.html#GPLPlugins
# ──────────────────────────────────────────────────────────────────────────────
#
# Usage (run inside Blender runtime — do not call with bare python3):
#   blender --background --python scripts/blender_render.py -- \
#     --input      board.wrl \
#     --output-dir /tmp/renders/ \
#     --presets    iso-left,iso-right,top,front-angled \
#     --samples    128 \
#     --resolution-x 1920 \
#     --resolution-y 1080 \
#     --seed       42 \
#     --material-map config/material_map.yaml \
#     --lighting     config/lighting.yaml \
#     --camera-presets config/camera_presets.yaml \
#     --hdri-path  assets/hdri/studio_small_09_2k.hdr
#
# All arguments after `--` are passed to this script by Blender.

import argparse
import json
import math
import os
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Argument parsing — must happen before any bpy import so errors surface early.
# Blender passes its own args before `--`; we only care about what follows.
# ---------------------------------------------------------------------------

def _parse_args():
    # Everything after the `--` separator belongs to our script.
    try:
        sep = sys.argv.index("--")
        script_args = sys.argv[sep + 1:]
    except ValueError:
        script_args = []

    p = argparse.ArgumentParser(
        prog="blender_render.py",
        description="Render KiCad PCB VRML with Blender Cycles (headless).",
    )
    p.add_argument("--input", required=True, help=".wrl or .step input file")
    p.add_argument("--output-dir", required=True, help="Directory for output PNGs")
    p.add_argument(
        "--presets",
        default="iso-left,top",
        help="Comma-separated camera preset names to render",
    )
    p.add_argument("--samples", type=int, default=128, help="Cycles sample count")
    p.add_argument("--resolution-x", type=int, default=1920)
    p.add_argument("--resolution-y", type=int, default=1080)
    p.add_argument("--seed", type=int, default=0, help="Cycles noise seed (reproducibility)")
    p.add_argument("--material-map", default="config/material_map.yaml")
    p.add_argument("--lighting", default="config/lighting.yaml")
    p.add_argument("--camera-presets", default="config/camera_presets.yaml")
    p.add_argument(
        "--hdri-path",
        default="assets/hdri/studio_small_09_2k.hdr",
        help="Path to HDRI .hdr or .exr file (downloaded by render_dispatch.sh if absent)",
    )
    p.add_argument(
        "--hdri-url",
        default="https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/studio_small_09_2k.hdr",
        help="Fallback URL to fetch HDRI if --hdri-path is missing",
    )
    p.add_argument(
        "--denoising",
        default="NLM",
        choices=["NLM", "OIDN", "none"],
        help="Denoiser: NLM (built-in, always available), OIDN (requires libopenimagedenoise), none",
    )
    return p.parse_args(script_args)


# ---------------------------------------------------------------------------
# YAML loader — minimal, no external deps required inside Blender runtime.
# PyYAML is not guaranteed in Blender's Python; use a tiny safe loader.
# ---------------------------------------------------------------------------

def _load_yaml(path: str) -> dict:
    """Load a simple YAML file (no anchors, no complex types)."""
    try:
        import yaml  # Blender 4.x ships with PyYAML via pip-installed packages
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    # Fallback: crude key:value / list parser for our known schemas.
    return _naive_yaml_load(path)


def _naive_yaml_load(path: str) -> dict:
    """Handles only our specific config YAML shapes — not a general parser."""
    import re
    result = {}
    current_key = None
    current_list = None
    current_dict = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(stripped)
            # Top-level key
            if indent == 0 and ":" in stripped:
                k, _, v = stripped.partition(":")
                v = v.strip()
                current_key = k.strip()
                current_list = None
                current_dict = None
                if v:
                    result[current_key] = _parse_scalar(v)
                else:
                    result[current_key] = None
            # List item
            elif stripped.startswith("- ") and current_key is not None:
                if result.get(current_key) is None:
                    result[current_key] = []
                item_str = stripped[2:].strip()
                if ":" in item_str:
                    # Inline dict: "name: val, key2: val2"
                    item = {}
                    for kv in item_str.split(","):
                        if ":" in kv:
                            ik, _, iv = kv.partition(":")
                            item[ik.strip()] = _parse_scalar(iv.strip())
                    result[current_key].append(item)
                else:
                    result[current_key].append(_parse_scalar(item_str))
            # Nested key under current_key
            elif indent >= 2 and ":" in stripped and current_key is not None:
                if isinstance(result.get(current_key), (type(None), dict)):
                    if result.get(current_key) is None:
                        result[current_key] = {}
                    k, _, v = stripped.partition(":")
                    result[current_key][k.strip()] = _parse_scalar(v.strip())
    return result


def _parse_scalar(v: str):
    v = v.strip().strip("\"'")
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ---------------------------------------------------------------------------
# HDRI download helper
# ---------------------------------------------------------------------------

def ensure_hdri(hdri_path: str, hdri_url: str) -> str:
    if os.path.exists(hdri_path):
        return hdri_path
    os.makedirs(os.path.dirname(os.path.abspath(hdri_path)) or ".", exist_ok=True)
    print(f"[blender_render] Downloading HDRI: {hdri_url} → {hdri_path}")
    try:
        urllib.request.urlretrieve(hdri_url, hdri_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to download HDRI from {hdri_url}: {exc}") from exc
    return hdri_path


# ---------------------------------------------------------------------------
# Blender scene operations — everything below requires bpy
# ---------------------------------------------------------------------------

def clear_scene():
    import bpy
    bpy.ops.wm.read_factory_settings(app_template="")
    # Delete any leftover objects
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    # Remove all orphan data
    for block in list(bpy.data.meshes):
        bpy.data.meshes.remove(block, do_unlink=True)
    for block in list(bpy.data.materials):
        bpy.data.materials.remove(block, do_unlink=True)
    for block in list(bpy.data.lights):
        bpy.data.lights.remove(block, do_unlink=True)
    for block in list(bpy.data.cameras):
        bpy.data.cameras.remove(block, do_unlink=True)


def import_model(filepath: str):
    """Import .wrl (X3D) or .step file into Blender scene."""
    import bpy
    import addon_utils

    ext = Path(filepath).suffix.lower()
    if ext in (".wrl", ".x3d"):
        # io_scene_x3d is bundled in Blender 3.x/4.x but must be enabled.
        addon_utils.enable("io_scene_x3d", default_set=False)
        # Import from the .wrl's directory so relative texture paths resolve.
        prev_dir = os.getcwd()
        os.chdir(os.path.dirname(os.path.abspath(filepath)))
        try:
            bpy.ops.import_scene.x3d(filepath=os.path.abspath(filepath))
        finally:
            os.chdir(prev_dir)
        # KiCad VRML units are mm; Blender treats imported values as metres.
        # Scale the whole import by 0.001 to convert mm → m.
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.transform.resize(value=(0.001, 0.001, 0.001))
        bpy.ops.object.transform_apply(scale=True)
        bpy.ops.object.select_all(action="DESELECT")
    elif ext in (".step", ".stp"):
        # io_import_scene_step addon — may not be available; fail clearly.
        if not hasattr(bpy.ops.import_scene, "step"):
            raise RuntimeError(
                "STEP import operator not available. "
                "Use VRML (.wrl) export from kicad-cli instead."
            )
        bpy.ops.import_scene.step(filepath=filepath)
    else:
        raise ValueError(f"Unsupported model format: {ext}")

    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs:
        raise RuntimeError(f"No mesh objects imported from {filepath}")
    print(f"[blender_render] Imported {len(objs)} mesh object(s) from {filepath}")
    return objs


def compute_board_bounds(objs):
    """Return (center_x, center_y, center_z, max_dim) from all mesh objects."""
    import bpy
    import mathutils

    min_xyz = [float("inf")] * 3
    max_xyz = [float("-inf")] * 3

    for obj in objs:
        for corner in obj.bound_box:
            world_pt = obj.matrix_world @ mathutils.Vector(corner)
            for i in range(3):
                min_xyz[i] = min(min_xyz[i], world_pt[i])
                max_xyz[i] = max(max_xyz[i], world_pt[i])

    cx = (min_xyz[0] + max_xyz[0]) / 2
    cy = (min_xyz[1] + max_xyz[1]) / 2
    cz = (min_xyz[2] + max_xyz[2]) / 2
    dx = max_xyz[0] - min_xyz[0]
    dy = max_xyz[1] - min_xyz[1]
    max_dim = max(dx, dy, 0.001)  # guard against zero-size
    return cx, cy, cz, max_dim


def center_model(objs, cx, cy, cz):
    """Translate all objects so board center sits at origin."""
    import mathutils
    offset = mathutils.Vector((-cx, -cy, -cz))
    for obj in objs:
        obj.location += offset


def setup_cycles(scene, samples: int, seed: int, denoising: str):
    """Configure Cycles renderer for CPU headless rendering."""
    import bpy
    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.seed = seed
    # CPU-only: GPU unavailable on CI runners without GPU passthrough
    prefs = bpy.context.preferences.addons.get("cycles")
    if prefs:
        cycles_prefs = prefs.preferences
        try:
            cycles_prefs.compute_device_type = "NONE"
        except Exception:
            pass
    scene.cycles.use_denoising = denoising != "none"
    if denoising == "OIDN":
        try:
            scene.cycles.denoiser = "OPENIMAGEDENOISE"
        except Exception:
            print("[blender_render] OIDN unavailable, falling back to NLM")
            scene.cycles.denoiser = "NLM"
    elif denoising == "NLM":
        try:
            scene.cycles.denoiser = "NLM"
        except Exception:
            pass
    # Deterministic tile order for reproducibility
    scene.render.use_persistent_data = False
    scene.cycles.use_light_tree = True


def setup_render_output(scene, output_path: str, res_x: int, res_y: int):
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.filepath = output_path
    scene.render.resolution_x = res_x
    scene.render.resolution_y = res_y
    scene.render.resolution_percentage = 100


# ---------------------------------------------------------------------------
# PBR Material assignment
# ---------------------------------------------------------------------------

_SRGB_TO_LINEAR = lambda c: c ** 2.2  # noqa: E731


def _dominant_color(obj) -> tuple:
    """Return (r, g, b) dominant color of an object, in LINEAR space."""
    import bpy
    if obj.data and obj.data.vertex_colors and len(obj.data.vertex_colors) > 0:
        vcol = obj.data.vertex_colors[0]
        if vcol.data:
            r = sum(d.color[0] for d in vcol.data) / len(vcol.data)
            g = sum(d.color[1] for d in vcol.data) / len(vcol.data)
            b = sum(d.color[2] for d in vcol.data) / len(vcol.data)
            return (r, g, b)
    if obj.active_material and obj.active_material.use_nodes:
        for node in obj.active_material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                c = node.inputs["Base Color"].default_value
                return (c[0], c[1], c[2])
    if obj.active_material:
        dc = obj.active_material.diffuse_color
        return (_SRGB_TO_LINEAR(dc[0]), _SRGB_TO_LINEAR(dc[1]), _SRGB_TO_LINEAR(dc[2]))
    return (0.5, 0.5, 0.5)


def _color_distance(a: tuple, b: tuple) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _make_pbr_material(name: str, bsdf_params: dict):
    """Create a Principled BSDF material with given params."""
    import bpy
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()

    out = tree.nodes.new("ShaderNodeOutputMaterial")
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bc = bsdf_params.get("base_color", [0.5, 0.5, 0.5])
    if isinstance(bc, list) and len(bc) >= 3:
        bsdf.inputs["Base Color"].default_value = (bc[0], bc[1], bc[2], 1.0)

    metallic = bsdf_params.get("metallic", 0.0)
    roughness = bsdf_params.get("roughness", 0.5)
    # Blender 4.x: input names unchanged from 3.x for these
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness

    if "ior" in bsdf_params:
        try:
            bsdf.inputs["IOR"].default_value = bsdf_params["ior"]
        except KeyError:
            pass
    if "transmission" in bsdf_params:
        try:
            bsdf.inputs["Transmission Weight"].default_value = bsdf_params["transmission"]
        except KeyError:
            try:
                bsdf.inputs["Transmission"].default_value = bsdf_params["transmission"]
            except KeyError:
                pass

    return mat


def assign_pbr_materials(objs, material_map_path: str):
    """Match each mesh to a PBR material based on dominant vertex color."""
    import bpy
    mat_map = _load_yaml(material_map_path) if os.path.exists(material_map_path) else {}

    # Pre-process: convert sRGB config colors to linear for comparison
    entries = []
    for mat_name, cfg in mat_map.items():
        raw_color = cfg.get("color", [0.5, 0.5, 0.5])
        if isinstance(raw_color, list) and len(raw_color) >= 3:
            lin_color = tuple(_SRGB_TO_LINEAR(c) for c in raw_color)
        else:
            lin_color = (0.5, 0.5, 0.5)
        entries.append((mat_name, lin_color, cfg.get("tolerance", 0.15), cfg.get("bsdf", {})))

    # Cache created materials
    mat_cache = {}

    for obj in objs:
        if obj.type != "MESH":
            continue
        dom = _dominant_color(obj)

        best_name = None
        best_dist = float("inf")
        best_bsdf = {}
        for mat_name, lin_color, tol, bsdf_params in entries:
            dist = _color_distance(dom, lin_color)
            if dist < tol and dist < best_dist:
                best_dist = dist
                best_name = mat_name
                best_bsdf = bsdf_params

        if best_name:
            if best_name not in mat_cache:
                mat_cache[best_name] = _make_pbr_material(f"kicad_{best_name}", best_bsdf)
            obj.data.materials.clear()
            obj.data.materials.append(mat_cache[best_name])
        else:
            # Default: grey diffuse with mild roughness
            fallback_key = "__fallback__"
            if fallback_key not in mat_cache:
                mat_cache[fallback_key] = _make_pbr_material(
                    "kicad_fallback",
                    {"base_color": [dom[0], dom[1], dom[2]], "metallic": 0.0, "roughness": 0.6},
                )
            obj.data.materials.clear()
            obj.data.materials.append(mat_cache[fallback_key])
            print(f"[blender_render] WARN: no material match for {obj.name}, dominant={dom}")


# ---------------------------------------------------------------------------
# HDRI lighting setup
# ---------------------------------------------------------------------------

def setup_hdri_lighting(scene, hdri_path: str, lighting_cfg: dict):
    """Set up World HDRI + three-point studio lights."""
    import bpy
    import mathutils

    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()

    out = tree.nodes.new("ShaderNodeOutputWorld")
    bg = tree.nodes.new("ShaderNodeBackground")
    env = tree.nodes.new("ShaderNodeTexEnvironment")
    mapping = tree.nodes.new("ShaderNodeMapping")
    tex_coord = tree.nodes.new("ShaderNodeTexCoord")

    tree.links.new(tex_coord.outputs["Generated"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    tree.links.new(env.outputs["Color"], bg.inputs["Color"])
    tree.links.new(bg.outputs["Background"], out.inputs["Surface"])

    bg.inputs["Strength"].default_value = lighting_cfg.get("hdri_strength", 1.0)

    # Rotate HDRI via Z-axis mapping
    rot_deg = lighting_cfg.get("hdri_rotation_deg", 0.0)
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(rot_deg))

    if os.path.exists(hdri_path):
        img = bpy.data.images.load(os.path.abspath(hdri_path))
        env.image = img
    else:
        print(f"[blender_render] WARN: HDRI not found at {hdri_path}, world will be grey")

    # Three-point studio lights around board center (origin after centering)
    _add_area_light(
        scene, "KeyLight",
        elevation_deg=45.0, azimuth_deg=30.0, distance=1.5,
        energy=lighting_cfg.get("key_strength", 500.0), size=0.5,
    )
    _add_area_light(
        scene, "FillLight",
        elevation_deg=20.0, azimuth_deg=150.0, distance=1.5,
        energy=lighting_cfg.get("fill_strength", 150.0), size=0.5,
    )
    _add_area_light(
        scene, "RimLight",
        elevation_deg=10.0, azimuth_deg=240.0, distance=1.5,
        energy=lighting_cfg.get("rim_strength", 80.0), size=0.2,
        light_type="SPOT",
    )


def _add_area_light(scene, name, elevation_deg, azimuth_deg, distance,
                    energy, size, light_type="AREA"):
    import bpy
    import mathutils

    el = math.radians(elevation_deg)
    az = math.radians(azimuth_deg)
    x = distance * math.cos(el) * math.cos(az)
    y = distance * math.cos(el) * math.sin(az)
    z = distance * math.sin(el)

    light_data = bpy.data.lights.new(name=name, type=light_type)
    light_data.energy = energy
    if light_type == "AREA":
        light_data.size = size
    elif light_type == "SPOT":
        light_data.spot_size = math.radians(60)
        light_data.spot_blend = 0.5

    obj = bpy.data.objects.new(name=name, object_data=light_data)
    scene.collection.objects.link(obj)
    obj.location = (x, y, z)

    # Point toward origin
    direction = mathutils.Vector((0, 0, 0)) - mathutils.Vector((x, y, z))
    rot_quat = direction.normalized().to_track_quat("-Z", "Y")
    obj.rotation_euler = rot_quat.to_euler()


# ---------------------------------------------------------------------------
# Camera preset system
# ---------------------------------------------------------------------------

_DEFAULT_PRESETS = [
    {"name": "iso-left",      "elevation_deg": 45.0, "azimuth_deg": 315.0, "distance_factor": 2.5, "focal_length_mm": 50.0},
    {"name": "iso-right",     "elevation_deg": 45.0, "azimuth_deg": 225.0, "distance_factor": 2.5, "focal_length_mm": 50.0},
    {"name": "top",           "elevation_deg": 89.0, "azimuth_deg":   0.0, "distance_factor": 3.0, "focal_length_mm": 90.0},
    {"name": "front-angled",  "elevation_deg": 25.0, "azimuth_deg": 270.0, "distance_factor": 2.5, "focal_length_mm": 70.0},
]


def load_camera_presets(camera_presets_path: str) -> list:
    if os.path.exists(camera_presets_path):
        cfg = _load_yaml(camera_presets_path)
        if cfg and "presets" in cfg:
            return cfg["presets"]
    return _DEFAULT_PRESETS


def add_camera(scene, preset: dict, max_dim: float):
    """Create and configure a camera from a spherical-coordinate preset."""
    import bpy
    import mathutils

    el = math.radians(preset.get("elevation_deg", 45.0))
    az = math.radians(preset.get("azimuth_deg", 315.0))
    factor = preset.get("distance_factor", 2.5)
    focal = preset.get("focal_length_mm", 50.0)
    sensor = preset.get("sensor_size_mm", 36.0)

    r = factor * max_dim / 2.0

    x = r * math.cos(el) * math.cos(az)
    y = r * math.cos(el) * math.sin(az)
    z = r * math.sin(el)

    cam_data = bpy.data.cameras.new(name=f"cam_{preset['name']}")
    cam_data.lens = focal
    cam_data.sensor_width = sensor

    cam_obj = bpy.data.objects.new(name=f"cam_{preset['name']}", object_data=cam_data)
    scene.collection.objects.link(cam_obj)
    cam_obj.location = (x, y, z)

    direction = mathutils.Vector((0.0, 0.0, 0.0)) - mathutils.Vector((x, y, z))
    rot_quat = direction.normalized().to_track_quat("-Z", "Y")
    cam_obj.rotation_euler = rot_quat.to_euler()

    return cam_obj


# ---------------------------------------------------------------------------
# Main render loop
# ---------------------------------------------------------------------------

def render_preset(scene, cam_obj, output_path: str, res_x: int, res_y: int):
    import bpy
    scene.camera = cam_obj
    setup_render_output(scene, output_path, res_x, res_y)
    bpy.ops.render.render(write_still=True)
    if not os.path.exists(output_path):
        raise RuntimeError(f"Render produced no output at {output_path}")
    size_kb = os.path.getsize(output_path) / 1024
    print(f"[blender_render] Rendered {output_path} ({size_kb:.1f} KB)")


def main():
    args = _parse_args()
    import bpy

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Ensure HDRI is available (download if missing)
    hdri_path = ensure_hdri(args.hdri_path, args.hdri_url)

    print(f"[blender_render] Input: {args.input}")
    print(f"[blender_render] Output dir: {out_dir}")
    print(f"[blender_render] Samples: {args.samples}, seed: {args.seed}")

    # 1. Clear scene
    clear_scene()

    # 2. Import model
    objs = import_model(args.input)

    # 3. Compute bounds and center
    cx, cy, cz, max_dim = compute_board_bounds(objs)
    center_model(objs, cx, cy, cz)
    print(f"[blender_render] Board bounds: max_dim={max_dim:.3f}m, centered at ({cx:.3f}, {cy:.3f}, {cz:.3f})")

    scene = bpy.context.scene

    # 4. Cycles setup
    setup_cycles(scene, args.samples, args.seed, args.denoising)

    # 5. PBR materials
    assign_pbr_materials(objs, args.material_map)

    # 6. HDRI lighting
    lighting_cfg = _load_yaml(args.lighting) if os.path.exists(args.lighting) else {}
    setup_hdri_lighting(scene, hdri_path, lighting_cfg)

    # 7. Camera presets
    all_presets = load_camera_presets(args.camera_presets)
    requested = set(p.strip() for p in args.presets.split(","))
    selected = [p for p in all_presets if p.get("name") in requested]
    if not selected:
        print(f"[blender_render] WARN: none of {requested} matched presets; using all defaults")
        selected = all_presets

    # 8. Render each preset
    results = []
    for preset in selected:
        name = preset["name"]
        cam = add_camera(scene, preset, max_dim)
        out_path = str(out_dir / f"{name}.png")
        render_preset(scene, cam, out_path, args.resolution_x, args.resolution_y)
        results.append({"preset": name, "path": out_path})

    # 9. Write manifest
    manifest_path = out_dir / "render_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"input": args.input, "renders": results}, f, indent=2)
    print(f"[blender_render] Manifest: {manifest_path}")
    print(f"[blender_render] Done — {len(results)} render(s) complete.")


if __name__ == "__main__":
    main()
