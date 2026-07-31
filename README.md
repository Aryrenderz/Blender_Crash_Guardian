# Crash Guardian (Blender Add-on)

An open-source Blender add-on that watches system memory while an animation
renders. If free RAM (and optionally NVIDIA VRAM) drops below a threshold
**before the next frame would start**, it stops rendering, closes the render
window, and asks you whether to move the timeline start to the next
unrendered frame and save the file.

## Installation

1. Download `crash_guardian.py`.
2. In Blender: **Edit ▸ Preferences ▸ Add-ons ▸ Install…**, select the file.
3. Enable **"Crash Guardian"** in the add-ons list.
4. That's it — no separate preferences step. Everything is configured
   directly in the 3D Viewport sidebar per file/scene (see below).

## Usage

Open the **Sidebar** in the 3D Viewport (press **N**, or the small arrow tab
on the right edge of the viewport). You'll find a new **Crash Guardian**
tab alongside any other add-on tabs you have (Tool, View, Animation, etc.).
Everything lives in this one panel:

- **Free RAM** readout at the top, live.
- **Safe Render Animation** button (also available in the top **Render**
  menu) — starts the monitored, frame-by-frame render.
- **Last completed frame** readout once a render has run.
- **Memory Thresholds** box:
  - Minimum Free RAM (%)
  - Check Interval (s)
  - Also Monitor GPU VRAM (with its own Minimum Free VRAM %)
  - Retry Once After Purging Orphan Data
- **Recovery Behaviour** box:
  - Default "Save File" to On (pre-ticks Save in the recovery dialog)

Because these are **scene properties**, they're saved with your .blend file
— every file can have its own thresholds, and they travel with the file
(e.g. to a render farm) rather than living only in one Blender install's
preferences.

The add-on renders frame by frame instead of using Blender's normal
"Render Animation," checking free RAM/VRAM against your thresholds before
every frame. If resources are too low, it will:
1. Stop the render loop (no more frames are started).
2. Close the Render Result window/area.
3. Optionally do one garbage-collection + orphan-data purge and retry the
   check once (configurable).
4. Pop up a dialog telling you why it stopped, offering to set
   **Timeline Start** to the next unrendered frame and **Save** the file.

Press **Esc** at any time to cancel monitoring manually.

## How detection works (and its limits)

Blender doesn't expose a way to predict an out-of-memory crash with
certainty, and there's no public API to safely abort a frame that's
*already* rendering. So this add-on takes the practical approach:

- It renders **one frame at a time** via its own loop (instead of Blender's
  built-in multi-frame animation render), and checks memory **before each
  frame starts**.
- RAM is read via `psutil` if installed in Blender's Python, otherwise via
  platform-native fallbacks (`/proc/meminfo` on Linux, `GlobalMemoryStatusEx`
  on Windows, `vm_stat`/`sysctl` on macOS).
- VRAM is read via `nvidia-smi` if present (NVIDIA GPUs only; there's no
  portable way to query AMD/Intel VRAM without vendor-specific tools).

**Important limitation:** this protects you between frames, not mid-frame.
A single frame that is *itself* too heavy (e.g., a scene whose memory use
spikes only partway through that frame's render) can still crash Blender,
because Blender's own `render.render()` call blocks and can't be safely
interrupted from a handler once it's underway. In practice, the between-
frame check catches the overwhelming majority of "memory creeps up over an
animation" crashes (leaking modifiers/geometry caches, growing sim caches,
etc.), which is the common failure mode this add-on targets.

## License

MIT — do whatever you like with it, no warranty. Contributions welcome.
