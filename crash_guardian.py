bl_info = {
    "name": "Crash Guardian",
    "author": "Aryrenderz",
    "version": (1, 1, 0),
    "blender": (3, 2, 0),
    "location": "3D Viewport > Sidebar (N-panel) > Render Guardian tab, Render menu (top bar)",
    "description": (
        "Monitors system RAM (and optionally NVIDIA VRAM) while rendering an "
        "animation. If resources drop below a safe threshold before the next "
        "frame, rendering is stopped, the render window is closed, and the "
        "user is asked whether to move the timeline start to the next "
        "unrendered frame and save the file. All settings live in the "
        "add-on's own tab in the 3D Viewport sidebar (N-panel)."
    ),
    "category": "Render",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import gc
import platform
import subprocess
import ctypes


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
    settings = context.scene.srg_settings

    available, total = get_system_memory()
    if available is not None and total:
        percent_free = (available / total) * 100.0
        if percent_free < settings.ram_threshold_percent:
            return False, (
                f"System RAM low: {percent_free:.1f}% free "
                f"(threshold {settings.ram_threshold_percent:.1f}%)"
            )

    if settings.check_vram:
        used, total_vram = get_gpu_memory()
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


# ---------------------------------------------------------------------------
# Settings (stored per-scene, shown in the Properties panel)
# ---------------------------------------------------------------------------

class SafeRenderGuardianSettings(bpy.types.PropertyGroup):
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
    is_monitoring: bpy.props.BoolProperty(default=False)
    last_completed_frame: bpy.props.IntProperty(default=-1)


# ---------------------------------------------------------------------------
# Core operator: frame-by-frame monitored render
# ---------------------------------------------------------------------------

class RENDER_OT_safe_render_animation(bpy.types.Operator):
    bl_idname = "render.safe_render_animation"
    bl_label = "Safe Render Animation"
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

    def invoke(self, context, event):
        scene = context.scene
        settings = scene.srg_settings
        self._current_frame = scene.frame_current
        self._frame_end = scene.frame_end
        self._frame_step = scene.frame_step if scene.frame_step else 1
        self._rendering = False
        self._gc_retried = False
        settings.is_monitoring = True

        wm = context.window_manager
        self._timer = wm.event_timer_add(settings.check_interval, window=context.window)
        wm.modal_handler_add(self)
        self.report({'INFO'}, "Safe Render Guardian: monitoring started")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        scene = context.scene
        settings = scene.srg_settings

        if event.type == 'ESC':
            self._cleanup(context)
            self.report({'WARNING'}, "Safe Render Guardian: cancelled by user")
            return {'CANCELLED'}

        if event.type == 'TIMER' and not self._rendering:
            if self._current_frame > self._frame_end:
                self._cleanup(context)
                self.report({'INFO'}, "Safe Render Guardian: animation complete")
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
            try:
                bpy.ops.render.render(write_still=True)
            except Exception as exc:
                self._rendering = False
                return self._handle_low_resources(context, f"Render error: {exc}")

            self._rendering = False
            settings.last_completed_frame = self._current_frame
            self._current_frame += self._frame_step

        return {'PASS_THROUGH'}

    def _handle_low_resources(self, context, reason):
        next_frame = self._current_frame
        close_render_window(context)
        self._cleanup(context)
        bpy.ops.render.safe_render_recovery(
            'INVOKE_DEFAULT', next_frame=next_frame, reason=reason
        )
        return {'CANCELLED'}

    def _cleanup(self, context):
        context.scene.srg_settings.is_monitoring = False
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None


# ---------------------------------------------------------------------------
# Recovery dialog
# ---------------------------------------------------------------------------

class RENDER_OT_safe_render_recovery(bpy.types.Operator):
    bl_idname = "render.safe_render_recovery"
    bl_label = "Rendering Stopped: Low Resources"
    bl_description = "Recovery dialog shown after Safe Render Guardian stops a render"
    bl_options = {'REGISTER'}

    next_frame: bpy.props.IntProperty(default=0)
    reason: bpy.props.StringProperty(default="")
    update_frame_start: bpy.props.BoolProperty(
        name="Set timeline start to the next frame", default=True,
    )
    save_file: bpy.props.BoolProperty(name="Save file now", default=True)

    def invoke(self, context, event):
        settings = context.scene.srg_settings
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
        self.report({'INFO'}, "Safe Render Guardian: recovery dismissed, no changes made")


# ---------------------------------------------------------------------------
# UI: everything lives in one Properties > Output panel
# ---------------------------------------------------------------------------

class RENDER_PT_safe_render_guardian(bpy.types.Panel):
    bl_label = "Safe Render Guardian"
    bl_idname = "RENDER_PT_safe_render_guardian"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Render Guardian"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.srg_settings

        available, total = get_system_memory()
        if available and total:
            percent = available / total * 100.0
            layout.label(
                text=f"Free RAM: {percent:.1f}%  ({available // (1024**2)} MB)",
            )
        else:
            layout.label(text="Free RAM: unavailable on this platform", icon='INFO')

        row = layout.row()
        if settings.is_monitoring:
            row.label(text="Monitoring active…", icon='REC')
        else:
            row.operator(
                RENDER_OT_safe_render_animation.bl_idname,
                text="Safe Render Animation", icon='RENDER_ANIMATION',
            )

        if settings.last_completed_frame >= 0:
            layout.label(text=f"Last completed frame: {settings.last_completed_frame}")

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


def menu_func(self, context):
    self.layout.operator(
        RENDER_OT_safe_render_animation.bl_idname, icon='RENDER_ANIMATION'
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    SafeRenderGuardianSettings,
    RENDER_OT_safe_render_animation,
    RENDER_OT_safe_render_recovery,
    RENDER_PT_safe_render_guardian,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.srg_settings = bpy.props.PointerProperty(type=SafeRenderGuardianSettings)
    bpy.types.TOPBAR_MT_render.append(menu_func)


def unregister():
    bpy.types.TOPBAR_MT_render.remove(menu_func)
    del bpy.types.Scene.srg_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
