bl_info = {
    "name": "Crash Guardian",
    "author": "Open Source Contributors",
    "version": (1, 2, 0),
    "blender": (3, 2, 0),
    "location": "3D Viewport > Sidebar (N-panel) > Crash Guardian tab, Render menu (top bar)",
    "description": (
        "Monitors system RAM (and optionally NVIDIA VRAM) while rendering an "
        "animation. If resources drop below a safe threshold before the next "
        "frame, rendering is stopped, the render window is closed, and the "
        "user is asked whether to move the timeline start to the next "
        "unrendered frame and save the file. Shows a live viewport overlay "
        "with VRAM usage and time estimates, and scans the scene for common "
        "crash risks."
    ),
    "category": "Render",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import gc
import time
import platform
import subprocess
import ctypes

import blf
import gpu
from gpu_extras.batch import batch_for_shader


# ---------------------------------------------------------------------------
# Resource monitoring helpers
# ---------------------------------------------------------------------------

def get_system_memory():
    """Return (available_bytes, total_bytes) for system RAM, or (None, None)
    if it could not be determined on this platform."""
    try:
        import psutil  # not bundled with Blender, but used if present
        vm = psutil.virtual_memory()
        return vm.available, vm.total
    except ImportError:
        pass

    system = platform.system()
    try:
        if system == "Windows":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullAvailPhys, stat.ullTotalPhys

        elif system == "Linux":
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        value_kb = int(parts[1].strip().split()[0])
                        meminfo[key] = value_kb * 1024
            total = meminfo.get("MemTotal")
            available = meminfo.get("MemAvailable", meminfo.get("MemFree"))
            return available, total

        elif system == "Darwin":
            total_bytes = int(
                subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()
            )
            vm_stat = subprocess.check_output(["vm_stat"]).decode()
            page_size = 4096
            stats = {}
            for line in vm_stat.splitlines():
                if ":" in line:
                    k, v = line.split(":")
                    v = v.strip().rstrip(".")
                    if v.isdigit():
                        stats[k.strip()] = int(v)
            free_pages = stats.get("Pages free", 0) + stats.get("Pages speculative", 0)
            return free_pages * page_size, total_bytes
    except Exception:
        pass

    return None, None


def get_gpu_memory():
    """Best-effort NVIDIA VRAM check via nvidia-smi.
    Returns (used_mb, total_mb) or (None, None) if unavailable."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip().splitlines()
        if out:
            used, total = out[0].split(",")
            return int(used.strip()), int(total.strip())
    except Exception:
        pass
    return None, None


_vram_cache = {"used": None, "total": None, "timestamp": 0.0}


def get_gpu_memory_cached(min_interval=1.0):
    """Cached wrapper around get_gpu_memory() so the overlay (which can be
    redrawn very often, e.g. while orbiting the viewport) doesn't spawn an
    nvidia-smi process on every redraw."""
    now = time.time()
    if now - _vram_cache["timestamp"] > min_interval:
        used, total = get_gpu_memory()
        _vram_cache["used"] = used
        _vram_cache["total"] = total
        _vram_cache["timestamp"] = now
    return _vram_cache["used"], _vram_cache["total"]


def purge_memory():
    """One cheap attempt to free memory before giving up on a frame."""
    try:
        bpy.ops.outliner.orphans_purge(
            do_local_ids=True, do_linked_ids=True, do_recursive=True
        )
    except Exception:
        pass
    gc.collect()


def memory_is_sufficient(context):
    """Check current RAM (and optionally VRAM) against the settings stored
    on the scene. Returns (ok: bool, reason: str)."""
    settings = context.scene.cg_settings

    available, total = get_system_memory()
    if available is not None and total:
        percent_free = (available / total) * 100.0
        if percent_free < settings.ram_threshold_percent:
            return False, (
                f"System RAM low: {percent_free:.1f}% free "
                f"(threshold {settings.ram_threshold_percent:.1f}%)"
            )

    if settings.check_vram:
        used, total_vram = get_gpu_memory_cached(min_interval=0.0)
        if used is not None and total_vram:
            percent_free_vram = ((total_vram - used) / total_vram) * 100.0
            if percent_free_vram < settings.vram_threshold_percent:
                return False, (
                    f"GPU VRAM low: {percent_free_vram:.1f}% free "
                    f"(threshold {settings.vram_threshold_percent:.1f}%)"
                )

    return True, ""


def close_render_window(context):
    """Best-effort: close a dedicated render-result window, or fall back
    the area showing the Render Result back to a normal editor."""
    wm = context.window_manager
    for window in list(wm.windows):
        screen = window.screen
        for area in screen.areas:
            if area.type != 'IMAGE_EDITOR':
                continue
            for space in area.spaces:
                if space.type == 'IMAGE_EDITOR' and space.image and \
                        space.image.name == "Render Result":
                    if len(wm.windows) > 1 and len(screen.areas) == 1:
                        try:
                            with context.temp_override(window=window):
                                bpy.ops.wm.window_close()
                        except AttributeError:
                            try:
                                bpy.ops.wm.window_close({'window': window})
                            except Exception:
                                pass
                        except Exception:
                            pass
                    else:
                        area.type = 'VIEW_3D'


def estimate_frames_until_threshold(samples, threshold_mb):
    """samples: list of (frame_index, available_mb), oldest first.
    Returns an estimated integer number of ADDITIONAL frames that can be
    rendered before available memory drops below threshold_mb, based on a
    simple linear trend across the samples. Returns None if there are too
    few samples or no downward trend is detected (i.e. not currently at
    risk, as far as this estimate can tell)."""
    if len(samples) < 3:
        return None
    first_frame, first_avail = samples[0]
    last_frame, last_avail = samples[-1]
    frame_span = last_frame - first_frame
    if frame_span <= 0:
        return None
    delta = first_avail - last_avail  # positive => memory shrinking over time
    if delta <= 0:
        return None
    avg_decrease_per_frame = delta / frame_span
    if avg_decrease_per_frame <= 0:
        return None
    frames_left = (last_avail - threshold_mb) / avg_decrease_per_frame
    return max(0, int(frames_left))


def format_duration(seconds):
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def get_warning_list(settings):
    return [w for w in settings.warning_text.split("\n") if w]


# ---------------------------------------------------------------------------
# Scene risk scanner
# ---------------------------------------------------------------------------

def analyze_scene_risks(context):
    """Heuristic scan for common causes of render crashes. Deliberately
    lightweight/approximate: it looks at base scene data rather than
    evaluating the full dependency graph, so it stays fast enough to run
    right before a render starts."""
    scene = context.scene
    warnings = []

    # Approximate total polycount from base meshes. Modifiers (subdivision,
    # arrays, etc.) are not evaluated here, so the real number at render
    # time can be significantly higher.
    total_polys = 0
    for obj in scene.objects:
        if obj.type == 'MESH' and obj.data:
            total_polys += len(obj.data.polygons)
    if total_polys > 3_000_000:
        warnings.append(
            f"High base mesh poly count (~{total_polys:,}); modifiers may push this much higher"
        )

    # Heavy subdivision surface modifiers.
    for obj in scene.objects:
        for mod in obj.modifiers:
            if mod.type == 'SUBSURF' and getattr(mod, 'render_levels', 0) >= 5:
                warnings.append(
                    f"'{obj.name}': Subdivision modifier render level is {mod.render_levels}"
                )

    # Large loaded textures (rough VRAM estimate, assumes 4 bytes/pixel).
    total_texture_bytes = 0
    for img in bpy.data.images:
        try:
            w, h = img.size
            if w and h:
                total_texture_bytes += w * h * 4
        except Exception:
            pass
    if total_texture_bytes > 4 * 1024 ** 3:
        warnings.append(
            f"Loaded image textures may use ~{total_texture_bytes / 1024 ** 3:.1f} GB of VRAM"
        )

    # Dense particle systems.
    for obj in scene.objects:
        for psys in obj.particle_systems:
            count = getattr(psys.settings, 'count', 0)
            if count > 200_000:
                warnings.append(
                    f"'{obj.name}': particle system '{psys.name}' has {count:,} particles"
                )

    # Fluid / smoke simulation domains.
    for obj in scene.objects:
        for mod in obj.modifiers:
            if mod.type == 'FLUID' and getattr(mod, 'fluid_type', '') == 'DOMAIN':
                res = getattr(mod.domain_settings, 'resolution_max', 0)
                if res >= 200:
                    warnings.append(
                        f"'{obj.name}': fluid domain resolution is {res}; large sims can exhaust memory"
                    )

    # Cycles-specific risks.
    if scene.render.engine == 'CYCLES':
        cycles = scene.cycles
        if getattr(cycles, 'use_persistent_data', False):
            warnings.append(
                "Persistent Data is enabled — memory can accumulate across frames of an animation"
            )
        if getattr(cycles, 'samples', 0) > 2000:
            warnings.append(f"Very high sample count ({cycles.samples})")

    # Output resolution.
    res_x = scene.render.resolution_x * scene.render.resolution_percentage / 100.0
    res_y = scene.render.resolution_y * scene.render.resolution_percentage / 100.0
    if res_x * res_y > 50_000_000:
        warnings.append(f"Very high output resolution ({int(res_x)}x{int(res_y)} px)")

    return warnings


# ---------------------------------------------------------------------------
# Viewport overlay
# ---------------------------------------------------------------------------

_draw_handler = None


def _blf_set_size(font_id, size):
    try:
        blf.size(font_id, size)
    except TypeError:
        # Older Blender versions require a dpi argument.
        blf.size(font_id, size, 72)


def _get_uniform_color_shader():
    try:
        return gpu.shader.from_builtin('UNIFORM_COLOR')
    except Exception:
        return gpu.shader.from_builtin('2D_UNIFORM_COLOR')


def _draw_box(x, y, width, height, color=(0.0, 0.0, 0.0, 0.55)):
    vertices = ((x, y), (x + width, y), (x + width, y + height), (x, y + height))
    indices = ((0, 1, 2), (2, 3, 0))
    shader = _get_uniform_color_shader()
    batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def draw_callback_px():
    context = bpy.context
    region = getattr(context, "region", None)
    scene = getattr(context, "scene", None)
    if region is None or scene is None:
        return
    settings = getattr(scene, "cg_settings", None)
    if settings is None or not settings.is_monitoring or not settings.show_overlay:
        return

    lines = []

    used, total = get_gpu_memory_cached()
    if used is not None and total:
        free_percent = (total - used) / total * 100.0
        lines.append(f"VRAM: {used:,} / {total:,} MB  ({free_percent:.1f}% free)")
    else:
        lines.append("VRAM: unavailable (non-NVIDIA GPU, or nvidia-smi not found)")

    if settings.avg_frame_seconds > 0:
        remaining = max(settings.total_frames_in_range - settings.frames_rendered_this_session, 0)
        eta = settings.avg_frame_seconds * remaining
        total_est = settings.avg_frame_seconds * settings.total_frames_in_range
        lines.append(f"Avg frame time: {settings.avg_frame_seconds:.1f}s")
        lines.append(f"ETA (remaining frames): {format_duration(eta)}")
        lines.append(f"Estimated total render time: {format_duration(total_est)}")
    else:
        lines.append("Avg frame time: measuring…")

    lines.append(
        f"Frames rendered: {settings.frames_rendered_this_session} / {settings.total_frames_in_range}"
    )
    if settings.projected_renderable_frames >= 0:
        lines.append(
            f"Estimated frames renderable before limit: {settings.projected_renderable_frames}"
        )

    for w in get_warning_list(settings):
        lines.append(f"\u26a0 {w}")

    x = 70  # offset clear of the left-hand tool shelf
    line_height = 20
    padding = 10
    box_width = 440
    box_height = line_height * len(lines) + padding * 2
    top_y = region.height - 50

    _draw_box(x - padding, top_y - box_height + line_height, box_width, box_height)

    font_id = 0
    _blf_set_size(font_id, 13)
    for i, line in enumerate(lines):
        if line.startswith("\u26a0"):
            blf.color(font_id, 1.0, 0.65, 0.15, 1.0)
        else:
            blf.color(font_id, 0.95, 0.95, 0.95, 1.0)
        blf.position(font_id, x, top_y - i * line_height, 0)
        blf.draw(font_id, line)


def add_overlay_draw_handler():
    global _draw_handler
    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            draw_callback_px, (), 'WINDOW', 'POST_PIXEL'
        )


def remove_overlay_draw_handler():
    global _draw_handler
    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None


def tag_redraw_all_view3d(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Settings (stored per-scene, shown in the sidebar panel)
# ---------------------------------------------------------------------------

class CrashGuardianSettings(bpy.types.PropertyGroup):
    ram_threshold_percent: bpy.props.FloatProperty(
        name="Minimum Free RAM (%)",
        description="Stop rendering if available system RAM falls below this "
                    "percentage of total RAM before the next frame starts",
        default=10.0, min=1.0, max=90.0, subtype='PERCENTAGE',
    )
    check_vram: bpy.props.BoolProperty(
        name="Also Monitor GPU VRAM",
        description="Best-effort NVIDIA-only check via nvidia-smi",
        default=True,
    )
    vram_threshold_percent: bpy.props.FloatProperty(
        name="Minimum Free VRAM (%)",
        default=10.0, min=1.0, max=90.0, subtype='PERCENTAGE',
    )
    check_interval: bpy.props.FloatProperty(
        name="Check Interval (s)",
        description="How often to poll system resources between frames",
        default=1.0, min=0.1, max=10.0,
    )
    retry_after_gc: bpy.props.BoolProperty(
        name="Retry Once After Purging Orphan Data",
        description="Attempt garbage collection and an orphan-data purge "
                    "before giving up on a frame",
        default=True,
    )
    auto_save_on_recovery: bpy.props.BoolProperty(
        name="Default 'Save File' to On",
        description="Pre-tick the Save checkbox in the recovery dialog",
        default=True,
    )
    show_overlay: bpy.props.BoolProperty(
        name="Show Viewport Overlay",
        description="Show a live VRAM / ETA / warnings overlay in the 3D "
                    "Viewport while a Crash Guardian render is active",
        default=True,
    )

    is_monitoring: bpy.props.BoolProperty(default=False)
    last_completed_frame: bpy.props.IntProperty(default=-1)
    avg_frame_seconds: bpy.props.FloatProperty(default=0.0)
    frames_rendered_this_session: bpy.props.IntProperty(default=0)
    total_frames_in_range: bpy.props.IntProperty(default=0)
    projected_renderable_frames: bpy.props.IntProperty(default=-1)
    warning_text: bpy.props.StringProperty(default="")


# ---------------------------------------------------------------------------
# Core operator: frame-by-frame monitored render
# ---------------------------------------------------------------------------

class RENDER_OT_crash_guardian_render_animation(bpy.types.Operator):
    bl_idname = "render.crash_guardian_render_animation"
    bl_label = "Crash-Safe Render Animation"
    bl_description = (
        "Render the animation one frame at a time, checking system memory "
        "before each frame and stopping safely if resources are too low"
    )

    _timer = None
    _current_frame = 0
    _frame_end = 0
    _frame_step = 1
    _rendering = False
    _gc_retried = False
    _frame_durations = None
    _ram_samples = None
    _vram_samples = None

    def invoke(self, context, event):
        scene = context.scene
        settings = scene.cg_settings

        self._current_frame = scene.frame_current
        self._frame_end = scene.frame_end
        self._frame_step = scene.frame_step if scene.frame_step else 1
        self._rendering = False
        self._gc_retried = False
        self._frame_durations = []
        self._ram_samples = []
        self._vram_samples = []

        settings.is_monitoring = True
        settings.frames_rendered_this_session = 0
        settings.avg_frame_seconds = 0.0
        settings.projected_renderable_frames = -1
        settings.total_frames_in_range = len(
            range(self._current_frame, self._frame_end + 1, self._frame_step)
        )
        settings.warning_text = "\n".join(analyze_scene_risks(context))

        add_overlay_draw_handler()
        tag_redraw_all_view3d(context)

        wm = context.window_manager
        self._timer = wm.event_timer_add(settings.check_interval, window=context.window)
        wm.modal_handler_add(self)
        self.report({'INFO'}, "Crash Guardian: monitoring started")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        scene = context.scene
        settings = scene.cg_settings

        if event.type == 'ESC':
            self._cleanup(context)
            self.report({'WARNING'}, "Crash Guardian: cancelled by user")
            return {'CANCELLED'}

        if event.type == 'TIMER' and not self._rendering:
            if self._current_frame > self._frame_end:
                self._cleanup(context)
                self.report({'INFO'}, "Crash Guardian: animation complete")
                return {'FINISHED'}

            ok, reason = memory_is_sufficient(context)
            if not ok:
                if settings.retry_after_gc and not self._gc_retried:
                    self._gc_retried = True
                    purge_memory()
                    ok, reason = memory_is_sufficient(context)

            if not ok:
                return self._handle_low_resources(context, reason)

            self._gc_retried = False
            self._rendering = True
            scene.frame_set(self._current_frame)
            start_time = time.time()
            try:
                bpy.ops.render.render(write_still=True)
            except Exception as exc:
                self._rendering = False
                return self._handle_low_resources(context, f"Render error: {exc}")
            elapsed = time.time() - start_time

            self._rendering = False
            settings.last_completed_frame = self._current_frame
            self._update_stats_after_frame(context, elapsed)
            self._current_frame += self._frame_step
            tag_redraw_all_view3d(context)

        return {'PASS_THROUGH'}

    def _update_stats_after_frame(self, context, elapsed_seconds):
        settings = context.scene.cg_settings

        self._frame_durations.append(elapsed_seconds)
        if len(self._frame_durations) > 10:
            self._frame_durations.pop(0)
        settings.avg_frame_seconds = sum(self._frame_durations) / len(self._frame_durations)
        settings.frames_rendered_this_session += 1

        projections = []

        avail_ram, total_ram = get_system_memory()
        if avail_ram is not None and total_ram:
            avail_ram_mb = avail_ram / (1024 ** 2)
            total_ram_mb = total_ram / (1024 ** 2)
            self._ram_samples.append((self._current_frame, avail_ram_mb))
            if len(self._ram_samples) > 8:
                self._ram_samples.pop(0)
            ram_threshold_mb = total_ram_mb * (settings.ram_threshold_percent / 100.0)
            frames_left = estimate_frames_until_threshold(self._ram_samples, ram_threshold_mb)
            if frames_left is not None:
                projections.append(settings.frames_rendered_this_session + frames_left)

        if settings.check_vram:
            used_vram, total_vram = get_gpu_memory_cached(min_interval=0.0)
            if used_vram is not None and total_vram:
                avail_vram_mb = total_vram - used_vram
                self._vram_samples.append((self._current_frame, avail_vram_mb))
                if len(self._vram_samples) > 8:
                    self._vram_samples.pop(0)
                vram_threshold_mb = total_vram * (settings.vram_threshold_percent / 100.0)
                frames_left_v = estimate_frames_until_threshold(self._vram_samples, vram_threshold_mb)
                if frames_left_v is not None:
                    projections.append(settings.frames_rendered_this_session + frames_left_v)

        if projections:
            settings.projected_renderable_frames = min(min(projections), settings.total_frames_in_range)
        else:
            settings.projected_renderable_frames = -1

    def _handle_low_resources(self, context, reason):
        next_frame = self._current_frame
        close_render_window(context)
        self._cleanup(context)
        bpy.ops.render.crash_guardian_recovery(
            'INVOKE_DEFAULT', next_frame=next_frame, reason=reason
        )
        return {'CANCELLED'}

    def _cleanup(self, context):
        context.scene.cg_settings.is_monitoring = False
        remove_overlay_draw_handler()
        tag_redraw_all_view3d(context)
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None


# ---------------------------------------------------------------------------
# Recovery dialog
# ---------------------------------------------------------------------------

class RENDER_OT_crash_guardian_recovery(bpy.types.Operator):
    bl_idname = "render.crash_guardian_recovery"
    bl_label = "Rendering Stopped: Low Resources"
    bl_description = "Recovery dialog shown after Crash Guardian stops a render"
    bl_options = {'REGISTER'}

    next_frame: bpy.props.IntProperty(default=0)
    reason: bpy.props.StringProperty(default="")
    update_frame_start: bpy.props.BoolProperty(
        name="Set timeline start to the next frame", default=True,
    )
    save_file: bpy.props.BoolProperty(name="Save file now", default=True)

    def invoke(self, context, event):
        settings = context.scene.cg_settings
        self.save_file = settings.auto_save_on_recovery
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Rendering was stopped before a likely crash.", icon='ERROR')
        layout.label(text=self.reason)
        layout.separator()
        layout.label(text=f"Next unrendered frame: {self.next_frame}")
        layout.prop(self, "update_frame_start",
                    text=f"Set timeline start to frame {self.next_frame}")
        layout.prop(self, "save_file")

    def execute(self, context):
        scene = context.scene
        if self.update_frame_start:
            scene.frame_start = self.next_frame
            scene.frame_current = self.next_frame
        if self.save_file:
            if bpy.data.filepath:
                bpy.ops.wm.save_mainfile()
                self.report({'INFO'}, "File saved")
            else:
                self.report({'WARNING'}, "File was never saved; use Save As instead")
        return {'FINISHED'}

    def cancel(self, context):
        self.report({'INFO'}, "Crash Guardian: recovery dismissed, no changes made")


# ---------------------------------------------------------------------------
# Manual scene scan operator
# ---------------------------------------------------------------------------

class RENDER_OT_crash_guardian_scan_scene(bpy.types.Operator):
    bl_idname = "render.crash_guardian_scan_scene"
    bl_label = "Scan Scene for Crash Risks"
    bl_description = (
        "Check the scene for common causes of render crashes (heavy "
        "subdivision, huge textures, dense particle systems, large fluid "
        "domains, Persistent Data, extreme samples/resolution, etc.)"
    )

    def execute(self, context):
        warnings = analyze_scene_risks(context)
        context.scene.cg_settings.warning_text = "\n".join(warnings)
        if warnings:
            self.report({'WARNING'}, f"{len(warnings)} potential risk(s) found — see panel")
        else:
            self.report({'INFO'}, "No obvious crash risks detected")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI: everything lives in one sidebar tab
# ---------------------------------------------------------------------------

class RENDER_PT_crash_guardian(bpy.types.Panel):
    bl_label = "Crash Guardian"
    bl_idname = "RENDER_PT_crash_guardian"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Crash Guardian"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.cg_settings

        available, total = get_system_memory()
        if available and total:
            percent = available / total * 100.0
            layout.label(text=f"Free RAM: {percent:.1f}%  ({available // (1024**2)} MB)")
        else:
            layout.label(text="Free RAM: unavailable on this platform", icon='INFO')

        used_vram, total_vram = get_gpu_memory_cached()
        if used_vram is not None and total_vram:
            free_vram_mb = total_vram - used_vram
            vram_free_percent = free_vram_mb / total_vram * 100.0
            layout.label(text=f"Free VRAM: {vram_free_percent:.1f}%  ({free_vram_mb:,} MB)")

        row = layout.row()
        if settings.is_monitoring:
            row.label(text="Monitoring active…", icon='REC')
        else:
            row.operator(
                RENDER_OT_crash_guardian_render_animation.bl_idname,
                text="Crash-Safe Render Animation", icon='RENDER_ANIMATION',
            )

        if settings.last_completed_frame >= 0:
            layout.label(text=f"Last completed frame: {settings.last_completed_frame}")

        if settings.is_monitoring or settings.frames_rendered_this_session > 0:
            layout.label(
                text=f"Frames rendered: {settings.frames_rendered_this_session} / "
                     f"{settings.total_frames_in_range}"
            )
            if settings.avg_frame_seconds > 0:
                remaining = max(
                    settings.total_frames_in_range - settings.frames_rendered_this_session, 0
                )
                layout.label(text=f"Avg frame time: {settings.avg_frame_seconds:.1f}s")
                layout.label(
                    text=f"ETA remaining: "
                         f"{format_duration(settings.avg_frame_seconds * remaining)}"
                )
            if settings.projected_renderable_frames >= 0:
                layout.label(
                    text=f"Est. frames renderable before limit: "
                         f"{settings.projected_renderable_frames}"
                )

        layout.separator()

        box = layout.box()
        box.label(text="Memory Thresholds", icon='PREFERENCES')
        box.prop(settings, "ram_threshold_percent")
        box.prop(settings, "check_interval")

        vbox = box.box()
        vbox.prop(settings, "check_vram")
        sub = vbox.row()
        sub.enabled = settings.check_vram
        sub.prop(settings, "vram_threshold_percent")

        box.prop(settings, "retry_after_gc")

        box2 = layout.box()
        box2.label(text="Recovery Behaviour", icon='FILE_TICK')
        box2.prop(settings, "auto_save_on_recovery")

        box3 = layout.box()
        box3.label(text="Viewport Overlay", icon='RESTRICT_VIEW_OFF')
        box3.prop(settings, "show_overlay")

        box4 = layout.box()
        box4.label(text="Scene Risk Warnings", icon='ERROR')
        box4.operator(RENDER_OT_crash_guardian_scan_scene.bl_idname, icon='VIEWZOOM')
        warnings = get_warning_list(settings)
        if warnings:
            for w in warnings:
                box4.label(text=w, icon='ERROR')
        else:
            box4.label(text="No risks detected yet — click Scan.", icon='INFO')


def menu_func(self, context):
    self.layout.operator(
        RENDER_OT_crash_guardian_render_animation.bl_idname, icon='RENDER_ANIMATION'
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    CrashGuardianSettings,
    RENDER_OT_crash_guardian_render_animation,
    RENDER_OT_crash_guardian_recovery,
    RENDER_OT_crash_guardian_scan_scene,
    RENDER_PT_crash_guardian,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cg_settings = bpy.props.PointerProperty(type=CrashGuardianSettings)
    bpy.types.TOPBAR_MT_render.append(menu_func)


def unregister():
    remove_overlay_draw_handler()
    bpy.types.TOPBAR_MT_render.remove(menu_func)
    del bpy.types.Scene.cg_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
