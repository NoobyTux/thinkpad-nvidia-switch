# SPDX-License-Identifier: MIT
"""A restricted adapter for the existing intel-undervolt configuration and tool.

Never writes MSRs directly. Only voltage domains 0/1/2 can be changed; never
accept arbitrary paths, shell commands, voltage domains, or power-limit changes.
"""
from __future__ import annotations
import hashlib
import fcntl
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

# sudo keeps the *caller's* PATH unless the distribution sets secure_path. These
# helpers run as root on behalf of an unprivileged user, so they must never look
# anything up in an inherited, user-controlled PATH (a planted "modprobe" would
# otherwise run as root).
SAFE_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

CONF = Path('/etc/intel-undervolt.conf')
# The only owner the helper accepts for the configuration file it edits as root. A named constant so that
# tests, which run as an ordinary user, can say whose files count as trusted; production always uses root.
TRUSTED_UID = 0
STATE = Path('/var/lib/thinkpad-nvidia-switch/undervolt-original.json')
LABELS = {0: 'CPU', 1: 'GPU', 2: 'CPU Cache'}
READER = re.compile(r'^\s*(CPU Cache|CPU|GPU)\s*\(([012])\)\s*:\s*([+-]?\d+(?:\.\d+)?)\s*mV', re.I | re.M)


# Hard limit for every offset the helper will accept. The CPU can encode far larger
# offsets, so this is a safety bound, not a hardware limit: it caps what a typo,
# a bug or any process running as the desktop user can request without a password.
MIN_OFFSET_MV = -200
# Offsets below this are legal but rarely stable. The GUI asks for an extra,
# explicit confirmation before applying them or saving them in a profile.
WARN_BELOW_MV = -100


def validate_mv(values):
    if (not isinstance(values, (list, tuple)) or len(values) != 3
            or any(type(x) not in (int, float) or not math.isfinite(x) or not MIN_OFFSET_MV <= x <= 0 for x in values)):
        raise ValueError(f'Offsets must be finite numbers between {MIN_OFFSET_MV} and 0 mV (CPU, cache, Intel GPU).')
    return tuple(values)


def read_offsets(output):
    values = {}
    for name, index, value in READER.findall(output):
        if LABELS[int(index)].casefold() == name.casefold():
            values[int(index)] = float(value)
    if set(values) != {0, 1, 2}:
        raise ValueError('intel-undervolt did not report readable CPU, cache and Intel GPU offsets.')
    return {'cpu': values[0], 'cache': values[2], 'gpu': values[1]}


def updated_config(text, cpu, cache, gpu):
    """Update only voltage 0/1/2 in the existing config; preserve other lines."""
    validate_mv((cpu, cache, gpu))
    target = {0: cpu, 1: gpu, 2: cache}
    seen = set()
    lines = []
    for line in text.splitlines(keepends=True):
        raw = line.strip()
        if not raw or raw.startswith('#'):
            lines.append(line)
            continue
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError as exc:
            raise ValueError('Cannot safely parse the existing intel-undervolt configuration.') from exc
        if not parts:
            lines.append(line)
            continue
        if parts[0] in ('power', 'tjoffset'):
            raise ValueError('Other active intel-undervolt power/thermal rules found. Disable those rules first to avoid overwriting CPU power limits.')
        if parts[0] == 'undervolt' and len(parts) >= 2:
            if len(parts) != 4 or not parts[1].isdigit() or int(parts[1]) not in range(5):
                raise ValueError('Unsupported voltage entry in intel-undervolt.conf.')
            index = int(parts[1])
            if index in target:
                if index in seen:
                    raise ValueError('Duplicate voltage domain in intel-undervolt.conf.')
                seen.add(index)
                lines.append(f'undervolt {index} "{LABELS[index]}" {target[index]}\n')
                continue
        lines.append(line)
    for index in (0, 1, 2):
        if index not in seen:
            if lines and not lines[-1].endswith('\n'):
                lines.append('\n')
            lines.append(f'undervolt {index} "{LABELS[index]}" {target[index]}\n')
    return ''.join(lines)


def safe_binary():
    for candidate in ('/usr/bin/intel-undervolt', '/usr/local/bin/intel-undervolt'):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('intel-undervolt is not installed. Install the intel-undervolt package first.')


def run_undervolt(action):
    binary = safe_binary()
    result = subprocess.run([binary, action], capture_output=True, text=True, timeout=12, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'intel-undervolt failed').strip()[:400])
    return result.stdout


def check_conf(path=CONF):
    # Say which of the three different problems it is: a vague "expected a regular file" hid
    # the commonest one (the package is not installed, so there is no file at all).
    if path.is_symlink():
        raise RuntimeError(f'{path} is a symbolic link. For safety the app only edits a regular file; '
                           'replace the link with a regular copy of its target.')
    if not path.exists():
        raise RuntimeError(f'{path} does not exist. The intel-undervolt package normally installs it; '
                           'reinstall the package or create the file (see the README, "Voltage offsets").')
    if not path.is_file():
        raise RuntimeError(f'{path} is not a regular file.')
    st = path.stat()
    if st.st_uid != TRUSTED_UID or st.st_mode & 0o022:
        raise RuntimeError(f'{path} must be root-owned and not group/world writable.')
    return path.read_text(encoding='utf-8')


def write_private(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', encoding='utf-8', delete=False) as stream:
            tmp = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def apply_voltages(cpu, cache, gpu, conf=CONF, state=STATE):
    validate_mv((cpu, cache, gpu))
    # Look for the program before the config: without the package there is no config either,
    # and "install intel-undervolt" is the useful answer.
    safe_binary()
    original = check_conf(conf)
    original_mode = conf.stat().st_mode & 0o777
    updated = updated_config(original, cpu, cache, gpu)
    if not state.exists():
        write_private(state, json.dumps({'original': original}, ensure_ascii=False) + '\n')
    write_private(conf, updated, original_mode)
    try:
        run_undervolt('apply')
        actual = read_offsets(run_undervolt('read'))
        for key, expected in (('cpu', cpu), ('cache', cache), ('gpu', gpu)):
            if abs(actual[key] - expected) > 1.1:
                raise RuntimeError(f'{key} voltage offset was not accepted by the CPU (reported {actual[key]:g} mV).')
    except Exception:
        write_private(conf, original, original_mode)
        try:
            run_undervolt('apply')
        except Exception:
            pass
        raise
    # Keep only a hash of the last configuration applied by this program.
    # Uninstall restores the original only if no one has edited the file since.
    try:
        stored = json.loads(state.read_text(encoding='utf-8'))
        stored['last_hash'] = hashlib.sha256(updated.encode('utf-8')).hexdigest()
        write_private(state, json.dumps(stored, ensure_ascii=False) + '\n')
    except Exception as exc:
        write_private(conf, original, original_mode)
        try:
            run_undervolt('apply')
        except Exception:
            pass
        raise RuntimeError(f'Could not save undervolt recovery data: {exc}') from exc
    return actual


def uninstall_restore(conf=CONF, state=STATE):
    if not state.is_file():
        return 'No app-managed voltage settings were recorded.'
    saved = json.loads(state.read_text(encoding='utf-8'))
    original = saved.get('original')
    last_hash = saved.get('last_hash')
    if not isinstance(original, str) or not isinstance(last_hash, str):
        return 'No confirmed app-applied voltage configuration; original remains unchanged.'
    if not conf.exists() and not conf.is_symlink():
        # The intel-undervolt package (and its config) was removed after the app
        # last wrote it; there is nothing left to restore and uninstall must not hang.
        return 'intel-undervolt.conf no longer exists; nothing to restore.'
    current = check_conf(conf)
    if hashlib.sha256(current.encode('utf-8')).hexdigest() != last_hash:
        return 'intel-undervolt.conf was edited outside the app; preserving its current contents.'
    original_mode = conf.stat().st_mode & 0o777
    write_private(conf, original, original_mode)
    try:
        run_undervolt('apply')
    except Exception:
        write_private(conf, current, original_mode)
        raise
    return 'Original intel-undervolt configuration and offsets restored.'


def main(args=None):
    import sys
    args = list(sys.argv[1:] if args is None else args)
    if os.geteuid() != 0:
        raise PermissionError('Run this root-owned helper via sudo.')
    os.environ['PATH'] = SAFE_PATH
    if args == ['read']:
        print(json.dumps(read_offsets(run_undervolt('read'))))
    elif args == ['uninstall-restore']:
        print(uninstall_restore())
    elif len(args) == 4 and args[0] == 'set':
        # Fixed simple decimals only (up to hundredths); no paths, shell, NaN or exponents.
        if any(not re.fullmatch(r'-?(?:0|[1-9]\d*)(?:\.\d{1,2})?', x) for x in args[1:]):
            raise ValueError('Expected three decimal voltage offsets in millivolts.')
        # Serialize app voltage edits against other instances of our own helper.
        # /run/lock is world-writable on some distributions. Keep the lock
        # inside our root-owned state directory, and reject symlinks.
        lockdir = STATE.parent
        lockdir.mkdir(parents=True, mode=0o755, exist_ok=True)
        st = lockdir.lstat()
        if lockdir.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
            raise RuntimeError('Unsafe voltage state-directory permissions.')
        lockfile = lockdir / '.undervolt.lock'
        descriptor = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'r+') as lock:
            if os.fstat(lock.fileno()).st_uid != 0:
                raise RuntimeError('Voltage control lock must be root-owned.')
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            print(json.dumps(apply_voltages(*(float(x) for x in args[1:]))))
    else:
        raise ValueError('Expected read, set <cpu_mV> <cache_mV> <gpu_mV>, or uninstall-restore.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=__import__('sys').stderr)
        raise SystemExit(1)
