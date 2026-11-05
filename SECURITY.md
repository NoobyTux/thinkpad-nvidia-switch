# Security and hardware safety

ThinkPad Nvidia Switch is a privileged power-control utility. It can modify NVIDIA driver-loading rules, executable/file permissions, PCI sysfs visibility, CPU online status, CPU frequency policy and Intel RAPL package-power limits. **Only install it after reviewing the code and its system changes.** Its normal user interface is not a substitute for a security sandbox.

## Supported and tested environment

The project is tested by its author on two Lenovo laptops running CachyOS with KDE Plasma on Wayland: a ThinkPad T440p (i7-4710MQ, GT 730M, legacy NVIDIA 470 driver) and a Y520 (i5-7300HQ, GTX 1060 Mobile). Other hardware, kernels, distributions, desktops and driver branches have not undergone comparable hardware testing. A passing CI run does not prove real-world safety.

## Privilege boundary

The GUI runs without root privileges. **Manual GPU and CPU actions, automatic profile switching and voltage reads/writes** use `sudo -n` with installed root-owned helper binaries under `/usr/local/libexec/`. The installer installs and checks a sudoers `NOPASSWD` rule for the current local desktop account **only for the named application helpers and auto-restore wrappers**, not a blanket rule granting unrestricted passwordless sudo. No runtime password/fingerprint dialog is shown: if the sudoers rule is absent, revoked, or invalid, that operation fails and is logged. Initial installation/upgrades and uninstallation still require administrator authorization.

The GPU helper checks a requested NVIDIA PCI address against `/etc/thinkpad-nvidia-switch/gpu-pci-address` and validates the device class/vendor. The CPU helper validates bounded numeric inputs, operates only on discovered CPU power controls, and rejects unknown commands. The profile helper validates an allowlisted profile payload and invokes only installed helper commands without a shell. The voltage helper handles only bounded offsets and a root-owned `intel-undervolt` configuration.

**Environment hardening.** `sudo` normally resets the environment (`env_reset`, the default) but keeps the caller's `PATH` unless the distribution configures `secure_path`. Every root entry point therefore sets `PATH` to fixed system directories before it starts any subprocess, so a program planted earlier in the user's `PATH` cannot be run as root (in v2.6.8 this was a local privilege-escalation path on systems without `secure_path`; earlier releases were not checked). Do not disable `env_reset` for these commands; the helpers rely on sudo removing variables such as `LD_PRELOAD` and `BASH_ENV`. As defence in depth the Python helpers start with `#!/usr/bin/python3 -Es`, so they also ignore every `PYTHON*` variable and the user site directory even if a distribution configures sudo to keep them (a user-controlled `PYTHONPATH` could otherwise make a helper import, for example, its own `json.py` as root). The helpers also import their own modules (`cpu_power.py`, `profiles.py`, `undervolt_control.py`) from `/usr/local/libexec/`, which must stay root-owned and not writable by your user.

**Privilege trade-off:** any process running as the same desktop user can invoke the allowlisted helpers without extra authentication, including actions that change CPU/graphics performance or potentially destabilize the machine through undervolting. Users should not install this feature on shared, managed, or untrusted accounts without an explicit security review. Do not make the installed helper files or imported modules writable by your user. The installer verifies a staged sudoers file with `visudo` before activating it; the uninstaller removes that rule once recovery operations finish. No helper accepts arbitrary shell commands or arbitrary caller-provided file paths, but the power settings themselves remain privileged and safety-sensitive.

## Stronger GPU protection: system-wide effects

The stronger guard may temporarily:

- install NVIDIA modprobe block rules;
- alter permission bits on `nvidia-modprobe` executables and NVIDIA-specific Vulkan/EGL/OpenCL loader manifests;
- set `driver_override=none` on the detected NVIDIA PCI function;
- bind-mount `/dev/null` over its sysfs `config` file in the executing mount namespace.

It **does not remove or rescan** PCI devices. However, PCI diagnostics and NVIDIA-aware applications can behave differently under the protection, and mount namespaces may limit which processes see the mask. It is not a security boundary and cannot guarantee an absence of GPU wakeups.

The helper records original modes and PCI override values before changing them, and refuses to unmount a foreign/unrecognized mount. Do not manually delete records in `/var/lib/thinkpad-nvidia-switch/`, especially after an interrupted operation. The directory is searchable by all users (`0755`) so the GUI can tell that recovery records exist; the files inside are root-only (`0600`). Disable Stronger protection before updating GPU drivers or attempting to game with NVIDIA.

## CPU controls and restoration

CPU hotplug always tries to leave at least the bootstrap physical core online and only brings back app-managed cores. A failed hotplug or power-limit operation attempts rollback. Intel RAPL PL1/PL2 limits are **ceilings**, not power-usage targets. Hardware/firmware may reject changes, or another system power tool may overwrite them.

The early-boot systemd service captures **observed early-boot** RAPL values; these are not necessarily manufacturer-immutable defaults. Its private snapshot is stored as root-controlled state, while a verified, non-sensitive copy is published for GUI display. Restoration refuses to invent unavailable boot defaults.

Restoring CPU frequency uses the hardware maximum for active policies, rather than an arbitrary prior custom cap. Hotplug and frequency-policy support vary across kernel versions.

## System installation and desktop changes

The installer sets up root-owned helpers, a restricted sudoers rule, a systemd boot-snapshot service, desktop launchers and, where KWin is installed, a KDE session environment file setting `KWIN_DISABLE_VULKAN=1` (skip it with `TPNS_KWIN_VULKAN_FIX=0`). This KDE workaround can affect the desktop's graphics backend. The uninstaller removes app-managed installation state **after** attempting to restore guard permissions, PCI access and CPU settings. If recovery fails, the uninstaller preserves records for repair.

The installer's user destination is below `~/.local/share/thinkpad-nvidia-switch/`. Avoid launching the installer with `sudo` because that may install user files into root's home directory.

## Logs and privacy

`~/.local/state/thinkpad-nvidia-switch/` contains logs that may include process names, PIDs, command output and home-directory paths. Review and redact them before attaching to an issue or posting publicly. The application stores UI preferences, named power profiles and logs with owner-only permissions; its status files expose only limited diagnostic information.

## Reporting vulnerabilities

**Do not post exploit instructions or sensitive logs in public issues.** Use GitHub's private vulnerability reporting (the repository's **Security** tab, then **Report a vulnerability**). If that option is not available, open a public issue that says only that you have a security report, without technical details, and ask for a private channel.

Maintainers: enable *Private vulnerability reporting* (Settings, Code security) before announcing the project, and keep branch protection and CI required for changes to anything installed under `/usr/local/libexec/`.

## Automatic profile behavior

Automatic switching is performed by the **running desktop app**, not at early boot. It polls reported charger status; if the status is unknown or an assignment is missing, it makes no automatic change. The prior-state restore is suppressed entirely when automatic profiles are enabled. The helper applies CPU steps first, then voltage, then NVIDIA; it attempts every independent group even if an earlier one failed (stopping only inside the dependent NVIDIA steps) and reports partial application: it cannot guarantee that a multi-step GPU/CPU transition is atomic. A failed automatic apply is retried once after 10 seconds. Profiles containing stronger PCI protection may interfere with other programs, just like using the stronger protection manually. Other tools may compete for Intel RAPL and cpufreq settings. See the README for assignment and restart details.

## Recovery if something goes wrong

1. Stop programs that use NVIDIA and attempt to disable Stronger protection through the GUI.
2. If needed, close the app and retry the included `uninstall.sh`; it attempts safe rollback.
3. Do not remove app-owned recovery records if any restoration step fails.
4. A reboot clears temporary sysfs power states and mounts but does **not** automatically revert chmod changes to installed driver metadata. Use the app's recovery procedure.
5. Do not attempt runtime PCI removal/rescanning; that approach failed to rediscover NVIDIA on the tested T440p without a reboot.

A recovery case involving firmware, kernel or other power tools may require manual repair. Seek experienced Linux administration help rather than guessing sysfs values or deleting state records.

### KWin Intel-only setting

When KWin >= 6.7.4, one Intel GPU with a render path **and** a display device
(`/dev/dri/by-path/pci-…-render` and `…-card`, both on the same PCI device) and
Mesa's EGL vendor file are available, the installer writes a user-scoped systemd
drop-in for KWin (`KWIN_RENDER_NODES`, `KWIN_DRM_DEVICES` and the Mesa EGL
vendor file) and two app-owned symlinks to Intel's stable PCI by-path nodes.
It does not write a global GLVND environment variable. This is a compatibility workaround,
not a security sandbox or proof that all applications will avoid NVIDIA.
Displays connected directly to the NVIDIA GPU may stop working in this mode.
The KWin-only restriction applies after the next login. It can be rolled back
by removing the app-owned drop-in from a text console and rebooting. Manual
user drop-ins (including earlier 50-mesa-only.conf and 70-intel-render.conf)
are never removed by this installer/uninstaller.

## Optional Intel voltage offsets

The CPU undervolt UI runs through a root-owned `/usr/local/libexec/thinkpad-nvidia-switch-undervolt` helper. The sudoers entry allows only this installed helper, not a shell or user-supplied executable path. It accepts `read`, `set <cpu_mv> <cache_mv> <gpu_mv>`, and `uninstall-restore`, and strictly checks decimal offsets (up to two digits after the decimal point) in the range −200..0 mV (the GUI additionally asks for confirmation below −100 mV). The helper uses the external `intel-undervolt` executable and `/etc/intel-undervolt.conf`, not its own arbitrary MSR accesses. It rejects active power or thermal configuration rules that could override the separate RAPL power controls.

**Security and operational warning:** granting the current desktop user passwordless access to voltage control is a deliberate privilege. An account compromise could cause instability even without memory corruption or privilege escalation. Undervolting is not guaranteed stable and can crash the operating system. Do not deploy this unrestricted on shared/untrusted desktops. Other software that edits or reapplies `intel-undervolt.conf` can still conflict; test before enabling voltage changes in automatic AC/battery profiles. Config changes are recorded and selectively undone on uninstall only while the file is unchanged since the application's own latest write.
