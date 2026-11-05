# Contributing to ThinkPad Nvidia Switch

Thanks for helping improve the app. It is a hardware-specific power-management utility: **a reliable recovery path matters as much as the feature itself**.

## Before opening an issue

Use the repository's bug-report or feature-request issue templates. Include hardware model, CPU, NVIDIA GPU, proprietary driver version, Linux distribution/kernel, and desktop environment. Explain what you expected, what actually happened and how you reproduced it.

The Activity Log's **Technical details** view is useful, but it can contain process names, PIDs and local paths. **Redact private details before sharing.** Please follow [SECURITY.md](SECURITY.md) for vulnerability reports rather than putting sensitive security details in public issues.

## Pull-request guidance

- Keep the GUI unprivileged and action names fixed; no general root-shell entry points or user-writable privileged helpers.
- Root helpers must not depend on the caller's environment: keep `PATH` pinned at every entry point, keep the `#!/usr/bin/python3 -Es` shebang (it ignores `PYTHON*` variables and the user site), call programs by absolute path or through the pinned `PATH`, and never import from a user-writable directory.
- The GUI must not assume it can read `/var/lib/thinkpad-nvidia-switch/` file *contents* (they are root-only); test for existence with `path_exists()`, and in shell scripts test through `sudo` (`root_has`).
- Preserve NVIDIA PCI enumeration; do not reintroduce PCI `remove`/`rescan`, firmware power hacks or irreversible GPIO operations.
- For changes that touch the Stronger protection mode, ensure all permission and PCI mask changes remain reversible even after partial failure or a reboot.
- For CPU changes, use detected topology/capabilities and do not change unrelated saved CPU settings.
- Keep normal UI copy friendly; retain technical explanations and diagnostics in the README and Activity Log.
- Include tests for normal operation, unsupported hardware, interrupted operations and rollback paths.
- Describe which **actual** laptop/kernel/driver configurations you tested. Never claim that mocked tests validate physical power savings.

Run before submitting:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n install.sh uninstall.sh src/*.sh
shellcheck --severity=warning install.sh uninstall.sh src/*.sh   # if installed
ruff check .                                                      # if installed; rules come from pyproject.toml and match CI
```

**Run the tests as an ordinary user, not as root.** CI does, and a test that quietly depends on root (file ownership and permission checks behave differently for root) passes on your machine and fails there.

The tests that open the real window need PySide6 and run offscreen (`QT_QPA_PLATFORM=offscreen`); they skip themselves when PySide6 or Qt's system libraries (for example `libegl1`) are missing. Interface changes should be checked at several window widths and with every tab selected: no blank frame below a tab's content and no clipped or overlapping controls (`tests/test_regressions.py` shows how). Interface code must not cause continuous repainting: never call `invalidate()` or `updateGeometry()` from a `sizeHint()`, and apply a style sheet or re-polish a widget only when its state actually changes. `IdleCpuUseTests` fails when an idle visible window keeps repainting.

All automated tests must be hardware-independent, use mock sysfs/PCI state, and never modify a contributor's actual CPU/GPU, system files or `/var/lib/` data.

## Repository checklist for maintainers

- Maintain `LICENSE`, `README.md`, `CHANGELOG.md` and `SECURITY.md`. Update the version in `src/optimus_tray.py` (`APP_VERSION`), `install.sh` (`RELEASE_VERSION`), the README header and the CHANGELOG together; a test checks that they agree.
- Build release archives from a clean checkout (`git archive`), never from a working directory, so caches and local files cannot slip in.
- Use a signed or otherwise reviewed release process for root-owned helper changes.
- Enable branch protection, CI and GitHub Private Vulnerability Reporting if available.
- Never commit personal log dumps, screenshots with private details, secrets, binaries of unknown origin or Python caches.
- Avoid promises of compatibility with untested Linux distributions and GPUs.

## Power profile contributions

Changes to automatic AC/battery switching must maintain the rule that saved-state restoration cannot execute first when automatic profiles are enabled. Add hardware-independent tests for profile JSON validation, AC power-source detection, privilege-command allowlists, and boot priority. Test suspend/resume and loss of a charger with extra care on real hardware. Never bypass root-helper input validation or turn the profile executor into an arbitrary command runner.

## Profile schema and voltage changes

Keep profile schema migration tests for earlier saved versions. The battery graph's window/interval remain global UI preferences, never part of a profile. Automatic power profiles must exclude old startup state restoration and temporarily disable the Remember checkbox while preserving its prior choice. Voltage changes must be carefully bounded, use the dedicated root-owned helper, and avoid overwriting independently configured power-limit/thermal directives. Add tests for any modification of the privileged argument schema or recovery behavior. Real-hardware undervolting tests must be conducted manually on an unlocked processor.
