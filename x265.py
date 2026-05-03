#!/usr/bin/env python3

import os
import sys
import subprocess
import re
import time
import math
import shutil
import json
import argparse
import tempfile
from collections import deque
import logging
from logging.handlers import RotatingFileHandler

# Default settings for the script. You can change these or use command-line arguments to override them.
#================================================
# Process priority tuning
# The script can lower the CPU scheduling priority of ffmpeg/x265 so encoding
# uses idle cycles more politely. This has no effect on encode quality, only on
# how aggressively the process competes with other system tasks.
# Higher nice values mean lower priority; range is -20 (highest) to 19 (lowest).
NICE = 19


# ffmpeg variables to change
#================================================
# The height of the desired video. Use 0 or the actual height for no scaling.
# Some Scale values might include
#SCALE = 2160 # (typically CRF 28-32)        3840×2160 4KUHD
#SCALE = 1080 # (typically CRF 32-36)        1920x1080 1080p
#SCALE = 720  # (typically CRF 36-40)        1280x720  720p
#SCALE = 576  # (typically CRF 42 or higher) 720×576   576p
#SCALE = 480  # (typically CRF 44 or higher) 720x480   480p Mobile Devices (Tiny File Sizes)
#SCALE = 360  # (typically CRF 46 or higher) 640x360   360p Mobile Devices (Tiny File Sizes)
SCALE = 0
#================================================
# Higher CRF values will result in a final output that takes less space
# but begin to lose detail. Lower CRF values retain more detail at the cost of larger file sizes.
# Range (0-51)
CRF = 30
#================================================
# This preset parameter governs the efficiency/encode-time trade-off.
# Faster presets encode more quickly with lower CPU work, while slower presets
# increase compression efficiency and quality for a given bitrate/CRF.
# Range (ultrafast..veryslow)
PRESET_X265 = 'fast'
X265_PRESET_NAMES = ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow']
#================================================
# Adaptive quantization helps preserve perceptual quality by allocating bits
# more aggressively to visually important or complex regions.
# AQ mode:
#   0 = off
#   1 = biased adaptive quantization (default)
#   2 = variance AQ
#   3 = auto-variance AQ
AQ_MODE = 1
# AQ strength controls how strong the effect is. Higher values emphasize
# perceptually important areas more, but can distort flat regions if set too high.
# Range 0.0..3.0
AQ_STRENGTH = 1.0
#================================================
# Log path constants
LOG_DIR = os.path.expanduser('~/x265_log')
LOG_FILE_NAME = 'x265.log'
#================================================
# Directory traversal depth for batch mode.
# 0 means only top-level files.
DEPTH = 0
#================================================

def parse_arguments():
    class ArgumentDefaultsAndRawHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        pass

    def ranged_int(min_val, max_val):
        """Return an argparse type function that accepts only integers in [min_val, max_val]."""
        def _type(value):
            ivalue = int(value)
            if not (min_val <= ivalue <= max_val):
                raise argparse.ArgumentTypeError(f"{ivalue} is out of range ({min_val}-{max_val})")
            return ivalue
        return _type

    def ranged_float(min_val, max_val):
        """Return an argparse type function that accepts only floats in [min_val, max_val]."""
        def _type(value):
            fvalue = float(value)
            if not (min_val <= fvalue <= max_val):
                raise argparse.ArgumentTypeError(f"{fvalue} is out of range ({min_val}-{max_val})")
            return fvalue
        return _type

    parser = argparse.ArgumentParser(
        description="Encode video using x265 with optional process priority tuning and scaling.",
        formatter_class=ArgumentDefaultsAndRawHelpFormatter,
        epilog="""Examples:
  x265.py /path/to/input.mp4 /path/to/output_dir
  x265.py /path/to/source_dir /path/to/dest_dir --scale 720 --crf 28 --preset medium --nice 15
  x265.py /path/to/input.mkv /path/to/output --nice 19
"""
    )
    parser.add_argument('source', help='Single video file or directory containing video files.')
    parser.add_argument('dest', help='Destination directory for encoded files.')
    parser.add_argument('--nice', type=ranged_int(-20, 19), default=NICE,
                        help='Process nice value for ffmpeg/x265. Higher values reduce priority.')
    parser.add_argument('--scale', type=int, default=SCALE, choices=[0, 360, 480, 576, 720, 1080, 2160],
                        help='Output height in pixels (0 means no scaling).')
    parser.add_argument('--crf', type=ranged_int(0, 51), default=CRF,
                        help='CRF value for x265 (0-51).')
    parser.add_argument('--preset', choices=X265_PRESET_NAMES, default=PRESET_X265,
                        help='x265 preset name. Faster presets use less CPU but encode faster; slower presets improve compression efficiency.')
    parser.add_argument('--aq-mode', type=ranged_int(0, 3), default=AQ_MODE,
                        help='x265 AQ mode (0=off, 1=biased, 2=variance, 3=auto-variance).')
    parser.add_argument('--aq-strength', type=ranged_float(0.0, 3.0), default=AQ_STRENGTH,
                        help='x265 AQ strength (0.0-3.0). Higher values increase perceptual AQ effect.')
    parser.add_argument('--depth', type=int, default=DEPTH,
                        help='Directory traversal depth for batch mode. 0 means only top-level files.')
    return parser.parse_args()


def _iter_lines_raw(fd):
    """Read from a file descriptor using os.read (bypasses all Python buffering).
    Splits on \\r or \\n so ffmpeg's carriage-return progress lines are yielded immediately."""
    buf = ''
    while True:
        try:
            raw = os.read(fd, 512)
        except OSError:
            break
        if not raw:
            break
        buf += raw.decode('utf-8', errors='replace')
        while True:
            cr = buf.find('\r')
            nl = buf.find('\n')
            # pick whichever separator comes first
            if cr == -1 and nl == -1:
                break
            if cr == -1:
                idx, skip = nl, 1
            elif nl == -1:
                idx, skip = cr, 1
            else:
                # Consume \r\n as a single separator to avoid a spurious extra iteration
                if cr < nl:
                    idx, skip = cr, 2 if nl == cr + 1 else 1
                else:
                    idx, skip = nl, 1
            line = buf[:idx]
            buf = buf[idx + skip:]
            if line.strip():
                yield line
    if buf.strip():
        yield buf


# Log to Screen and or File
def setup_dual_logging():
    log_dir = LOG_DIR
    log_path = os.path.join(log_dir, LOG_FILE_NAME)

    # Ensure log directory exists
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"Could not create log directory {log_dir}: {e}", file=sys.stderr)
        return None

    # === FILE-ONLY LOGGER ===
    file_logger = logging.getLogger('file_logger')
    file_logger.setLevel(logging.DEBUG)
    file_logger.propagate = False  # Prevent going to root

    # Clear any existing handlers
    file_logger.handlers.clear()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)

    file_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    file_logger.addHandler(file_handler)

    # Silence the root logger completely
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.CRITICAL + 1)

    return file_logger


file_logger = None

# Color definitions
class colors:
    # Foreground colors (bold or normal)
    BoRed = "\033[1;31m"        # Bold Red
    Bogreen = "\033[1;38;5;28m" # Bold Green (using 256-color mode)
    green = "\033[38;5;28m"     # Green (using 256-color mode)
    black = "\033[38;5;3m"      # NOTE: palette index 3 renders as yellow on most terminals (misleadingly named)
    red = "\033[31m"            # Red
    white = "\033[97m"          # White
    blue = "\033[38;5;21m"      # Blue (using 256-color mode)
    skyblue = "\033[38;5;25m"   # SkyBlue (using 256-color mode)
    pink = "\033[38;5;200m"     # Pink (using 256-color mode)
    Lblue = "\033[38;5;117m"    # Light Blue (using 256-color mode)
    orange = "\033[38;5;202m"   # Orange (using 256-color mode)
    Lorange = "\033[38;5;215m"  # Light Orange (using 256-color mode)
    yellow = "\033[38;5;190m"   # Yellow (using 256-color mode)
    lmagenta = "\033[38;5;95m"  # Light Magenta (using 256-color mode)
    rblue = "\033[38;5;63m"     # RoyalBlue (using 256-color mode)
    gray  = "\033[38;5;240m"    # Dark Gray (using 256-color mode)
    
    # Background colors
    blpurple = "\033[48;5;99m"    # Background Light Purple (using 256-color mode)
    byellow = "\033[48;5;11m"     # Background Yellow (using 256-color mode)
    bmagenta = "\033[48;5;5m"     # Background Magenta (using 256-color mode)
    bred = "\033[48;5;9m"         # Background Red (using 256-color mode)
    bgreen = "\033[48;5;28m"      # Background Green (using 256-color mode)
    bskyblue = "\033[48;5;27m"    # Background SkyBlue (using 256-color mode)
    borangered = "\033[48;5;203m" # Background OrangeRed (using 256-color mode)
    
    # Reset color to default
    reset = "\033[0m"


def run_ffmpeg_with_size_estimation(cmd, original_duration, original_size, output_file, file_num=1, total_files=1):
    """Run ffmpeg command with real-time feedback and file size estimation."""
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    print(f"{colors.orange}Processing with nice value {NICE}{colors.reset}", flush=True)

    N_LINES = 12
    # Reserve vertical space for the in-place display block
    sys.stdout.write('\n' * N_LINES)
    sys.stdout.flush()

    speed_history = deque(maxlen=10)
    last_update_time = 0.0
    all_output = []

    for line in _iter_lines_raw(process.stdout.fileno()):
        all_output.append(line)
        if 'frame=' not in line:
            continue

        try:
            frame_match   = re.search(r'frame=\s*(\d+)', line)
            time_match    = re.search(r'time=\s*(\d+:\d+:\d+\.\d+)', line)
            fps_match     = re.search(r'fps=\s*(\d+\.?\d*)', line)
            q_match       = re.search(r'q=\s*(\d+\.?\d*)', line)
            size_match    = re.search(r'size=\s*(\S+)', line)
            bitrate_match = re.search(r'bitrate=\s*(\S+)', line)
            speed_match   = re.search(r'speed=\s*(\d+\.?\d*)x', line)

            if not all((frame_match, fps_match, q_match, size_match)):
                continue

            total_frames = int(frame_match.group(1))
            if time_match:
                h, m, s = map(float, time_match.group(1).split(':'))
                current_duration = h * 3600 + m * 60 + s
                time_str = time_match.group(1).rsplit('.', 1)[0]
            else:
                current_duration = 0.0
                time_str = 'N/A'

            fps     = int(float(fps_match.group(1)))
            q       = q_match.group(1)
            fmt_sz  = size_match.group(1)
            bitrate = bitrate_match.group(1) if bitrate_match else 'N/A'
            speed   = float(speed_match.group(1)) if speed_match else 0.0

            if speed > 0:
                speed_history.append(speed)

            current_size = os.path.getsize(output_file) if os.path.exists(output_file) else 0

            if current_duration > 0 and original_duration > 0:
                estimated_size   = (current_size / current_duration) * original_duration
                avg_spd          = sum(speed_history) / len(speed_history) if speed_history else speed
                remaining_secs   = (original_duration - current_duration) / avg_spd if avg_spd > 0 else None
                percent_complete = (current_duration / original_duration) * 100
            else:
                estimated_size   = 0
                remaining_secs   = None
                percent_complete = 0.0

            rt = (f"{int(remaining_secs // 3600):02}:{int((remaining_secs % 3600) // 60):02}:{int(remaining_secs % 60):02}"
                  if remaining_secs is not None else "Calculating...")
            eta = (time.strftime('%r', time.localtime(time.time() + remaining_secs))
                   if remaining_secs is not None else "--:--")

            bar_width = 30
            filled = int(bar_width * percent_complete / 100)

            display_lines = [
                f"{'Frame':.<17}: {colors.Lblue}{total_frames}{colors.reset}",
                f"{'FPS':.<17}: {colors.Lblue}{fps}{colors.reset}",
                f"{'Q':.<17}: {colors.Lblue}{int(float(q))}{colors.reset}",
                f"{'Size':.<17}: {colors.Lblue}{fmt_sz}{colors.reset}",
                f"{'Time':.<17}: {colors.Lblue}{time_str}{colors.reset}",
                f"{'Bitrate':.<17}: {colors.Lblue}{bitrate}{colors.reset}",
                f"{'Speed':.<17}: {colors.Lblue}{speed:.1f}x{colors.reset}",
                f"{'Percent Complete':.<17}: {colors.yellow}{percent_complete:.1f}%{colors.reset}",
                f"{'Est Final Size':.<17}: {colors.pink}{format_bytes(estimated_size)}{colors.reset}",
                f"{'Time Remaining':.<17}: {colors.yellow}{rt}{colors.reset}",
                f"{'Time At End':.<17}: {colors.yellow}{eta}{colors.reset}",
                f"{'Progress':.<17}: {colors.green}{'█' * filled}{colors.gray}{'░' * (bar_width - filled)}{colors.reset} {colors.yellow}{percent_complete:.1f}%{colors.reset}",
            ]

            if time.time() - last_update_time >= 1.0:
                # Move cursor up N_LINES, then overwrite each line in-place
                out = f'\033[{N_LINES}A'
                for dl in display_lines:
                    out += f'\033[2K{dl}\n'
                sys.stdout.write(out)
                sys.stdout.flush()
                last_update_time = time.time()

        except Exception:
            continue

    process.wait()
    # Move cursor back up over: 12 display lines + 1 pin-cores line + 2 audio info lines = N_LINES + 3
    sys.stdout.write(f'\033[{N_LINES + 3}A\033[J')
    sys.stdout.flush()
    return process.returncode, '\n'.join(all_output)

def get_video_duration(file_path):
    """Get video duration in seconds."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
        duration = result.stdout.decode('utf-8', errors='replace').strip()
        if not duration:
            # Fallback: try reading duration from the first video stream
            fallback = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
            )
            duration = fallback.stdout.decode('utf-8', errors='replace').strip()
        if duration:
            try:
                return float(duration)
            except ValueError:
                print(f"{colors.yellow}Error: Duration '{duration}' from ffprobe is not a valid number. Script will exit.{colors.reset}", file=sys.stderr)
                sys.exit(1)
        else:
            stderr_msg = result.stderr.decode('utf-8', errors='replace').strip()
            print(f"{colors.yellow}Error: Could not find video duration. Script will exit.{colors.reset}", file=sys.stderr)
            if stderr_msg:
                print(f"{colors.yellow}ffprobe stderr: {stderr_msg}{colors.reset}", file=sys.stderr)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"{colors.yellow}Error: ffprobe timed out trying to get video duration. Script will exit.{colors.reset}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"{colors.yellow}Error: An unexpected error occurred while getting video duration. Script will exit.\nError: {e}{colors.reset}", file=sys.stderr)
        sys.exit(1)


def get_video_resolution(file_path):
    """Get video resolution."""
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', 
                                 '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', file_path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"{colors.yellow}Warning: ffprobe timed out trying to get video resolution. Using default value.{colors.reset}")
        return "Unknown"
    except Exception as e:
        print(f"{colors.yellow}Warning: An error occurred while getting video resolution. Using default value.\nError: {e}{colors.reset}")
        return "Unknown"

def get_audio_channels(file_path):
    """Return the number of channels in the first audio stream (e.g. 2 for stereo, 6 for 5.1)."""
    try:
        result = subprocess.run(['ffprobe', '-v', 'quiet', '-show_streams', '-select_streams', 'a:0', 
                                 '-print_format', 'json', file_path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        audio_info = json.loads(result.stdout)
        if 'streams' in audio_info:
            for stream in audio_info['streams']:
                if stream['codec_type'] == 'audio':
                    return int(stream.get('channels', 2))
        print(f"{colors.yellow}Warning: Could not find audio channel information. Defaulting to stereo.{colors.reset}")
        return 2
    except subprocess.TimeoutExpired:
        print(f"{colors.yellow}Warning: ffprobe timed out trying to get audio channels. Defaulting to stereo.{colors.reset}")
        return 2
    except Exception as e:
        print(f"{colors.yellow}Warning: An error occurred while getting audio channels. Defaulting to stereo.\nError: {e}{colors.reset}")
        return 2


def get_media_info(file_path):
    """Return (video_codec, audio_codec) for the first non-cover-art video and first audio stream."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_streams', '-print_format', 'json', file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
        )
        streams = json.loads(result.stdout).get('streams', [])
        video_codec = next(
            (s.get('codec_name', 'Unknown') for s in streams
             if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic', 0)),
            'Unknown'
        )
        audio_codec = next(
            (s.get('codec_name', 'Unknown') for s in streams if s.get('codec_type') == 'audio'),
            'Unknown'
        )
        return video_codec, audio_codec
    except subprocess.TimeoutExpired:
        print(f"{colors.yellow}Warning: ffprobe timed out getting media info.{colors.reset}")
        return 'Unknown', 'Unknown'
    except Exception:
        return 'Unknown', 'Unknown'


def has_chapters(file_path):
    """Return True if the file contains at least one chapter."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_chapters', '-print_format', 'json', file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
        )
        return len(json.loads(result.stdout).get('chapters', [])) > 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def has_subtitle_streams(file_path):
    """Return True if the input file contains subtitle streams."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index', '-of', 'csv=p=0', file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
        )
        return bool(result.stdout.strip())
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def get_subtitle_codecs(file_path):
    """Return a list of subtitle codec names present in the file (lowercase)."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-select_streams', 's',
             '-show_entries', 'stream=codec_name', '-print_format', 'json', file_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
        )
        streams = json.loads(result.stdout).get('streams', [])
        return [s.get('codec_name', '').lower() for s in streams if s.get('codec_name')]
    except Exception:
        return []


def _parse_srt_timestamp(timestamp):
    h, m, s_ms = timestamp.split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _format_srt_timestamp(seconds):
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _merge_incremental_srt_cues(cues):
    """Merge incremental or partial subtitle cues into larger, stable cues."""
    merged = []
    for cue in cues:
        if not cue['text'] or all(line.strip() == '' for line in cue['text']):
            continue
        if not merged:
            merged.append(cue)
            continue

        prev = merged[-1]
        start_diff = cue['start'] - prev['end']
        prev_text = ' '.join(prev['text']).strip()
        cur_text = ' '.join(cue['text']).strip()

        if start_diff <= 0.05 and cur_text.startswith(prev_text):
            prev['end'] = max(prev['end'], cue['end'])
            prev['text'] = cue['text'] if len(cur_text) >= len(prev_text) else prev['text']
            continue
        if start_diff <= 0.05 and prev_text.startswith(cur_text):
            prev['end'] = max(prev['end'], cue['end'])
            continue

        merged.append(cue)

    return merged


def detect_crop(file_path, duration):
    start_crop = math.floor(duration * 0.1)  # Start crop detection at 10% of video length

    cmd = [
        'ffmpeg',
        '-ss', str(start_crop),
        '-i', file_path,
        '-t', '00:00:30',
        '-vf', 'cropdetect=24:16:0',
        '-f', 'null',
        '-'
    ]

    with subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        encoding='utf-8',
        errors='replace'
    ) as proc:

        crop = None
        for line in proc.stderr:
            m = re.search(r'crop=(\d+:\d+:\d+:\d+)', line)
            if m:
                crop = m.group(1)

        proc.wait()
        return crop



def get_attachment_stream(input_file):
    ffprobe_cmd = ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', input_file]
    # Use universal_newlines with utf-8 encoding and specify how to handle errors
    process = subprocess.Popen(ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8', errors='replace')
    stdout, stderr = process.communicate()

    if process.returncode == 0:
        try:
            # JSON parsing will now work with 'replace' errors, replacing invalid bytes with a replacement character
            streams = json.loads(stdout).get('streams', [])
            for stream in streams:
                if 'disposition' in stream and stream['disposition'].get('attached_pic', False):
                    return int(stream['index'])
        except json.JSONDecodeError as e:
            print(f"Failed to decode JSON: {e}")
            print(f"Raw output: {stdout}")
    else:
        print(f"ffprobe command failed with error:\n{stderr}")
    
    return None


def select_audio_settings(audio_channels, scale):
    """Return the ffmpeg audio settings list based on channel count and output scale."""
    # ----- 5.1 Opus (high-quality) -----
    if audio_channels >= 6 and scale not in [360, 480, 576]:
        print(f"{colors.yellow}5.1 Audio Detected\n{colors.green}"
              f"Target Video File will have 5.1 Opus Audio Stream{colors.reset}")
        return [
            '-c:a', 'libopus',
            '-b:a', '256k',
            '-vbr', 'on',
            '-ac', '6',
            '-filter:a', 'aformat=channel_layouts=5.1',
        ]
    # ----- Mobile (low-bitrate stereo) -----
    elif scale in [360, 480, 576]:
        print(f"{colors.yellow}Stereo Audio Selected\n{colors.green}Target Video File will have Low bitrate Stereo Opus Audio Stream for Mobile Device{colors.reset}")
        return [
            '-c:a', 'libopus',
            '-b:a', '32k',
            '-vbr', 'on',
            '-ac', '2',
        ]
    # ----- Normal stereo (default) -----
    else:
        print(f"{colors.yellow}Stereo Audio Detected\n{colors.green}Target Video File will have Stereo Opus Audio Stream{colors.reset}")
        return [
            '-c:a', 'libopus',
            '-b:a', '128k',
            '-vbr', 'on',
            '-ac', '2',
        ]


def build_video_filter(crop, scale):
    """Return the ffmpeg filter_complex string for video processing."""
    if scale == 0:
        # No scaling requested – pass video through unchanged (crop only if detected)
        if crop:
            return f"[v:0]crop={crop},setsar=1:1[v]"
        else:
            return "[v:0]copy[v]"
    if crop:
        return f"[v:0]crop={crop},scale=-8:{scale}:flags=lanczos,setsar=1:1[v]"
    else:
        return f"[v:0]scale=-8:{scale}:flags=lanczos,setsar=1:1[v]"


def encode_video(source, dest, filename, file_num=1, total_files=1):
    """Encode video using ffmpeg and then edit MKV properties."""
    encoding_started = False
    try:
        input_file = os.path.join(source, filename) if source != filename else filename
        if SCALE == 0:
            output_file = os.path.join(dest, f"{os.path.splitext(filename)[0]}-x265-No-Scaling.mkv")
        else:
            output_file = os.path.join(dest, f"{os.path.splitext(filename)[0]}-x265-{SCALE}p.mkv")

        # Check if input file exists and is readable (must be before get_video_duration)
        if not os.path.isfile(input_file):
            print(f"{colors.red}Error: Input file '{input_file}' does not exist or is not readable.{colors.reset}")
            return

        # Check if output file already exists (skip early before probing)
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            print(f"{colors.red}Existing Destination File Found:{colors.reset}")
            print(f"{colors.orange}{output_file}{colors.reset}")
            print(f"{colors.yellow}Skipping\n\n{colors.reset}")
            print('=' * 80)
            return

        output_dir = os.path.dirname(output_file)
        if output_dir and not os.path.isdir(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # Determine source duration.
        video_duration = get_video_duration(input_file)
    
        # Check if destination directory is writable
        if not os.access(dest, os.W_OK):
            print(f"{colors.red}Error: Destination directory '{dest}' is not writable.{colors.reset}")
            return
    
        input_res = get_video_resolution(input_file)
        audio_channels = get_audio_channels(input_file)
        video_codec, audio_codec = get_media_info(input_file)
        preserve_subtitles = has_subtitle_streams(input_file)
        file_has_chapters = has_chapters(input_file)
        crop = detect_crop(input_file, video_duration)
        if crop:
            print(f"{colors.Lblue}Crop Detected..........:\t{colors.yellow}{crop}{colors.reset}")
        else:
            print(f"{colors.Lblue}Crop Detected..........:\t{colors.gray}None{colors.reset}")

        basename = os.path.basename(input_file)
        input_size = os.path.getsize(input_file)

        print(f"{colors.blue}Input File.............:\t{basename}{colors.reset}")
        print(f"{colors.rblue}Destination............:\t{output_file}{colors.reset}")
        print(f"{colors.bskyblue}Input Resolution.......:\t{input_res}{colors.reset}")
        if SCALE == 2160:
            print(f"{colors.bgreen}Output Resolution......:\t4K{colors.reset}")
        elif SCALE == 1080:
            print(f"{colors.bgreen}Output Resolution......:\t1080p{colors.reset}")
        elif SCALE == 720:
            print(f"{colors.bgreen}Output Resolution......:\t720p{colors.reset}")
        elif SCALE == 576:
            print(f"{colors.bgreen}Output Resolution......:\t576p for Mobile Devices{colors.reset}")
        elif SCALE == 480:
            print(f"{colors.bgreen}Output Resolution......:\t480p for Mobile Devices{colors.reset}")
        elif SCALE == 360:
            print(f"{colors.bgreen}Output Resolution......:\t360p for Mobile Devices{colors.reset}")
        elif SCALE == 0:
            print(f"{colors.bgreen}Output Resolution......:\tNo Scaling Selected. Output resolution will be the same as input{colors.reset}")

        print(f"{colors.Lblue}Input File Size........:\t{format_bytes(input_size)}{colors.reset}")
        print(f"{colors.Lblue}Input Video Codec......:\t{video_codec.upper()}{colors.reset}")
        print(f"{colors.Lblue}Input Audio Codec......:\t{audio_codec.upper()}{colors.reset}")
        print(f"{colors.Lblue}Input Subtitles........:\t{'Yes' if preserve_subtitles else 'No'}{colors.reset}")
        print(f"{colors.Lblue}Input Chapters.........:\t{'Yes' if file_has_chapters else 'No'}{colors.reset}")
        if video_duration == 0:
            print(f"{colors.Lblue}Video Duration.........:\t{colors.yellow}Unknown{colors.reset}")
        else:
            print(f"{colors.Lblue}Video Duration.........:\t{time.strftime('%-H Hrs %-M Min %-S Sec', time.gmtime(video_duration))}{colors.reset}")
        print(f"{colors.pink}CRF Value..............:\t{CRF}{colors.reset}")
        print(f"{colors.pink}Preset Type............:\t{PRESET_X265}{colors.reset}")
        
        start_time = time.time()
        print(f"{colors.orange}Start Time.............:\t{time.strftime('%a %d %b %Y  %r', time.localtime(start_time))}{colors.reset}")
        
        audio_settings = select_audio_settings(audio_channels, SCALE)
        video_filter = build_video_filter(crop, SCALE)
        subtitle_codecs = get_subtitle_codecs(input_file) if preserve_subtitles else []
        # mov_text / tx3g are MP4-only subtitle formats; MKV requires conversion to srt
        mkv_incompatible_subs = {'mov_text', 'tx3g'}
        needs_sub_conversion = preserve_subtitles and bool(set(subtitle_codecs) & mkv_incompatible_subs)
        if preserve_subtitles:
            if needs_sub_conversion:
                print(f"{colors.yellow}Subtitle streams detected (mov_text/tx3g → converting to SRT for MKV).{colors.reset}")
            else:
                print(f"{colors.yellow}Subtitle streams detected and will be preserved.{colors.reset}")

        # -------------------------------------------------
        # BUILD FFMPEG COMMAND
        # -------------------------------------------------
        cmd = [
            'ffmpeg', '-y',
            '-i', input_file,

            '-filter_complex', video_filter,
            '-map', '[v]',

            '-map', '0:a:0',    # First audio
            '-map', '0:s?',     # All subtitles (optional)

            '-map_metadata', '0',
            '-map_chapters', '0',

            '-c:v', 'libx265',
            '-pix_fmt', 'yuv420p10le',
            '-preset', PRESET_X265,
            '-crf', str(CRF),
            '-x265-params', f"aq-mode={AQ_MODE}:aq-strength={AQ_STRENGTH}",
            '-g', '120',
        ]

        if preserve_subtitles:
            if needs_sub_conversion:
                cmd += ['-c:s', 'srt']
            else:
                cmd += ['-c:s', 'copy']

        prefix = ['nice', '-n', str(NICE), 'ionice', '-c3']
        cmd = prefix + cmd
        
        
        
        
        
        
        # --- Cover art ---
        attachment_stream = get_attachment_stream(input_file)
        if attachment_stream is not None:
            cmd.extend([
                '-map', f'0:{attachment_stream}',
                '-c:v:1', 'copy',
                '-disposition:v:1', 'attached_pic'
            ])
        #    print(attachment_stream)

        #else:
        #    print(f"{colors.yellow}Warning: No attachment (cover art) stream found in the input file.{colors.reset}")

        cmd += audio_settings + [output_file]

        # Get original duration and size for size prediction
        original_duration = video_duration if isinstance(video_duration, float) else 0
        original_size = os.path.getsize(input_file)

        encoding_started = True
        return_code, stderr = run_ffmpeg_with_size_estimation(cmd, original_duration, original_size, output_file, file_num, total_files)
        if return_code != 0:
            print(f"{colors.red}FFmpeg encoding failed with error code {return_code}{colors.reset}")
            print(f"{colors.red}FFmpeg Error Details:\n{stderr}{colors.reset}")
            subprocess.run(["tput", "cnorm"])
            subprocess.run(["tput", "sgr0"])
            return
    
        elapsed_time = time.time() - start_time
        # When subtitle notice text is printed before the ffmpeg progress block,
        # there is one extra terminal line above the progress display that must be
        # removed after ffmpeg finishes.
        subtitle_notice_lines = 1 if preserve_subtitles else 0
        if subtitle_notice_lines:
            sys.stdout.write(f'\033[{subtitle_notice_lines}A\033[J')
            sys.stdout.flush()
        print(f"{colors.orange}End Time...............:\t{time.strftime('%a %d %b %Y  %r', time.localtime())}{colors.reset}")
        print(f"{colors.yellow}Processing Time........:\t{time.strftime('%-H Hr %-M Min %-S Sec', time.gmtime(elapsed_time))}{colors.reset}")
    
        # Add mkvpropedit command after encoding
        if os.path.exists(output_file):
            subprocess.run(["tput", "blink"])
            #print(f"{colors.pink}mkvpropedit --add-track-statistics-tags {os.path.basename(output_file)}{colors.reset}")
            print(f"{colors.pink}Refreshing Tags and Stats....{colors.reset}")
            subprocess.run(["tput", "sgr0"])
            mkvpropedit_cmd = ['mkvpropedit', output_file, '--add-track-statistics-tags']
            mkvpropedit_process = subprocess.Popen(mkvpropedit_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            _, mkvpropedit_stderr = mkvpropedit_process.communicate()
            if mkvpropedit_process.returncode != 0:
                subprocess.run(["tput", "cnorm"])
                subprocess.run(["tput", "sgr0"])
                print(f"{colors.red}mkvpropedit failed with error code {mkvpropedit_process.returncode}{colors.reset}")
                print(f"{colors.red}mkvpropedit Error Details:\n{mkvpropedit_stderr}{colors.reset}")
            reduce_file_size(input_file, output_file, video_duration, start_time, elapsed_time, input_res, SCALE, video_codec, audio_codec, preserve_subtitles, file_has_chapters)

        else:
            print(f"{colors.red}Output file was not created, skipping size reduction{colors.reset}")
         
            

    except KeyboardInterrupt:
        subprocess.run(["tput", "cnorm"])
        subprocess.run(["tput", "sgr0"])
        #subprocess.run(["tput", "rc"]) # restore saved cursor position
        subprocess.run(["tput", "cuu", "3"])
        subprocess.run(['tput', 'cr']) # Moves cursor to beginning of line
        subprocess.run(['tput', 'ed']) # Clears the screen below
        print(f"\n{colors.orange}*** CTRL+C Detected ***{colors.reset}")
        if encoding_started and 'output_file' in locals():
                print(f"{colors.orange}*** Removing Unfinished x265 Video ***{colors.reset}")
                print(f"{colors.red}  {output_file}{colors.reset}")
                try:
                    os.remove(output_file)
                except OSError:
                    pass
        sys.exit(1)

def reduce_file_size(input_file, output_file, video_duration, start_time, elapsed_time, input_res, scale, video_codec='Unknown', audio_codec='Unknown', preserve_subtitles=False, file_has_chapters=False):
    # Wait for mkvpropedit to finish writing (max ~10 seconds)
    final_size = 0
    for _ in range(50):
        try:
            current = os.path.getsize(output_file)
            time.sleep(0.2)
            if os.path.getsize(output_file) == current:
                final_size = current
                break
            final_size = current
        except OSError:
            time.sleep(0.5)

    start_size = os.path.getsize(input_file)
    reduction = (start_size - final_size) / start_size * 100

    subprocess.run(["tput", "cuu", "1"])
    subprocess.run(['tput', 'cr'])
    subprocess.run(['tput', 'ed'])
    print(f"{colors.green}Original File Size.....:\t{format_bytes(start_size)}{colors.reset}")
    print(f"{colors.green}New File Size..........:\t{format_bytes(final_size)}{colors.reset}")
    print(f"{colors.lmagenta}File Size Reduced by...:\t{reduction:.2f} Percent{colors.reset}")
    print('=' * 80)
    
    
    if scale == 0:
        SCALING = input_res
    else:
        SCALING = scale
    file_logger.info(f"{'='*50}")
    file_logger.info(f"Input File Name........:\t{input_file}")
    file_logger.info(f"Output File Name.......:\t{output_file}")
    file_logger.info(f"Input Resolution.......:\t{input_res}")
    file_logger.info(f"OutPut Resolution......:\t{SCALING}")
    file_logger.info(f"Input Video Codec......:\t{video_codec.upper()}")
    file_logger.info(f"Input Audio Codec......:\t{audio_codec.upper()}")
    file_logger.info(f"Input Subtitles........:\t{'Yes' if preserve_subtitles else 'No'}")
    file_logger.info(f"Input Chapters.........:\t{'Yes' if file_has_chapters else 'No'}")
    file_logger.info(f"Video Duration.........:\t{time.strftime('%-H Hrs %-M Min %-S Sec', time.gmtime(video_duration))}")
    file_logger.info(f"CRF Value..............:\t{CRF}")
    file_logger.info(f"Preset Value...........:\t{PRESET_X265}")
    file_logger.info(f"Start Time.............:\t{time.strftime('%a %d %b %Y  %r', time.localtime(start_time))}")
    file_logger.info(f"End Time...............:\t{time.strftime('%a %d %b %Y  %r', time.localtime())}")
    file_logger.info(f"Processing Time........:\t{time.strftime('%-H Hr %-M Min %-S Sec', time.gmtime(elapsed_time))}")
    file_logger.info(f"Input File Size........:\t{format_bytes(start_size)}")
    file_logger.info(f"New File Size..........:\t{format_bytes(final_size)}")
    file_logger.info(f"File Size Reduced by...:\t{reduction:.2f} Percent")
    file_logger.info(f"{'='*50}")

def format_bytes(size):
    """Convert bytes to human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"

def check_dependencies():
    """Check if required dependencies are installed."""
    dependencies = ['ffmpeg', 'ffprobe', 'mkvpropedit']
    for dep in dependencies:
        if not shutil.which(dep):
            print(f"{colors.BoRed}Error: {dep} is not installed or not in your path. {dep} is required.{colors.reset}")
            subprocess.run(["tput", "cnorm"])
            subprocess.run(["tput", "sgr0"])
            sys.exit(1)

def main():
    global file_logger, NICE, SCALE, CRF, PRESET_X265, AQ_MODE, AQ_STRENGTH

    args = parse_arguments()
    NICE = args.nice
    SCALE = args.scale
    CRF = args.crf
    PRESET_X265 = args.preset
    AQ_MODE = args.aq_mode
    AQ_STRENGTH = args.aq_strength

    # Clear screen and set up terminal
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    # Set up logging
    file_logger = setup_dual_logging()
    if not file_logger:
        sys.exit(1)
    # Hide cursor
    subprocess.run(["tput", "civis"])
    source = os.path.normpath(args.source)
    dest = os.path.normpath(args.dest)

    if args.depth < 0:
        print(f"{colors.BoRed}Error: --depth must be 0 or greater.{colors.reset}")
        sys.exit(1)

    if source == dest: # Prevent source and destination being the same path in any mode
        print(f"{colors.BoRed}Destination should not be the same as source directory: {dest}{colors.reset}\n"
        "If you want to have the files in the same directory use the single file mode.\n"
        "This is because when you try to use the same dest DIR as the Source in batch mode,\n"
        "if you stop and then restart the script the already encoded files will be encoded again\n"
        "By using a different Dest DIR in \"Batch Mode\" an already encoded file can be skipped."
        )
        subprocess.run(["tput", "sgr0"])
        subprocess.run(["tput", "cnorm"])
        sys.exit(1)
    
    if os.path.isdir(source) and os.path.isdir(dest):
        # Batch mode for directory encoding (x265)
        print(f"{colors.red}Batch mode for directory encoding (x265){colors.yellow} nice={NICE}{colors.reset}")
        if args.depth == 0:
            files = [f for f in os.listdir(source) if f.lower().endswith(('.avi', '.mp4', '.mkv', '.webm', '.flv', '.mov', '.wmv', '.m4v'))]
        else:
            root_depth = source.rstrip(os.path.sep).count(os.path.sep)
            files = []
            for root, dirs, filenames in os.walk(source):
                current_depth = root.count(os.path.sep) - root_depth
                if current_depth > args.depth:
                    dirs[:] = []
                    continue
                for f in filenames:
                    if f.lower().endswith(('.avi', '.mp4', '.mkv', '.webm', '.flv', '.mov', '.wmv', '.m4v')):
                        rel_path = os.path.relpath(os.path.join(root, f), source)
                        files.append(rel_path)
        files.sort()
        total_files = len(files) # Get the total number of files
        print(f"{colors.Bogreen}Files to be processed{colors.reset}")
        print('\n'.join(files))
        print('=' * 80)
    
    elif os.path.isfile(source) and os.path.isdir(dest):
        # Single file mode encoding (x265)
        print(f"{colors.red}Single File mode encoding (x265){colors.yellow} nice={NICE}{colors.reset}")
        files = [os.path.basename(source)]  # Filename only for the loop
        total_files = 1
        source = os.path.dirname(source)  # Adjust source to be the directory part for single file mode
        
        # Verify if destination is a valid directory
        if not os.path.isdir(dest):
            print(f"{colors.BoRed}Destination is not a valid directory: {dest}{colors.reset}")
            sys.exit(1)

    else:
        print(f"{colors.BoRed}Argument is not valid{colors.reset}")
        subprocess.run(["tput", "cnorm"])
        subprocess.run(["tput", "sgr0"])
        sys.exit(1)

    check_dependencies()

    for i, filename in enumerate(files, 1):
        print(f"{colors.pink}Processing File {i} of {total_files}{colors.reset}")
        # Pass variables as they were before, but source is adjusted for single file mode
        encode_video(source, dest, filename, i, total_files)
    subprocess.run(["tput", "cnorm"])
    subprocess.run(["tput", "sgr0"])

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        subprocess.run(["tput", "cnorm"])
        subprocess.run(["tput", "sgr0"])
        print(f"{colors.orange}*** CTRL+C Detected ***{colors.reset}")
        sys.exit(1)
