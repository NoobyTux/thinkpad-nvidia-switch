# Changelog

## 2.6.9 — Audit release: layout fix, security hardening, fresh-install fixes (2026-11-05)

### Security

- **Root helpers no longer trust the caller's `PATH`.** `sudo` keeps the invoking user's `PATH` unless the distribution sets `secure_path`; the GPU helper called `modprobe` by bare name, so any process running as the desktop user could plant a fake `modprobe` and have it executed as root through the passwordless rule. Every root entry point (GPU, CPU, profile and voltage helpers and both shell wrappers) now pins `PATH` to system directories before doing anything else.
- **Root Python helpers ignore the caller's Python environment.** The four root entry points now start with `#!/usr/bin/python3 -Es`: no `PYTHON*` variable and no user site directory is honoured. `sudo` normally resets the environment, but a distribution that keeps `PYTHONPATH` would otherwise let a user process make a helper import its own `json.py` (or any other standard-library name) as root; this was reproduced with the old shebang and is now covered by a test.
- Private state files are created with their final permissions (`0600`/`0644`) instead of being `chmod`-ed after creation, and a failed write leaves no partial file behind.
- The single-instance lock no longer falls back to a world-writable `/tmp`.

### Changed

- **Voltage offsets may go down to −200 mV (was −100 mV), with a warning.** The limit is now one constant (`MIN_OFFSET_MV` in `undervolt_control.py`) shared by the root helper, profile validation and both sets of spin boxes. Offsets below −100 mV (`WARN_BELOW_MV`) need an extra confirmation that defaults to *No*: when applying them, and when saving newly chosen ones in a profile (profiles can run unattended at login and on every charger change). The helper still rejects anything beyond −200 mV, whoever asks.

### Fixed

- **The CPU tab repainted and re-laid-out itself every second while idle** (about 40 events a second, found when the idle-window test hit its limit on GitHub's CI runner). The once-a-second refresh set the speed and core-count spin boxes' ranges and values and several labels to what they already were, and Qt repaints and re-lays-out the page for that. These setters now change a widget only when the new value differs; an idle visible window does close to nothing on every tab. A new test refreshes unchanged CPU readings repeatedly and checks that nothing is repainted, and the idle-window limit is now 15 events a second instead of 40.
- **Automatic profiles could apply only the first step.** Root cause, found from a user's log: after the cores were switched, the helper re-applied the previous CPU speed cap, and the kernel refused it for one policy (`policy4 rejected frequency cap (3500000 kHz read back)`). That follow-up failure made `cores-set` exit with an error although the cores had already changed, and the profile executor then stopped at that "failed" step, leaving the speed limit, voltage offsets and NVIDIA settings unapplied. Because a failure of an *automatic* apply was only written to the log, it looked as if the profile "only disables 2 cores" (applying it by hand later worked, since nothing had to be re-applied). Now re-applying the old cap after a core change is best-effort: the core change reports success with a warning (shown in the activity log and carried into the profile result), and the profile continues. In addition, the executor no longer stops at the first problem: every independent group (cores, speed, power limits, voltage, NVIDIA) is attempted even if another failed; a failed CPU step is retried once after 1.5 s; the report names every failed step and what was applied; a failed automatic apply shows a tray warning, puts the reason in the *Power profile* tooltip and is retried once after 10 s (never in a loop).
- A second log from the same laptop showed the speed cap being refused for the first seconds after the charger was unplugged (`policy0 rejected frequency cap (3500000 kHz read back)`, then accepted on the automatic retry 10 s later). The cause on that system is not known yet, so the app now (a) waits and rewrites a refused cap (four short, growing pauses, about two seconds in total, and none at all when the first read is right) before giving up, (b) tries a still-refused speed cap twice more *after* the profile's other steps (3 s and 5 s later) instead of holding them up, and (c) writes a `SPEED LIMIT DIAGNOSTICS` snapshot to the technical log when a cap is refused (driver, governor, per-policy min/max, intel_pstate settings, known power tools that are running).
- **"Restore CPU hardware maximum" could fail right after a manual speed limit** (`policy0 did not accept hardware maximum 3500000 kHz (read back 800000)`, and a second click a moment later worked). The kernel applies a written `scaling_max_freq` from a work queue, so reading the file straight after the write can still show the old value. The wait-and-rewrite step existed only for *setting* a limit; both restore paths (restore to hardware maximum, restore of the previous limits) read the value back once and gave up. All three paths now share one helper, and a failed restore also writes the `SPEED LIMIT DIAGNOSTICS` snapshot to the technical log.
- **"NVIDIA is in use" now says by whom.** On a Lenovo Y520 (Intel + GTX 1060, KDE Wayland) turning NVIDIA off failed with `modprobe: FATAL: Module nvidia_drm is in use` and nothing else. The helper only looked for programs holding `/dev/nvidia*`, so a compositor holding NVIDIA's display node (`/dev/dri/cardN` or `renderDN`) was invisible. It now also checks the DRM nodes that belong to the NVIDIA PCI device, appends `NVIDIA is held open by: kwin_wayland (PID 812), …` to the error (profile warnings and tray balloons repeat the names), and writes `NVIDIA HOLDER` and `NVIDIA MODULE` lines (reference count, modules using it, `modeset`, `fbdev`) to the technical log, which also covers a driver held by the kernel rather than by a program.
- **A visible window used far too much CPU (about 12 % of a core on a laptop; a whole core in an offscreen test), while the hidden app used almost none.** Root cause: `ContentSizedTabWidget.sizeHint()` called `invalidate()` on the page layout every time it was asked for its size. Each invalidation queues a layout request, the parent layout then asks for `sizeHint()` again, which invalidates again: an endless relayout/repaint loop (about 940 layout requests and 470 repaints per second, measured with an event filter) that only runs while the window is on screen. The bug is older than 2.6.9 (the original 2.6.8 shows the same loop). A size-hint getter must have no side effects: it no longer invalidates anything, and the stale-cache refresh that the invalidation was meant to provide now happens once, on the explicit triggers (`remeasure_page()` when the window is shown, resized or the tab changes). Measured on the same machine with a realistic mock sysfs tree (offscreen, software-rendered, so absolute values are higher than on real hardware): visible window about 99 % → 0.7 % of a core, hidden 0.8 % → 0.2 %; events per second on an idle window: about 1,400 → 0.
- Smaller CPU savings found on the way: the GPU-state colour and the ON/OFF button style were re-applied every second (a style-sheet change and a re-polish that restyle and relayout the whole window) and are now applied only when the state changes; the `/proc` scan that finds programs using NVIDIA is about 4× faster with identical results (no `pathlib` objects per entry); the battery and CPU read-outs (about 140 sysfs reads a second) and the 15-second voltage poll (which starts `sudo`, a Python helper and `intel-undervolt`) are skipped while the window is hidden in the tray and refreshed as soon as it is shown. GPU state tracking, wake/sleep notifications, the tray menu and its checkboxes, battery-history sampling and automatic profile switching are unchanged and keep running while hidden.
- **The NVIDIA ON/OFF button gave no feedback.** The privileged helper ran on the window's own thread, so after a click the whole window froze for the seconds the GPU needed and it felt as if nothing had been pressed. The switch now runs in the background (`QProcess`): the button shows a spinning arc and "Turning NVIDIA off…/on…" from the click until Linux reports the new power state (or 10 seconds after the helper finished, whichever comes first), a second click is ignored, and the protections, the tray toggle, the profile buttons and automatic profile switching are paused until it ends. The automatic switch is not lost: it runs as soon as the GPU switch has finished.
- **A failed manual switch showed nothing, and profile messages did not say what was using the GPU.** Pressing *Turn NVIDIA OFF* while a program used NVIDIA only wrote a raw `modprobe: FATAL: Module nvidia_drm is in use` line to the log. A failed GPU switch (and a failed Basic/Stronger protection switch) now shows a plain explanation, in a dialog or as a tray notification when the window is hidden: *NVIDIA cannot be turned off because it is in use by blender and Steam. Close them, then try again.* Names come from the helper and from the window's own list of NVIDIA applications; process IDs and `modprobe` text are never shown. Desktop components (`kwin_wayland`, `Xwayland`, …) are described as the desktop session, with a pointer to the KDE section, and `systemd`/`systemd-logind` entries that only mirror the compositor are dropped. Profile warnings and tray balloons use the same wording. A missing driver, a timeout and other errors get their own short reason instead of the helper's raw output.
- **The checked button in the Activity Log was clipped** (a letter cut off *Recent activity* or *Technical details*). The `:checked` style made the label bold, but Qt sizes a button for its regular text, so the bold label overflowed by 3–4 px. The selected button is now marked by its red border and background only, with no change of font weight. Found while rendering screenshots of the real window; a photo from the T440p showed the same cut-off.
- **A working GPU power-off was reported as a failure.** On the ThinkPad T440p the first automatic Battery apply showed `modprobe: FATAL: Module i915 is in use` and a failure warning, although NVIDIA did turn off (the GPU went to `D3cold` a second later and the automatic retry, which found no module left to unload, succeeded). `modprobe -r` also removes unused dependencies and processes the soft dependencies configured in `modprobe.d` (checked in kmod's source), and it can fail on one of those after every NVIDIA module is already gone. The helper only looked at modprobe's exit status. It now checks the result: when no NVIDIA module is loaded any more, the unload counts as done and modprobe's complaint is kept in the technical log only; when a module really remains, it is still an error with the list of holders. In the technical log, `MODPROBE PLAN` lines (a dry run of `modprobe -r`) show which modules modprobe intends to touch, so the origin of the `i915` entry can be traced.
- **The KWin Intel-only workaround was incomplete on current NVIDIA drivers.** The Lenovo Y520 log (NVIDIA registered as `card0`, `nvidia-drm` with `modeset=Y`) showed `kwin_wayland`, `systemd-logind`, `systemd` (which keeps logind's copy) and Xwayland holding NVIDIA's *display* device `/dev/dri/card0` although KWin held no NVIDIA render node. The drop-in only set `KWIN_RENDER_NODES`, which limits render nodes; KWin's DRM backend, however, opens every GPU on the seat unless `KWIN_DRM_DEVICES` is set (checked in KWin's source: `DrmBackend::initialize` and `GpuManager`). The old 470 driver on the ThinkPad T440p creates no such display device, which is why the workaround looked complete there. The installer now also sets `KWIN_DRM_DEVICES` through a second app-owned, colon-free alias (`~/.config/thinkpad-nvidia-switch/kwin-intel-card` -> Intel's stable `pci-…-card` link). It requires the render node and the display device to belong to the same single Intel GPU, checks both aliases before changing anything, upgrades a drop-in written by an earlier release in place, and the uninstaller removes both forms. **Re-run `./install.sh` and log out and back in** to get it. Monitors wired to NVIDIA do not work in the KDE session while it is installed (opt out with `TPNS_KWIN_INTEL_ONLY=0`).
- **Applying voltage offsets reported a misleading error when `intel-undervolt` was not installed** (`Expected a regular, root-owned /etc/intel-undervolt.conf file.`, seen on both fresh CachyOS installs). The helper checked the config file before looking for the program, and without the package there is no config file, so the real cause was hidden. It now looks for the program first (`intel-undervolt is not installed. Install the intel-undervolt package first.`). The config check also names the actual problem: file missing, symbolic link, not a regular file, or wrong owner/permissions. On the CPU tab the *Apply* and *Reset* voltage buttons are greyed out with an explanation while the package is missing and come back on their own once a check finds it (a password or hardware problem does not grey them out). A profile with voltage offsets explains the same thing in its warning and is not retried automatically, since installing a package does not happen by itself within seconds.
- The repeated `Voltage read failed: intel-undervolt is not installed` line (written every 15 seconds) is now logged once per distinct problem, and the voltage card says that the package is not installed.
- A failed profile step is now reported in plain words: the raw `TPNS_DETAIL|…` audit lines no longer appear in the failure text, tooltip or notification (they still go to the technical log), a missing NVIDIA kernel module for the running kernel gets an actionable explanation, and failures that retrying cannot fix are not retried.
- **Empty space below the GPU, CPU and Profiles tabs.** `QTabWidget` forwards height-for-width to its stacked pages and answers with the tallest page, which a parent layout trusts over `sizeHint()`. The GPU tab was laid out at the CPU tab's height (about 660 px of blank frame) and Profiles about 360 px. Height-for-width is now disabled on `ContentSizedTabWidget`, and the exact wrapped height at the real width is used, so every tab hugs its content and nothing is squeezed.
- **Fresh installs: the GUI raised `PermissionError` every timer tick.** The state directory `/var/lib/thinkpad-nvidia-switch` was created `0700` (root only), and the unprivileged GUI tests for recovery records inside it. The directory is now created searchable (`0755`; the files inside stay `0600`), existing `0700` directories are repaired by the installer and the helpers, and the GUI treats an unreadable location as "absent" instead of raising.
- **Installer and uninstaller tested root-only files as the desktop user.** On an upgrade over a `0700` state directory the installer therefore overwrote the user's remembered CPU settings, and the uninstaller skipped CPU frequency/power-limit and PCI-mask recovery and then deleted the records. All such checks now go through `sudo`.
- **Startup exception and lost tab.** `currentChanged` fired while the window was still being built, raising `AttributeError` and overwriting the saved tab with the first one on every launch. The remembered tab is now restored.
- Spin boxes (cores, CPU speed), the profile name field and the profile list were unstyled (bright white in the dark theme, unreadable on light desktop themes); spin-box arrows were invisible. The window now sets an explicit dark palette and styles these widgets.
- The wake log said `Basic+Basic` when only the basic guard was active.
- Profile editor: a duplicate name is reported before the dialog closes (previously the edits were discarded), and the suggested default name no longer collides with an existing profile.
- The app no longer exits silently when its lock file cannot be created, and waits up to 20 seconds for a system tray at login instead of failing immediately.
- The close handler accepts the close while the desktop is saving the session, so a visible window cannot veto log-out (Qt's `isSavingSession()`; not exercised on a real session manager in the automated tests).
- Log files were re-read and rewritten in full on every appended line once they reached their cap; they are now trimmed in batches.
- The tray icon and tooltip are only updated when the GPU state changes (they were rebuilt every second).
- Battery power and current readings are used by magnitude: some drivers report negative values while discharging, which previously showed as "unavailable".
- Uninstalling no longer fails when `/etc/intel-undervolt.conf` has been removed, and removes the `__pycache__` files that root created next to the helper modules.

### Installer

- Refuses to run as root, requires Python 3.9+ for both the desktop `python3` and the root helpers' `/usr/bin/python3`, and authenticates before inspecting root-only records.
- The launcher records the exact Python interpreter that was verified to have PySide6, so virtual-environment installs start from the menu.
- The "already running" check only matches real Python processes running `optimus_tray.py`; the uninstaller performs the same check.
- The KDE `KWIN_DISABLE_VULKAN=1` session file is only installed where KWin exists, and can be skipped with `TPNS_KWIN_VULKAN_FIX=0`.
- Checks at the end that the passwordless rule really works and warns if it does not.
- When the optional `intel-undervolt` package is missing the installer now prints a clear note with the install command (it previously printed a one-line hint that was easy to miss). It looks in the same two places as the root helper (`/usr/bin`, `/usr/local/bin`) instead of searching `PATH`.
- The uninstaller's closing message says that packages you installed yourself (PySide6, `intel-undervolt`, the NVIDIA driver) are left in place.

### Project

- Added SPDX licence identifiers, a pull-request template, a broader `.gitignore`, ShellCheck and Ruff in CI, a Python 3.9/3.11/3.13 test matrix and an offscreen Qt test job.
- New regression tests (`tests/test_regressions.py`) run the real window offscreen and cover every fix above; each was verified to fail on v2.6.8.
- README rewritten against the code. *Requirements* is split into required and optional parts and the Arch/CachyOS install commands now include `intel-undervolt` (the package was only named in a table row, so following the commands never installed it). *Install*, *Upgrade* and *Uninstall* are separate sections with their own entries in the table of contents; *Uninstall* lists what is restored, what is removed, and that packages are left for you to remove (with the command, and the order that matters for `intel-undervolt`).
- Bytecode caches are no longer part of the release archive.
- Release preparation: a `pyproject.toml` with the CI lint rules (a plain `ruff check .` now matches CI), `.editorconfig`, Dependabot for the pinned GitHub Actions, a *Compatibility report* issue template, an app-version and graphics-mode field in the bug template, blank issues disabled, README screenshots (rendered with simulated readings, in `docs/screenshots/`), a *Tested hardware* section, and the regression test module renamed to `tests/test_regressions.py`.
- **CI runs the Python matrix on Ubuntu 24.04.** GitHub's `ubuntu-latest` moved to Ubuntu 26.04, for which no Python 3.9 is published (`The version '3.9' with architecture 'x64' was not found for Ubuntu 26.04`), although the project supports 3.9. The three Python jobs are pinned to `ubuntu-24.04`; the lint and Qt jobs stay on `ubuntu-latest`.
- **The tests no longer depend on the umask** of the account that runs them (some systems use 002, which made files group-writable and failed four tests). They now fix the umask to 022 for the duration of those tests.
- **The tests no longer assume they run as root.** Three undervolt tests built a temporary configuration file owned by whoever ran them and were rejected by the helper's "must be root-owned" rule, so they passed as root but failed for any ordinary user, which is how GitHub's CI runners run (found by the first CI run after publishing). The helper's rule is unchanged and still strict; the owner it trusts is now a named constant (`TRUSTED_UID`, root) that those tests set to their own user, and new tests prove that a file owned by anyone else, or writable by group or others, is still refused. The whole suite was re-run as an ordinary user, with no `XDG_RUNTIME_DIR`, on Python 3.9, 3.11 and 3.13 and with PySide6.
- Full pre-release analysis of the archive: ruff, bandit (no medium or high findings), vermin (Python 3.9 is the minimum), ShellCheck at every severity, byte-compilation on Python 3.9, 3.11, 3.12 and 3.13, YAML and unit-file validation, and scans for personal data, leftovers and whitespace problems.

## 2.6.8 — Live voltage summary and passwordless runtime controls

- Show current CPU core, CPU cache, and Intel integrated graphics voltage offsets directly under the PL1/PL2 and maximum-frequency dashboard line, retaining both decimal places.
- Refresh the voltage readout automatically in the background every 15 seconds and after CPU-tab visits, profile changes, and manual voltage changes. Use a non-blocking Qt process to avoid freezing the UI while reading hardware state; leave unsaved voltage editor values alone.
- Replace the last manual `pkexec` GPU and CPU operations with `sudo -n` and extend the installed root-owned helper sudoers allowlist, so normal GUI operations do not prompt for passwords. Any missing authorization is reported instead of falling back to an interactive prompt.
- Keep administrator authentication for installation/uninstallation, keep manual confirmation of potentially unstable voltage changes, and preserve hardware safety checks in the helpers.
- Preserve all existing GPU, CPU, battery, profile and notification features.

## 2.6.7 — Tab clipping regression repair

- Revert the faulty v2.6.6 height algorithm, which measured the geometry of already-compressed cards and then repeatedly shrank the tab again. This could overlap buttons, labels, and list widgets in both CPU and Profiles.
- Use `ContentSizedTabWidget` to request the natural height of the *selected tab's layout* through Qt size hints and, where supported, height-for-width. No manual fixed tab height is assigned.
- Keep per-tab compact sizing, the responsive one/two-column breakpoints and normal scroll-area overflow. No power-management, helper, or notification behavior changed.
- Add regression coverage preventing the old geometry-based shrinking from returning.


## 2.6.6 — Notification preferences and compact CPU page

- Add a **Notifications** menu next to Options, with individually persisted switches for NVIDIA power changes, charger connection changes, successfully applied automatic profiles, and successful startup restoration of saved GPU/CPU settings.
- Keep previous GPU notifications enabled by default; new notification categories start off until the user opts in. Avoid spurious charger notifications from the initial power-source reading at login.
- Show the active application using the NVIDIA GPU in the D0 alert when an NVIDIA device-holder can be identified (for example, Steam). Never claim certainty about which process triggered PCI wake; otherwise show an explicit unknown-application message.
- Correct lingering CPU-tab whitespace by using the measured bottom edge of rendered cards rather than a stale Qt grid size hint. Retain the responsive one/two-column layout and per-tab sizing.
- Preserve all NVIDIA, CPU, power profile, battery-history and undervolting functions; no changes to privileged helper commands.

## 2.6.5 — Responsive layout regression fixes

- Fix exaggerated vertical spacing in GPU controls by sizing tabs to their selected page and keeping the protection card compact.
- Fix clipped Profiles controls by using the visible scroll viewport width for layout decisions instead of a stale child width.
- Remove leftover grid row stretch and give each side-by-side card a conservative readable minimum width.
- Rearrange CPU speed and core actions onto two rows to avoid cramped controls in split view.
- Preserve the v2.6.4 compact battery display, automatic voltage read-back, profile behavior and existing hardware controls.

## 2.6.4 — Compact battery header and responsive layout (initial version)

- Automatically disable **Remember and restore** when automatic power profiles are enabled, retaining its previous on/off setting and restoring that choice when automatic switching is disabled; never run old startup settings ahead of the selected profile.
- Remove battery history window and update interval from profiles. Migrate version 1 named profile files to version 2 without deleting names, assignments, CPU/GPU controls or automatic switching.
- Display battery discharge watts and estimated time remaining, or on AC while charging display battery charge watts and estimated time until full (where supported). Retain the history graph and its global preferences.
- Add optional, manually confirmed CPU cores, CPU cache and Intel integrated GPU undervolting controls using the external intel-undervolt tool, with voltage read-back, strict bounds, preserved external config directives, and conditional uninstall cleanup. Profiles can opt in to tested voltage settings; existing profiles leave them unchanged.
- Add root-owned privileged helper, narrow sudoers entry, updated documentation and hardware-independent tests.
- No changes to existing NVIDIA wake/guard, KWin Intel render, core hotplug, cpufreq, RAPL, or profile AC/battery detection algorithms.

## 2.6.2

- Integrate the KWin 6.8 Intel-only render-node workaround validated on a ThinkPad T440p with NVIDIA 470.
- Detect the Intel GPU through PCI udev render links and vendor/class metadata, without hardcoding `renderD128` or `card1`.
- Use an app-owned colon-free symlink to the stable PCI by-path render link, so changes in render-node numbering do not alter the selected GPU.
- Restrict KWin only using `KWIN_RENDER_NODES` and Mesa EGL service environment settings; preserve Steam/PRIME offloading and other application environments.
- Guard installation by KWin version, Intel render device, and Mesa vendor configuration checks; skip ambiguous hardware rather than guess.
- Remove only unchanged app-managed KWin service configuration on uninstall; preserve manually created drop-ins and provide rollback directions.
- Keep all v2.6.1 GPU, CPU, battery-history, profile switching and startup-priority behaviors.


## 2.6.1

- Apply the CPU portion of profiles before NVIDIA changes, so a busy NVIDIA
  DRM module cannot prevent battery-saving CPU limits from being applied.
- Show a human-readable explanation when the NVIDIA driver is in use;
  preserve full diagnostic logs without putting them in a modal popup.
- Continue to refuse forced driver unloading, PCI removal or rescanning.

## 2.6.0

- Added an unlimited named profile manager with create, edit, delete, save-current and manually apply actions.
- Added optional automatic AC/battery profile selection with an on/off checkbox and separately assigned profiles for plugged-in and battery operation.
- Added a power-source indicator to the shared dashboard and a Profiles shortcut in the tray menu.
- Saved profiles atomically under the user's local config directory; corrupt saved profiles are not silently discarded.
- Added a restricted, root-owned profile application helper. The desktop UI stays unprivileged; helper actions and values are validated.
- Gave automatic profiles **exclusive priority after login**, preventing the old remembered CPU/GPU snapshot from applying before the correct AC/battery profile. Unknown/unassigned power sources result in no fallback restore.
- Debounced power-source changes and prevented repeated unattended retry loops after profile errors.
- Preserved the v2.5.6 GPU/CPU, battery-history, logging, scrolling and recovery features.

## 2.5.6

- Moved **View activity log** into the shared top dashboard and added a tray-menu shortcut. The log remains modeless.
- Reworded GPU/CPU controls, descriptions and power-limit explanations for non-specialists while keeping technical details in the log and documentation.
- Fixed guard status reporting when the app-owned PCI `config` mask recovery record exists but the other Aggressive guard records are missing.
- Made desktop UI preference writes atomic to reduce the risk of losing saved selections after an interrupted write.
- Rebuilt the README and refreshed public-repository contributing/security guidance and issue templates.
- No intentional changes to GPU device-removal policy, NVIDIA power-control actions, CPU hotplug, RAPL or frequency helper actions.

## 2.5.5

- Published verified same-boot Intel PL1/PL2 limits into an unprivileged, read-only status file while keeping the private recovery record protected.

## 2.5.4

- Restored CPU maximum supported frequency and captured early-boot Intel RAPL limits for reliable restoration.

## 2.5.3

- Added a CPU maximum-frequency control and separated remembered CPU core, power and frequency settings.

## 2.5.2

- Added topology-aware selection of how many physical CPU cores to disable.

## 2.5.1

- Fixed topology reporting after CPU core hotplug and extended saved-state restoration to CPU settings.

## 2.5.0

- Added the CPU tab, CPU topology/power dashboard and privileged CPU power controls.

## 2.4.x

- Added the modeless log, scrollable UI, battery-history preferences and reversible PCI wake protections.
