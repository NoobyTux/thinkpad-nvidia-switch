## What does this change and why?

## How was it tested?

<!-- Mocked unit tests do not prove physical power savings. Name the real laptop, CPU, NVIDIA GPU and driver, kernel, distribution and desktop you tested on, or say it was not tested on hardware. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests -v` passes
- [ ] `bash -n install.sh uninstall.sh src/*.sh` passes
- [ ] Root helpers still accept only fixed actions and validated values (no new shell, path or command pass-through)
- [ ] Changes to recovery/restore behavior keep every system change reversible after a partial failure or reboot
- [ ] README, CHANGELOG and SECURITY.md are updated if behavior, files or privileges changed
- [ ] No private logs, usernames, paths or screenshots with personal data are included
