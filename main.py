#!/usr/bin/env python3
"""Entry point, CLI and daemonizer for the Real-time AI Interview Assistant.

The phone URL + QR code are printed to the terminal BEFORE the process forks
into the background, so the user can scan and then close the terminal. After
the double-fork there is absolute silence on stdout/stderr - everything goes to
the log file.

Note: the local module ``daemon.py`` is the orchestrator (not the python-daemon
package). Backgrounding is implemented with a manual double-fork to avoid the
name clash and to keep zero external runtime surprises.
"""

import argparse
import atexit
import os
import secrets
import signal
import sys
import time
from pathlib import Path

import config
from logutil import log, set_echo


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------
def _read_pid():
    try:
        with open(config.PID_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_pid():
    config.ensure_dirs()
    with open(config.PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid():
    try:
        os.remove(config.PID_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Server key
# ---------------------------------------------------------------------------
def _ensure_server_key():
    if config.SERVER_KEY:
        return config.SERVER_KEY
    key = secrets.token_urlsafe(12)
    config.SERVER_KEY = key
    _persist_env("SERVER_KEY", key)
    return key


def _persist_env(name, value):
    env_path = Path(__file__).resolve().parent / ".env"
    lines = []
    found = False
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            found = True
            break
    if not found:
        lines.append(f"{name}={value}")
    try:
        env_path.write_text("\n".join(lines) + "\n")
    except Exception as e:
        log(f"main: could not persist {name} to .env: {e}")


# ---------------------------------------------------------------------------
# QR / URL display
# ---------------------------------------------------------------------------
def _print_access(url):
    print("\n" + "=" * 52)
    print("  Real-time AI Interview Assistant")
    print("=" * 52)
    print(f"\n  Open on your phone:\n  {url}\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as e:
        print(f"  (QR code unavailable: {e})")
    print("\n  Scan the QR code above with your phone (same WiFi).")
    print("  Daemon will background itself - you can close this terminal.\n")
    print("=" * 52 + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Daemonize (manual double fork)
# ---------------------------------------------------------------------------
def _daemonize():
    if os.fork() > 0:
        os._exit(0)  # parent exits
    os.setsid()
    if os.fork() > 0:
        os._exit(0)  # second parent exits

    sys.stdout.flush()
    sys.stderr.flush()
    config.ensure_dirs()

    devnull = os.open(os.devnull, os.O_RDONLY)
    logfd = os.open(config.LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull, sys.stdin.fileno())
    os.dup2(logfd, sys.stdout.fileno())
    os.dup2(logfd, sys.stderr.fileno())
    os.close(devnull)

    set_echo(False)  # no more stderr echo; everything to log file
    _write_pid()
    atexit.register(_remove_pid)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_start(args):
    if _pid_alive(_read_pid()):
        print("Daemon already running.")
        return

    from audio import list_devices

    mic = args.mic if args.mic is not None else _int_or_none(os.environ.get("MIC_INDEX"))
    sysd = args.sys if args.sys is not None else _int_or_none(os.environ.get("SYS_INDEX"))
    fallback_ai = args.ai or config.DEFAULT_FALLBACK_AI

    # Prefer PulseAudio/PipeWire source names (deterministic speaker labeling).
    # Auto-detect each field independently when blank, since the default sink /
    # source can change between sessions.
    mic_source = config.MIC_SOURCE or None
    sys_source = config.SYS_SOURCE or None
    if mic_source is None or sys_source is None:
        from audio import default_sources

        auto_mic, auto_sys = default_sources()
        if mic_source is None:
            mic_source = auto_mic
        if sys_source is None:
            sys_source = auto_sys

    key = _ensure_server_key()

    # Build server just to compute the URL for display before forking.
    from server import PhoneServer

    url = PhoneServer(key).url()
    _print_access(url)

    if not config.GEMINI_API_KEY:
        print("  [warn] GEMINI_API_KEY not set - starting on Deepgram fallback.")
    if not config.DEEPGRAM_API_KEY:
        print("  [warn] DEEPGRAM_API_KEY not set - fallback transcription disabled.")
    print(f"  Mic source (YOU)        : {mic_source or '(sounddevice index)'}")
    print(f"  System source (INTERVIEWER): {sys_source or '(none)'}")

    time.sleep(0.5)
    _daemonize()

    # --- past this point we are the backgrounded process ---
    from daemon import InterviewDaemon

    log("=" * 40)
    log(
        f"daemon boot mic={mic} sys={sysd} mic_source={mic_source} "
        f"sys_source={sys_source} ai={fallback_ai}"
    )

    def _on_signal(signum, frame):
        log(f"daemon: received signal {signum}")
        try:
            d.stop()
        finally:
            _remove_pid()
            os._exit(0)

    d = InterviewDaemon(
        mic_index=mic,
        sys_index=sysd,
        fallback_ai=fallback_ai,
        server_key=key,
        mic_source=mic_source,
        sys_source=sys_source,
    )
    _seed_context(args)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    d.start()
    d.run_forever()


def cmd_stop(args):
    pid = _read_pid()
    if not _pid_alive(pid):
        print("Daemon not running.")
        _remove_pid()
        return
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        print("Daemon stopped.")
    except Exception as e:
        print(f"Failed to stop: {e}")
    _remove_pid()


def cmd_restart(args):
    cmd_stop(args)
    time.sleep(1)
    cmd_start(args)


def cmd_status(args):
    pid = _read_pid()
    if _pid_alive(pid):
        print(f"Daemon RUNNING (pid {pid})")
        print(f"Log: {config.LOG_FILE}")
        from server import PhoneServer

        key = config.SERVER_KEY or "(unknown)"
        print(f"Phone URL: http://{_lan()}:{config.SERVER_PORT}/?key={key}")
    else:
        print("Daemon NOT running.")


def cmd_list_devices(args):
    from audio import list_devices, list_pulse_sources, default_sources

    devs = list_devices()
    if devs:
        print("sounddevice input devices:")
        for d in devs:
            print(
                f"  [{d['index']}] {d['name']} "
                f"(ch={d['channels']}, sr={int(d['default_samplerate'])})"
            )
    else:
        print("No sounddevice inputs found (is sounddevice installed?).")

    sources = list_pulse_sources()
    if sources:
        print("\nPulseAudio/PipeWire sources (preferred - deterministic labeling):")
        for s in sources:
            tag = ""
            if s["name"].endswith(".monitor"):
                tag = "  <- system audio (INTERVIEWER)"
            print(f"  [{s['id']}] {s['name']}{tag}")

    mic_src, sys_src = default_sources()
    print("\nRecommended .env (auto-detected):")
    print(f"  MIC_SOURCE={mic_src or ''}      # your mic = YOU")
    print(f"  SYS_SOURCE={sys_src or ''}      # speakers monitor = INTERVIEWER")
    print(
        "\nLeave MIC_SOURCE/SYS_SOURCE blank to let the daemon auto-detect these "
        "defaults at launch."
    )


def cmd_check_audio(args):
    """Capture a few seconds and report capture level so the user can verify
    their mic + system-monitor are actually picking up real audio (play the
    interview/video while this runs)."""
    import numpy as np

    from audio import AudioCapture, default_sources

    mic_source = config.MIC_SOURCE or None
    sys_source = config.SYS_SOURCE or None
    auto_mic, auto_sys = default_sources()
    mic_source = mic_source or auto_mic
    sys_source = sys_source or auto_sys

    secs = 6
    print(f"Mic source (YOU)         : {mic_source}")
    print(f"System source (INTERVIEWER): {sys_source}")
    print(f"\nPlay your interview/video now. Capturing {secs}s ...")

    cap = AudioCapture(mic_source=mic_source, sys_source=sys_source)
    chunks = []
    cap.get_stereo_callback()(lambda b: chunks.append(b))
    cap.start()
    time.sleep(secs)
    cap.stop()

    a = np.frombuffer(b"".join(chunks), dtype=np.int16)
    if a.size == 0:
        print("\nNo audio captured at all - check that pw-record is installed.")
        return
    for label, ch in (("YOU (mic)", a[0::2]), ("INTERVIEWER (system)", a[1::2])):
        peak = int(np.abs(ch).max())
        mean = int(np.abs(ch).mean())
        crest = (peak + 1) / (mean + 1)
        if mean < 30:
            verdict = "SILENT - this source is not capturing audio"
        elif crest > 40:
            verdict = "SPARSE/NOISY - monitor may not loop back playback (try another output device)"
        else:
            verdict = "OK - looks like real audio"
        print(f"  {label:22s} peak={peak:6d} mean={mean:5d} crest={crest:4.1f}  -> {verdict}")
    print(
        "\nTip: if the INTERVIEWER source is SILENT/SPARSE, switch your system "
        "output to a different device (e.g. built-in speakers/headphones) and "
        "re-run. Set SYS_SOURCE in .env to that device's .monitor, or leave it "
        "blank to auto-detect the default output."
    )


def cmd_check_screen(args):
    """Take a single screenshot and report whether screen capture works."""
    from screen import ScreenCapture

    config.ensure_dirs()
    cap = ScreenCapture()
    ok, info = cap.capture_once()
    if ok:
        print(f"Screen capture OK: {info}")
        print("Enable at runtime from the phone UI, or set SCREEN_CONTEXT=1 in .env")
    else:
        print(f"Screen capture FAILED: {info}")
        print("Install one of: cosmic-screenshot, grim, gnome-screenshot, scrot,")
        print('or set SCREEN_CAPTURE_CMD="<tool> {path}" in .env')


def cmd_calibrate(args):
    from calibration import run_calibration

    mic = args.mic if args.mic is not None else _int_or_none(os.environ.get("MIC_INDEX"))
    run_calibration(mic_index=mic)


def cmd_login(args):
    print("First-time setup")
    print("-" * 40)
    key = _ensure_server_key()
    print(f"Server key: {key} (saved to .env)")
    if not config.GEMINI_API_KEY:
        print("Reminder: set GEMINI_API_KEY in .env")
    if not config.DEEPGRAM_API_KEY:
        print("Reminder: set DEEPGRAM_API_KEY in .env")
    print("Run 'python3 main.py --list-devices' next, then './start.sh'.")


def _read_context_file(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:
        log(f"main: could not read context file {path}: {e}")
        return None


def _seed_context(args):
    """Load optional resume/jd/projects files into context_store before start."""
    if not (args.resume or args.jd or args.projects):
        return
    import context_store

    kwargs = {}
    if args.resume:
        text = _read_context_file(args.resume)
        if text is not None:
            kwargs["resume"] = text
    if args.jd:
        text = _read_context_file(args.jd)
        if text is not None:
            kwargs["jd"] = text
    if args.projects:
        text = _read_context_file(args.projects)
        if text is not None:
            kwargs["projects"] = text
    if kwargs:
        context_store.save(**kwargs)
        log(f"main: seeded context from CLI ({', '.join(kwargs.keys())})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _lan():
    from server import get_lan_ip

    return get_lan_ip()


def build_parser():
    p = argparse.ArgumentParser(description="Real-time AI Interview Assistant")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true", help="start daemon in background")
    g.add_argument("--stop", action="store_true", help="stop daemon")
    g.add_argument("--restart", action="store_true", help="restart daemon")
    g.add_argument("--status", action="store_true", help="show daemon status")
    g.add_argument("--list-devices", action="store_true", help="list audio devices")
    g.add_argument(
        "--check-audio",
        action="store_true",
        help="capture a few seconds and report whether sources pick up real audio",
    )
    g.add_argument(
        "--check-screen",
        action="store_true",
        help="take one screenshot and report backend, resolution and size",
    )
    g.add_argument("--calibrate", action="store_true", help="run voice calibration")
    g.add_argument("--login", action="store_true", help="first-time setup")

    p.add_argument("--mic", type=int, default=None, help="mic device index")
    p.add_argument("--sys", type=int, default=None, help="system audio device index")
    p.add_argument(
        "--ai",
        choices=["groq", "claude", "chatgpt"],
        default=None,
        help="fallback AI preference",
    )
    p.add_argument("--resume", type=str, default=None, help="path to resume .txt/.md file")
    p.add_argument("--jd", type=str, default=None, help="path to job description file")
    p.add_argument(
        "--projects",
        type=str,
        default=None,
        help="path to key projects file",
    )
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.stop:
        cmd_stop(args)
    elif args.restart:
        cmd_restart(args)
    elif args.status:
        cmd_status(args)
    elif args.list_devices:
        cmd_list_devices(args)
    elif args.check_audio:
        cmd_check_audio(args)
    elif args.check_screen:
        cmd_check_screen(args)
    elif args.calibrate:
        cmd_calibrate(args)
    elif args.login:
        cmd_login(args)
    elif args.start:
        cmd_start(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
