#!/usr/bin/python3
import curses
import pulsectl
import time
import threading
import numpy as np
import subprocess
import math
import os
import sys
import pyaudio
import wave
import json
import shutil
import copy

# ==========================================
# 1. SYSTEM INITIALIZATION & WORKSPACE
# ==========================================

if len(sys.argv) > 1:
    WORKSPACE_DIR = os.path.abspath(sys.argv[1])
else:
    WORKSPACE_DIR = "/tmp/pulselooper"

if os.path.exists(WORKSPACE_DIR):
    if not os.path.isdir(WORKSPACE_DIR):
        print(f"Error: '{WORKSPACE_DIR}' is not a directory.")
        sys.exit(1)
    if not os.path.exists(os.path.join(WORKSPACE_DIR, "session.json")) and len(os.listdir(WORKSPACE_DIR)) > 0:
        print(f"Error: Bad directory. '{WORKSPACE_DIR}' contains files but no session.json.")
        sys.exit(1)
else:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)

# OS-Level Stderr Redirection (Silences ALSA C-level spam)
debug_log_path = os.path.join(WORKSPACE_DIR, "debug.log")
debug_log_fd = os.open(debug_log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
os.dup2(debug_log_fd, sys.stderr.fileno())

try:
    with pulsectl.Pulse('rate-detect') as p:
        server_info = p.server_info()
        SYSTEM_RATE = server_info.default_sample_spec.rate
except Exception:
    SYSTEM_RATE = 44100 

GLOBAL_CONFIG_PATH = os.path.expanduser("~/.pulselooper")

README_PATH = os.path.join(WORKSPACE_DIR, "README.md")
with open(README_PATH, "w") as f:
    f.write("# PulseLooper Audio Buffers\n\n")
    f.write("This directory contains the raw audio buffers recorded by PulseLooper.\n\n")
    f.write("## Audio Format Specifications\n")
    f.write("- **Format:** 32-bit Float Little-Endian (`float32le`)\n")
    f.write(f"- **Sample Rate:** `{SYSTEM_RATE}` Hz (Auto-detected)\n")
    f.write("- **Channels:** 2 (Stereo Normalized)\n")

# ==========================================
# 2. AUDIO ENGINE & CLOCK (PHASE-LOCKED)
# ==========================================

class TempoClock:
    def __init__(self, bpm, buffers):
        self.bpm = bpm
        self.last_bpm = None
        self.buffers = buffers 
        
        self.global_playing = True
        self.absolute_beat = 0
        self.current_beat = 1
        self.beats_per_measure = 4
        self.time_sig_string = "4/4"
        self.last_sig = None
        
        self.clock_comp_ms = 0  
        
        self.play_metronome = False
        self.metro_data = None
        
        self.buffer_data = {}
        self.playheads = {}
        
        self.global_frames = 0
        self.exact_global_beat = 0.0
        self.last_total_beats = -1
        self.last_downbeat_val = 0.0
        
        self._update_ticks()
        
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paFloat32,
            channels=2,
            rate=SYSTEM_RATE,
            output=True,
            frames_per_buffer=1024, 
            stream_callback=self._audio_callback
        )

    def start(self):
        self.stream.start_stream()
        
    def stop(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pa.terminate()

    def _update_ticks(self):
        frames_per_beat = int(SYSTEM_RATE * (60.0 / self.bpm))
        duration = 0.01 
        tick_frames = int(SYSTEM_RATE * duration)
        
        t = np.linspace(0, duration, tick_frames, False)
        envelope = np.exp(-t * 300) 
        
        silence_frames = max(0, frames_per_beat - tick_frames)
        silence = np.zeros(silence_frames, dtype=np.float32)
        
        audio_high = np.sin(1200 * t * 2 * np.pi) * envelope * 0.15 
        beat_high = np.concatenate((audio_high, silence)).astype(np.float32)
        
        audio_low = np.sin(600 * t * 2 * np.pi) * envelope * 0.15 
        beat_low = np.concatenate((audio_low, silence)).astype(np.float32)
        
        beat_high_stereo = np.column_stack((beat_high, beat_high))
        beat_low_stereo = np.column_stack((beat_low, beat_low))
        
        measure_data = [beat_high_stereo]
        for _ in range(self.beats_per_measure - 1):
            measure_data.append(beat_low_stereo)
            
        self.metro_data = np.concatenate(measure_data)
        self.last_bpm = self.bpm
        self.last_sig = self.time_sig_string

    def _load_buffer(self, buf_id):
        file_path = os.path.join(WORKSPACE_DIR, f"buffer_{buf_id:02d}.raw")
        if os.path.exists(file_path):
            try:
                data = np.fromfile(file_path, dtype=np.float32).reshape(-1, 2)
                self.buffer_data[buf_id] = data
                if buf_id not in self.playheads:
                    self.playheads[buf_id] = 0.0 
            except: pass

    def _audio_callback(self, in_data, frame_count, time_info, status):
        outdata = np.zeros((frame_count, 2), dtype=np.float32)
        
        if self.bpm != self.last_bpm or self.time_sig_string != self.last_sig:
            self._update_ticks()
            
        if not self.global_playing:
            return (outdata.tobytes(), pyaudio.paContinue)

        frames_per_beat_f = SYSTEM_RATE * 60.0 / self.bpm
        
        start_frame = self.global_frames
        self.global_frames += frame_count
        
        self.exact_global_beat = self.global_frames / frames_per_beat_f
        total_beats = int(self.exact_global_beat)
        
        self.absolute_beat = total_beats
        self.current_beat = (total_beats % self.beats_per_measure) + 1
        
        is_beat_trigger = (total_beats > self.last_total_beats)
        is_downbeat_trigger = (is_beat_trigger and self.current_beat == 1)
        
        if is_beat_trigger:
            self.last_total_beats = total_beats
            
        if is_downbeat_trigger:
            self.last_downbeat_val = float(total_beats)
            for buf in self.buffers:
                if buf["state"] == "QUEUED_PLAY":
                    buf["state"] = "PLAYING"
                    self._load_buffer(buf["id"])
                    self.playheads[buf["id"]] = 0.0 
                elif buf["state"] == "QUEUED_STOP":
                    buf["state"] = "STOPPED"
                    if buf["id"] in self.buffer_data:
                        del self.buffer_data[buf["id"]]
                        
        any_solo = any(b.get("soloed", False) for b in self.buffers if b["state"] in ["PLAYING", "QUEUED_STOP"])
                        
        for buf in self.buffers:
            if buf["state"] in ["PLAYING", "QUEUED_STOP"] and buf["id"] in self.buffer_data:
                data = self.buffer_data[buf["id"]]
                length = len(data)
                if length == 0: continue
                
                effective_vol = buf["vol"]
                if buf.get("muted", False):
                    effective_vol = 0.0
                elif any_solo and not buf.get("soloed", False):
                    effective_vol = 0.0
                
                recorded_bpm = buf.get("recorded_bpm", self.bpm)
                rec_frames_per_beat = SYSTEM_RATE * 60.0 / recorded_bpm
                
                beats_elapsed = self.exact_global_beat - buf.get("sync_beat", 0.0)
                ph_start_frames = beats_elapsed * rec_frames_per_beat
                
                offset_frames = int(SYSTEM_RATE * (buf.get("offset_ms", 0.0) / 1000.0))
                ratio = self.bpm / recorded_bpm
                
                out_indices = np.arange(frame_count)
                
                buf_indices = (ph_start_frames + offset_frames + out_indices * ratio).astype(int) % length
                
                track_audio = data[buf_indices] * effective_vol
                outdata += track_audio
                
                ph = (ph_start_frames + frame_count * ratio) % length
                self.playheads[buf["id"]] = ph
                
                current_visual_ph = (ph_start_frames + offset_frames + frame_count * ratio) % length
                buf["playhead_ratio"] = current_visual_ph / length
                
                if frame_count > 0:
                    if effective_vol > 0.0:
                        buf["peak"] = max(np.max(np.abs(track_audio)), buf.get("peak", 0)*0.7)
                    else:
                        buf["peak"] = buf.get("peak", 0)*0.7
                    
        if self.play_metronome and self.metro_data is not None:
            metro_len = len(self.metro_data)
            clock_delay_frames = int(SYSTEM_RATE * (self.clock_comp_ms / 1000.0))
            shifted_start_frame = start_frame - clock_delay_frames
            measure_start_beats = (shifted_start_frame // int(frames_per_beat_f)) % self.beats_per_measure
            frame_within_beat = shifted_start_frame % int(frames_per_beat_f)
            ph = int(measure_start_beats * frames_per_beat_f + frame_within_beat) % metro_len
            
            out_indices = np.arange(frame_count)
            metro_indices = (ph + out_indices) % metro_len
            outdata += self.metro_data[metro_indices]
            
        return (outdata.tobytes(), pyaudio.paContinue)

# ==========================================
# 3. RECORDING ENGINE (PAREC SUBPROCESS)
# ==========================================

class MonitorThread(threading.Thread):
    def __init__(self, source_name, mon_key, device_channels, target_pair, clock):
        super().__init__(daemon=True)
        self.source_name = source_name
        self.mon_key = mon_key
        self.device_channels = device_channels
        self.target_pair = list(target_pair)
        if len(self.target_pair) == 1:
            self.target_pair = [self.target_pair[0], self.target_pair[0]]
            
        self.clock = clock
        self.rms_level = 0.0
        self.running = True
        self.process = None
        
        self.is_armed = False
        self.is_recording = False
        self.armed_buffer_id = None
        self.record_file = None
        self.recorded_bytes = 0
        self.target_beats = 4
        self.recorded_bpm = 120 
        self.sync_beat = 0.0
        
        self.frame_bytes = 4 * self.device_channels
        self.target_bytes = 0

    def trigger_record(self, buffer_id, target_beats, current_bpm):
        self.armed_buffer_id = buffer_id
        self.target_beats = target_beats
        self.recorded_bpm = current_bpm
        self.is_armed = True 
        self.record_file = open(os.path.join(WORKSPACE_DIR, f"buffer_{buffer_id:02d}.raw"), "wb")

    def stop_record(self):
        self.is_armed = False
        self.is_recording = False
        if self.record_file:
            self.record_file.close()
            self.record_file = None

    def run(self):
        cmd = ['parec', f'--device={self.source_name}', '--format=float32le', f'--rate={SYSTEM_RATE}', f'--channels={self.device_channels}', '--latency-msec=1']
        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            fd = self.process.stdout.fileno()
            os.set_blocking(fd, False)
            
            last_beat = self.clock.current_beat
            leftover_bytes = b""
            
            while self.running:
                current_beat = self.clock.current_beat
                is_downbeat = (current_beat == 1 and last_beat != 1)
                last_beat = current_beat

                try:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        time.sleep(0.005)
                        continue
                        
                    data = leftover_bytes + chunk
                    safe_length = len(data) - (len(data) % self.frame_bytes)
                    if safe_length == 0: 
                        leftover_bytes = data
                        continue
                        
                    leftover_bytes = data[safe_length:]
                    valid_data = data[:safe_length]
                    
                    if self.clock.global_playing and self.is_armed and is_downbeat:
                        self.is_armed = False
                        self.is_recording = True
                        self.recorded_bytes = 0
                        self.sync_beat = self.clock.last_downbeat_val
                        
                        frames_per_beat = SYSTEM_RATE * (60.0 / self.recorded_bpm)
                        self.target_bytes = int(frames_per_beat * self.target_beats) * 2 * 4 

                    all_samples = np.frombuffer(valid_data, dtype=np.float32).reshape(-1, self.device_channels)
                    stereo_samples = all_samples[:, self.target_pair]
                    
                    if len(stereo_samples) > 0:
                        raw_peak = np.max(np.abs(stereo_samples))
                        self.rms_level = max(raw_peak, self.rms_level * 0.7)

                    if self.is_recording and self.record_file:
                        rec_stereo = stereo_samples.tobytes()
                        remaining = self.target_bytes - self.recorded_bytes
                        if len(rec_stereo) >= remaining:
                            self.record_file.write(rec_stereo[:remaining])
                            self.stop_record() 
                        else:
                            self.record_file.write(rec_stereo)
                            self.recorded_bytes += len(rec_stereo)
                            
                except BlockingIOError:
                    time.sleep(0.005)
                    self.rms_level *= 0.90 
        except Exception:
            self.rms_level = 0.0

    def stop(self):
        self.running = False
        self.stop_record()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except Exception:
                pass

# ==========================================
# 4. MAIN TUI INTERFACE
# ==========================================

class AudioTool:
    def __init__(self):
        self.pulse = pulsectl.Pulse('pulselooper-master')
        self.mode = 1 
        
        self.state_f1 = (0, 0)
        self.state_f2 = (0, 0)
        self.f3_selected_index = 0
        
        self.selected_index = 0
        self.scroll_y = 0 
        self.looper_index = 0 
        self.target_buffer_index = 0  
        
        self.running = True
        self.max_vol = 1.5
        self.expanded_devices = set()
        self.monitors = {} 
        self.nav_list = [] 
        self.f3_list = []
        self.last_cache_update = 0
        self.cached_devices = []
        
        self.hidden_devices = set() 
        self.clipboard_buf = None
        self.clipboard_pattern = None
        
        self.latency_comp_ms = 60 
        self.theme_idx = 0
        self.status_msg = ""
        self.status_time = 0
        
        self.dropdown = None          
        self.text_input = None
        
        self.show_pattern_menu = False
        self.pattern_sel_idx = 0
        self.current_pattern_idx = 0
        
        self.last_key = None
        self.key_press_start = 0.0
        
        # We start with default global definitions. 
        # _load_session will fully override this if a project exists!
        self.patterns = []
        for i in range(16):
            self.patterns.append({
                "id": i + 1,
                "name": f"Pattern {i+1:02d}",
                "buffers": [{"id": j, "name": f"Buf {j:02d}", "state": "EMPTY", "length": 4, "vol": 1.0, "sync_beat": 0.0, "peak": 0.0, "recorded_bpm": 120, "offset_ms": 0.0, "playhead_ratio": 0.0, "muted": False, "soloed": False} for j in range(1, 33)]
            })
            
        global_cfg = self._load_config()
        self.latency_comp_ms = global_cfg.get("latency_comp_ms", 60)
        self.hidden_devices = set(global_cfg.get("hidden_devices", []))
        self.theme_idx = global_cfg.get("theme", 0)
        
        session_cfg = self._load_session()
        start_bpm = session_cfg.get("bpm", 120)
        
        self.buffers = self.patterns[self.current_pattern_idx]["buffers"]
        self.clock = TempoClock(bpm=start_bpm, buffers=self.buffers)
        self.clock.time_sig_string = session_cfg.get("time_sig", "4/4")
        sig = self.clock.time_sig_string
        if sig == "1/8": self.clock.beats_per_measure = 1
        elif sig == "1/4": self.clock.beats_per_measure = 1
        elif sig == "2/4": self.clock.beats_per_measure = 2
        elif sig == "3/4": self.clock.beats_per_measure = 3
        elif sig == "4/4": self.clock.beats_per_measure = 4
        elif sig == "8/4": self.clock.beats_per_measure = 8
        self.clock.clock_comp_ms = global_cfg.get("clock_comp_ms", 0)
        self.clock.play_metronome = global_cfg.get("play_metronome", False)
        
        for b in self.buffers:
            if b["state"] in ["PLAYING", "STOPPED", "QUEUED_PLAY", "QUEUED_STOP"]:
                self.clock._load_buffer(b["id"])

        self.clock.start()

    def _switch_mode(self, new_mode):
        if self.mode == new_mode: return
        if self.mode == 1: self.state_f1 = (self.selected_index, self.scroll_y)
        elif self.mode == 2: self.state_f2 = (self.selected_index, self.scroll_y)
            
        self.mode = new_mode
        self.last_cache_update = 0
        
        if self.mode == 1: self.selected_index, self.scroll_y = self.state_f1
        elif self.mode == 2: self.selected_index, self.scroll_y = self.state_f2

    def _load_config(self):
        if not os.path.exists(GLOBAL_CONFIG_PATH): return {}
        try:
            with open(GLOBAL_CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception: return {}

    def _save_config(self):
        data = {
            "latency_comp_ms": self.latency_comp_ms,
            "clock_comp_ms": self.clock.clock_comp_ms,
            "play_metronome": self.clock.play_metronome,
            "hidden_devices": list(self.hidden_devices),
            "theme": self.theme_idx
        }
        try:
            with open(GLOBAL_CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=4)
        except Exception: pass

    def _load_session(self):
        session_file = os.path.join(WORKSPACE_DIR, "session.json")
        if not os.path.exists(session_file):
            return {}
        try:
            with open(session_file, "r") as f:
                data = json.load(f)
            
            self.current_pattern_idx = data.get("current_pattern_idx", 0)
            loaded_patterns = data.get("patterns", [])
            
            if loaded_patterns:
                self.patterns = []
                for pat_data in loaded_patterns:
                    p = {"id": pat_data["id"], "name": pat_data.get("name", f"Pattern {pat_data['id']:02d}"), "buffers": []}
                    for lb in pat_data.get("buffers", []):
                        state = lb.get("state", "EMPTY")
                        # Validate that physical file still exists if it claims to have audio
                        if state != "EMPTY" and not os.path.exists(os.path.join(WORKSPACE_DIR, f"buffer_{lb['id']:02d}.raw")):
                            state = "EMPTY"
                        
                        p["buffers"].append({
                            "id": lb["id"],
                            "name": lb.get("name", f"Buf {lb['id']:02d}"),
                            "state": state,
                            "length": lb.get("length", 4),
                            "vol": lb.get("vol", 1.0),
                            "recorded_bpm": lb.get("recorded_bpm", 120),
                            "offset_ms": lb.get("offset_ms", 0.0),
                            "sync_beat": lb.get("sync_beat", 0.0),
                            "muted": lb.get("muted", False),
                            "soloed": lb.get("soloed", False),
                            "playhead_ratio": 0.0,
                            "peak": 0.0
                        })
                    self.patterns.append(p)
            return data.get("global", {})
        except Exception:
            return {}

    def _save_session(self):
        session_file = os.path.join(WORKSPACE_DIR, "session.json")
        data = {
            "global": {
                "bpm": self.clock.bpm,
                "time_sig": self.clock.time_sig_string,
            },
            "current_pattern_idx": self.current_pattern_idx,
            "patterns": []
        }
        
        for p in self.patterns:
            pat_save = {"id": p["id"], "name": p["name"], "buffers": []}
            for b in p["buffers"]:
                buf_data = {
                    "id": b["id"],
                    "name": b["name"],
                    "state": b["state"],
                    "length": b["length"],
                    "vol": b["vol"],
                    "recorded_bpm": b.get("recorded_bpm", 120),
                    "offset_ms": b.get("offset_ms", 0.0),
                    "sync_beat": b.get("sync_beat", 0.0),
                    "muted": b.get("muted", False),
                    "soloed": b.get("soloed", False)
                }
                if buf_data["state"] in ["RECORDING", "ARMED"]: buf_data["state"] = "EMPTY"
                if not os.path.exists(os.path.join(WORKSPACE_DIR, f"buffer_{b['id']:02d}.raw")): buf_data["state"] = "EMPTY"
                pat_save["buffers"].append(buf_data)
            data["patterns"].append(pat_save)
            
        try:
            with open(session_file, "w") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    def _switch_pattern(self, new_idx):
        if new_idx == self.current_pattern_idx: return
        
        old_buffers = self.buffers
        new_buffers = self.patterns[new_idx]["buffers"]
        
        for i in range(32):
            old_b = old_buffers[i]
            new_b = new_buffers[i]
            
            if new_b["state"] == "PLAYING" and old_b["state"] not in ["PLAYING", "QUEUED_STOP"]:
                new_b["state"] = "QUEUED_PLAY"
            elif new_b["state"] in ["STOPPED", "EMPTY"] and old_b["state"] in ["PLAYING", "QUEUED_STOP"]:
                new_b["state"] = "QUEUED_STOP"
                
            if new_b["state"] in ["PLAYING", "QUEUED_PLAY", "QUEUED_STOP"] and new_b["id"] not in self.clock.buffer_data:
                self.clock._load_buffer(new_b["id"])
            elif new_b["state"] in ["STOPPED", "EMPTY"] and new_b["id"] in self.clock.buffer_data:
                del self.clock.buffer_data[new_b["id"]]
                
        self.current_pattern_idx = new_idx
        self.buffers = new_buffers
        self.clock.buffers = self.buffers

    def _apply_theme(self):
        themes = [
            (curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_MAGENTA), 
            (curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN),    
            (curses.COLOR_MAGENTA, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_BLUE, curses.COLOR_YELLOW), 
            (curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_CYAN)    
        ]
        t = themes[self.theme_idx % 4]
        curses.init_pair(1, t[0], -1)
        curses.init_pair(2, t[1], -1)
        curses.init_pair(3, t[2], -1)
        curses.init_pair(4, t[3], -1)
        curses.init_pair(5, t[4], -1)

    def get_nav_items(self):
        now = time.time()
        if now - self.last_cache_update > 0.5:
            try:
                self.cached_devices = self.pulse.sink_list() if self.mode == 1 else self.pulse.source_list()
                self.f3_list = [(d, "OUT") for d in self.pulse.sink_list()] + [(d, "IN") for d in self.pulse.source_list()]
                self.last_cache_update = now
            except: pass
        
        items = []
        for dev in self.cached_devices:
            if dev.name in self.hidden_devices: continue
            
            items.append(('device', dev, None))
            if dev.index in self.expanded_devices:
                ch_count = len(dev.volume.values)
                for i in range(0, ch_count, 2):
                    if i + 1 < ch_count:
                        items.append(('pair', dev, (i, i+1)))
                    else:
                        items.append(('pair', dev, (i,))) 
        return items

    def draw_bar(self, stdscr, y, x, width, vol, peak, muted):
        available_width = width - x - 8
        if available_width <= 2: return
        
        bar_width = available_width
        stop_100 = int(bar_width * (1.0 / self.max_vol))
        v_pos = int(bar_width * (min(vol, self.max_vol) / self.max_vol))
        
        visual_peak = math.pow(peak, 0.45) if peak > 0 else 0
        p_pos = int(bar_width * (min(visual_peak, self.max_vol) / self.max_vol))

        try:
            stdscr.addstr(y, x, "[")
            for i in range(bar_width):
                if i == stop_100:
                    stdscr.addch('|', curses.A_BOLD)
                    continue
                
                if i < p_pos:
                    stdscr.addch('*', curses.color_pair(5) | curses.A_BOLD)
                elif i < v_pos:
                    col = curses.color_pair(3) if muted else (curses.color_pair(4) if i >= stop_100 else curses.color_pair(2))
                    stdscr.addch('#', col)
                else:
                    stdscr.addch('-', curses.A_DIM)
            
            stdscr.addstr(f"] {int(vol*100):>3}%")
        except curses.error:
            pass

    def _draw_dropdown(self, stdscr, h, w):
        if not self.dropdown: return
        
        opts = self.dropdown["options"]
        sel = self.dropdown["sel"]
        title = self.dropdown["title"]
        
        box_w = max(36, max(len(o) for o in opts) + 6)
        box_h = min(h - 4, len(opts) + 4)
        start_y = (h - box_h) // 2
        start_x = (w - box_w) // 2
        
        for y in range(start_y, start_y + box_h):
            stdscr.addstr(y, start_x, " " * box_w, curses.color_pair(1) | curses.A_REVERSE)
            
        stdscr.addstr(start_y + 1, start_x + 2, title, curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD)
        
        visible_opts = box_h - 4
        scroll_start = max(0, min(sel - visible_opts // 2, len(opts) - visible_opts))
        
        for i in range(scroll_start, min(len(opts), scroll_start + visible_opts)):
            opt = opts[i]
            y = start_y + 3 + (i - scroll_start)
            prefix = "> " if i == sel else "  "
            
            attr = curses.color_pair(3) | curses.A_REVERSE | curses.A_BOLD if i == sel else curses.color_pair(1) | curses.A_REVERSE
            stdscr.addstr(y, start_x + 2, f"{prefix}{opt}".ljust(box_w - 4), attr)

    def _draw_text_input(self, stdscr, h, w):
        if not self.text_input: return
        if self.text_input.get("type") == "rename_pattern": return 
        if self.text_input.get("type") == "rename": return 
        
        prompt = self.text_input["prompt"]
        val = self.text_input["val"]
        
        box_w = max(40, len(prompt) + len(val) + 6)
        start_y = h // 2
        start_x = (w - box_w) // 2
        
        for y in range(start_y - 1, start_y + 2):
            stdscr.addstr(y, start_x, " " * box_w, curses.color_pair(1) | curses.A_REVERSE)
            
        display_str = f"{prompt} {val}_"
        stdscr.addstr(start_y, start_x + 2, display_str, curses.color_pair(4) | curses.A_REVERSE | curses.A_BOLD)

    def _draw_pattern_menu(self, stdscr, h, w):
        box_w = 40
        box_h = 22
        start_y = (h - box_h) // 2
        start_x = (w - box_w) // 2
        
        for y in range(start_y, start_y + box_h):
            stdscr.addstr(y, start_x, " " * box_w, curses.color_pair(1) | curses.A_REVERSE)
            
        stdscr.addstr(start_y + 1, start_x + 2, "SESSION PATTERNS", curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD)
        stdscr.addstr(start_y + 2, start_x + 2, "Enter: Load | c/v: Copy/Paste", curses.color_pair(1) | curses.A_REVERSE | curses.A_DIM)
        
        for i in range(16):
            pat = self.patterns[i]
            y = start_y + 4 + i
            
            is_sel = (i == self.pattern_sel_idx)
            is_cur = (i == self.current_pattern_idx)
            
            pref = "> " if is_sel else "  "
            play_ind = "▶ " if is_cur else "  "
            
            attr = curses.color_pair(3) | curses.A_REVERSE | curses.A_BOLD if is_sel else curses.color_pair(1) | curses.A_REVERSE
            if is_cur: attr |= curses.A_UNDERLINE
            
            if is_sel and self.text_input and self.text_input.get("type") == "rename_pattern":
                name_disp = self.text_input["val"] + "_"
            else:
                name_disp = pat['name'][:20]
                
            disp = f"{pref}{play_ind}{pat['id']:02d}: {name_disp}"
            stdscr.addstr(y, start_x + 2, disp[:box_w - 4].ljust(box_w - 4), attr)
            
        stdscr.addstr(start_y + box_h - 2, start_x + 2, "J/K: Move | r: Rename | p: Close", curses.color_pair(1) | curses.A_REVERSE | curses.A_DIM)

    def _get_marquee(self, text, max_len):
        if len(text) <= max_len:
            return text.ljust(max_len)
        padded = text + " *** "
        idx = int(time.time() * 3) % len(padded)
        return (padded[idx:] + padded[:idx])[:max_len]

    def _export_mix(self, filename, bars_str):
        try:
            bars = int(bars_str)
        except ValueError:
            bars = 4
            
        frames_per_beat = int(SYSTEM_RATE * 60.0 / self.clock.bpm)
        total_frames = bars * self.clock.beats_per_measure * frames_per_beat
        export_data = np.zeros((total_frames, 2), dtype=np.float32)

        any_solo = any(b.get("soloed", False) for b in self.buffers if b["state"] == "PLAYING")

        for buf in self.buffers:
            if buf["state"] == "PLAYING" and buf["id"] in self.clock.buffer_data:
                data = self.clock.buffer_data[buf["id"]]
                vol = buf["vol"]
                length = len(data)
                
                effective_vol = vol
                if buf.get("muted", False):
                    effective_vol = 0.0
                elif any_solo and not buf.get("soloed", False):
                    effective_vol = 0.0
                
                ratio = self.clock.bpm / buf.get("recorded_bpm", self.clock.bpm)
                offset_frames = int(SYSTEM_RATE * (buf.get("offset_ms", 0.0) / 1000.0))
                
                out_indices = np.arange(total_frames)
                buf_indices = (offset_frames + out_indices * ratio).astype(int) % length
                export_data += data[buf_indices] * effective_vol
                
        max_val = np.max(np.abs(export_data))
        if max_val > 1.0:
            export_data /= max_val
            
        int16_data = (export_data * 32767).astype(np.int16)
        
        filepath = os.path.join(WORKSPACE_DIR, filename)
        try:
            with wave.open(filepath, 'wb') as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(SYSTEM_RATE)
                wav_file.writeframes(int16_data.tobytes())
            self.status_msg = f"Exported {bars} bars to {filename}!"
        except Exception as e:
            self.status_msg = f"Export Error: {str(e)}"
        self.status_time = time.time()

    def _swap_buffers(self, idx1, idx2):
        # Swap list elements across ALL patterns to keep Track Order perfectly synchronized!
        for p in self.patterns:
            p["buffers"][idx1], p["buffers"][idx2] = p["buffers"][idx2], p["buffers"][idx1]

    def draw_f4_looper(self, stdscr, h, w):
        visible_rows = h - 7
        start_idx = max(0, min(self.looper_index - visible_rows // 2, 32 - visible_rows))
        
        for i in range(start_idx, min(32, start_idx + visible_rows)):
            buf = self.buffers[i]
            row = 5 + (i - start_idx)
            is_sel = (i == self.looper_index)
            
            state = buf["state"]
            if state == "PLAYING": color = curses.color_pair(2) | curses.A_BOLD
            elif state == "RECORDING": color = curses.color_pair(3) | curses.A_BOLD
            elif state == "ARMED": color = curses.color_pair(4) | curses.A_BOLD
            elif state == "QUEUED_PLAY": color = curses.color_pair(2) | curses.A_BLINK
            elif state == "QUEUED_STOP": color = curses.color_pair(4) | curses.A_BLINK
            elif state == "STOPPED": color = curses.color_pair(1) 
            else: color = curses.A_DIM
            
            pref = "> " if is_sel else "  "
            attr = curses.color_pair(1) | curses.A_BOLD if is_sel else curses.A_NORMAL
            
            mute_char = "M" if buf.get("muted", False) else "-"
            solo_char = "S" if buf.get("soloed", False) else "-"
            
            stdscr.addstr(row, 2, pref, attr)
            stdscr.addstr(row, 4, "[", attr)
            if buf.get("muted", False): stdscr.addstr(row, 5, mute_char, curses.color_pair(3) | curses.A_BOLD)
            else: stdscr.addstr(row, 5, mute_char, curses.A_DIM)
            stdscr.addstr(row, 6, " ", attr)
            if buf.get("soloed", False): stdscr.addstr(row, 7, solo_char, curses.color_pair(4) | curses.A_BOLD)
            else: stdscr.addstr(row, 7, solo_char, curses.A_DIM)
            stdscr.addstr(row, 8, "] ", attr)
            
            if is_sel and self.text_input and self.text_input.get("type") == "rename":
                name_str = (self.text_input["val"] + "_").ljust(10)[:10]
            else:
                name_str = self._get_marquee(buf["name"], 10)
                
            offset_str = f"{buf['offset_ms']:+04.0f}ms"
            info_str = f"[{buf['length']:02d}B {offset_str}]"
            
            stdscr.addstr(row, 10, f"{name_str} {info_str}: ", attr)
            stdscr.addstr(row, 36, f"[{state.center(9)}]", color)
            
            if state in ["PLAYING", "QUEUED_STOP"]:
                prog_pos = int(buf.get("playhead_ratio", 0) * 10)
                prog_str = "├" + "─" * prog_pos + "▶" + "─" * (9 - prog_pos) + "┤"
            else:
                prog_str = "├" + "─" * 10 + "┤"
            
            stdscr.addstr(row, 48, prog_str, color)
            
            peak_val = buf.get("peak", 0.0)
            self.draw_bar(stdscr, row, 60, w, buf["vol"], peak_val, False)
            
        stdscr.addstr(h-2, 2, "Spc: Play | C/V: Copy | J/K: Move | </>: Offset | m/s: Mute/Solo | r: Ren | p: Pat", curses.A_DIM)

    def draw_f3_cards(self, stdscr, h, w):
        stdscr.addstr(5, 4, "Hardware / Cards Configuration", curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(7, 4, "Select which devices appear in F1/F2 menus (Space to toggle visibility):", curses.A_DIM)
        
        self.get_nav_items() 
        
        visible_rows = h - 10
        start_idx = max(0, min(self.f3_selected_index - visible_rows // 2, len(self.f3_list) - visible_rows))
        
        for i in range(start_idx, min(len(self.f3_list), start_idx + visible_rows)):
            dev, io_type = self.f3_list[i]
            row = 9 + (i - start_idx)
            is_sel = (i == self.f3_selected_index)
            
            pref = "> " if is_sel else "  "
            attr = curses.color_pair(1) | curses.A_BOLD if is_sel else curses.A_NORMAL
            
            is_hidden = dev.name in self.hidden_devices
            box = "[ ]" if is_hidden else "[X]"
            color = curses.A_DIM if is_hidden else (curses.color_pair(2) | curses.A_BOLD)
            
            safe_desc = dev.description[:w-21]
            stdscr.addstr(row, 4, f"{pref}", attr)
            stdscr.addstr(row, 6, f"{box}", color)
            
            io_color = curses.color_pair(4) if io_type == "OUT" else curses.color_pair(5)
            stdscr.addstr(row, 10, f"[{io_type}]", io_color | curses.A_BOLD)
            stdscr.addstr(row, 16, f"{safe_desc}", attr)

    def draw_ui(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        
        for i, name in enumerate([" F1: Output ", " F2: Input ", " F3: Cards ", " F4: Looper "]):
            attr = curses.color_pair(1) | curses.A_REVERSE if self.mode == i+1 else curses.A_NORMAL
            stdscr.addstr(0, i*15, name, attr)
        stdscr.addstr(1, 0, "─" * w)

        play_icon = "▶" if self.clock.global_playing else "■"
        beat_str = " ".join([f"[{i}]" if (i == self.clock.current_beat and self.clock.global_playing) else f" {i} " for i in range(1, self.clock.beats_per_measure + 1)])
        metro_status = "ON" if self.clock.play_metronome else "OFF"
        pat_name = self.patterns[self.current_pattern_idx]["name"]
        
        header_text = f" [{play_icon}] (Spc) | PAT: {pat_name} | BPM: {self.clock.bpm:03d} (+/-) | SIG: {self.clock.time_sig_string} | BEAT: {beat_str} | MET: {metro_status} [c]"
        stdscr.addstr(2, 0, header_text[:w-1], curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(3, 0, "─" * w)
        
        if time.time() - self.status_time < 3:
            stdscr.addstr(3, w - len(self.status_msg) - 2, self.status_msg, curses.color_pair(4) | curses.A_BOLD)

        if self.mode == 4:
            self.draw_f4_looper(stdscr, h, w)
            
        elif self.mode == 3:
            self.draw_f3_cards(stdscr, h, w)
            
        else:
            target_text = f" TARGET BUFFER: [ {self.buffers[self.target_buffer_index]['name']} ] (Press 'b' to change target)"
            stdscr.addstr(4, 0, target_text[:w-1], curses.color_pair(4) | curses.A_BOLD)
            
            self.nav_list = self.get_nav_items()
            if not self.nav_list:
                stdscr.addstr(6, 2, "Waiting for PulseAudio...")
                stdscr.refresh()
                return

            self.selected_index = max(0, min(self.selected_index, len(self.nav_list)-1))
            
            item_y_starts = []
            current_y = 0
            for kind, dev, c_idx in self.nav_list:
                item_y_starts.append(current_y)
                current_y += 3 if kind == 'device' else 1
                
            sel_y = item_y_starts[self.selected_index]
            sel_h = 3 if self.nav_list[self.selected_index][0] == 'device' else 1
            max_visible_h = h - 8 
            
            if sel_y < self.scroll_y:
                self.scroll_y = sel_y
            elif sel_y + sel_h > self.scroll_y + max_visible_h:
                self.scroll_y = sel_y + sel_h - max_visible_h
            
            for idx, item in enumerate(self.nav_list):
                kind, dev, c_idx = item
                item_start = item_y_starts[idx]
                item_h = 3 if kind == 'device' else 1
                
                if item_start + item_h <= self.scroll_y: continue
                if item_start >= self.scroll_y + max_visible_h: break
                
                draw_y = 6 + (item_start - self.scroll_y)
                is_sel = (idx == self.selected_index)
                
                if kind == 'device':
                    vol = sum(dev.volume.values)/len(dev.volume.values)
                    mon_state = "     "
                    peak = 0
                    prog_str = ""
                    
                    is_multichannel = len(dev.volume.values) > 2
                    if not is_multichannel:
                        mon_key = f"{dev.index}-root"
                        if mon_key in self.monitors:
                            m = self.monitors[mon_key]
                            peak = m.rms_level
                            if m.is_recording: 
                                mon_state = "[REC]"
                                ratio = min(1.0, max(0.0, m.recorded_bytes / max(1, m.target_bytes)))
                                p = max(0, min(9, int(ratio * 10)))
                                prog_str = "  ├" + "─" * p + "▶" + "─" * (9 - p) + "┤"
                            elif m.is_armed: 
                                mon_state = "[ARM]"
                                prog_str = "  ├──────────┤"
                            else: 
                                mon_state = "[M]  "

                    pref = "> " if is_sel else "  "
                    icon = "▼ " if dev.index in self.expanded_devices else "▶ "
                    is_rec_or_arm = "[REC]" in mon_state or "[ARM]" in mon_state
                    attr = curses.color_pair(3) | curses.A_BOLD if is_rec_or_arm else (curses.color_pair(1) | curses.A_BOLD if is_sel else curses.A_NORMAL)
                    
                    extra_hint = " (Unfold for Pairs)" if is_multichannel and is_sel and not dev.index in self.expanded_devices else ""
                    
                    safe_desc_len = max(5, w - 25 - len(extra_hint) - len(prog_str))
                    
                    if draw_y < h - 1:
                        stdscr.addstr(draw_y, 2, f"{pref}{mon_state}{icon}{dev.description[:safe_desc_len]}{extra_hint}{prog_str}", attr)
                    if draw_y + 1 < h - 1:
                        self.draw_bar(stdscr, draw_y + 1, 9, w, vol, peak, dev.mute)
                
                elif kind == 'pair':
                    vol = sum(dev.volume.values[i] for i in c_idx) / len(c_idx)
                    mon_key = f"{dev.index}-{c_idx[0]}"
                    mon_state = "     "
                    peak = 0
                    prog_str = ""
                    
                    if mon_key in self.monitors:
                        m = self.monitors[mon_key]
                        peak = m.rms_level
                        if m.is_recording: 
                            mon_state = "[REC]"
                            ratio = min(1.0, max(0.0, m.recorded_bytes / max(1, m.target_bytes)))
                            p = max(0, min(9, int(ratio * 10)))
                            prog_str = "  ├" + "─" * p + "▶" + "─" * (9 - p) + "┤"
                        elif m.is_armed: 
                            mon_state = "[ARM]"
                            prog_str = "  ├──────────┤"
                        else: 
                            mon_state = "[M]  "
                    
                    pref = "> " if is_sel else "  "
                    is_rec_or_arm = "[REC]" in mon_state or "[ARM]" in mon_state
                    attr = curses.color_pair(3) | curses.A_BOLD if is_rec_or_arm else (curses.color_pair(1) | curses.A_BOLD if is_sel else curses.A_DIM)
                    
                    label = f"Ch {c_idx[0]+1}/{c_idx[1]+1}" if len(c_idx) == 2 else f"Ch {c_idx[0]+1}"
                    
                    if draw_y < h - 1:
                        stdscr.addstr(draw_y, 11, f"{pref}{mon_state}{label}{prog_str}", attr)
                        bar_offset = 28 + len(prog_str)
                        self.draw_bar(stdscr, draw_y, bar_offset, w, vol, peak, dev.mute)

        if self.show_pattern_menu:
            self._draw_pattern_menu(stdscr, h, w)
            
        self._draw_dropdown(stdscr, h, w)
        self._draw_text_input(stdscr, h, w)
            
        stdscr.refresh()

    def _toggle_monitor_thread(self, dev, mon_key, target_pair):
        if mon_key in self.monitors:
            self.monitors[mon_key].stop()
            del self.monitors[mon_key]
        else:
            src = dev.name if self.mode == 2 else dev.monitor_source_name
            ch_count = len(dev.volume.values)
            t = MonitorThread(src, mon_key, ch_count, target_pair, self.clock)
            t.start()
            self.monitors[mon_key] = t

    def _trigger_record_logic(self, mon_key):
        if mon_key in self.monitors:
            mon = self.monitors[mon_key]
            
            if mon.is_recording or mon.is_armed:
                mon.stop_record()
                # Finding by ID guarantees it works no matter what row the track is on!
                target_buf = next((b for b in self.buffers if b["id"] == mon.armed_buffer_id), None)
                if target_buf:
                    target_buf["state"] = "PLAYING"
            else:
                buf = self.buffers[self.target_buffer_index]
                if buf["state"] in ["PLAYING", "QUEUED_PLAY", "QUEUED_STOP", "STOPPED"]:
                    buf["state"] = "EMPTY"
                    buf["peak"] = 0.0
                    if buf["id"] in self.clock.buffer_data:
                        del self.clock.buffer_data[buf["id"]]
                    
                buf["state"] = "ARMED"
                mon.trigger_record(buf["id"], buf["length"], self.clock.bpm)

    def _sync_states(self):
        for buf in self.buffers:
            buf["peak"] = buf.get("peak", 0.0) * 0.85
            
        for mon_key, m in self.monitors.items():
            if m.armed_buffer_id is not None:
                # Find Target by physical ID, resolving swapped layout lists safely
                target_buf = next((b for b in self.buffers if b["id"] == m.armed_buffer_id), None)
                if target_buf:
                    if m.is_recording:
                        target_buf["state"] = "RECORDING"
                    elif not m.is_armed and not m.is_recording:
                        if target_buf["state"] in ["RECORDING", "ARMED"]:
                            target_buf["state"] = "PLAYING"
                            
                            target_buf["sync_beat"] = m.sync_beat
                            target_buf["recorded_bpm"] = m.recorded_bpm
                            target_buf["offset_ms"] = float(self.latency_comp_ms)
                            
                            self.clock._load_buffer(target_buf["id"])
                        m.armed_buffer_id = None

    def handle_input(self, stdscr):
        key = stdscr.getch()
        
        now = time.time()
        if key != -1:
            if key == self.last_key:
                if now - self.key_press_start > 0.3:
                    accel_step = 5.0 
                else:
                    accel_step = 1.0
            else:
                self.last_key = key
                self.key_press_start = now
                accel_step = 1.0
        else:
            self.last_key = None
            return
            
        # --- 1. TEXT INPUT MODAL BLOCKER ---
        if self.text_input:
            if key in [curses.KEY_ENTER, 10, 13]:
                if self.text_input["type"] == "rename":
                    val = self.text_input["val"].strip()
                    if val:
                        target_id = self.buffers[self.looper_index]["id"]
                        # Globally propagate rename to this physical buffer in all patterns!
                        for p in self.patterns:
                            for b in p["buffers"]:
                                if b["id"] == target_id:
                                    b["name"] = val
                    self.text_input = None
                elif self.text_input["type"] == "rename_pattern":
                    val = self.text_input["val"].strip()
                    if val:
                        self.patterns[self.pattern_sel_idx]["name"] = val
                    self.text_input = None
                elif self.text_input["type"] == "export_name":
                    filename = self.text_input["val"].strip() or "export.wav"
                    if not filename.endswith(".wav"): filename += ".wav"
                    self.text_input = {
                        "type": "export_bars", 
                        "prompt": "Number of bars to export:", 
                        "val": "4",
                        "filename": filename
                    }
                elif self.text_input["type"] == "export_bars":
                    filename = self.text_input["filename"]
                    bars_str = self.text_input["val"].strip()
                    self._export_mix(filename, bars_str)
                    self.text_input = None
            elif key in [curses.KEY_BACKSPACE, curses.KEY_DC, 127, 8]:
                self.text_input["val"] = self.text_input["val"][:-1]
            elif key in [27]: 
                self.text_input = None
            elif 32 <= key <= 126:
                if len(self.text_input["val"]) < 30:
                    self.text_input["val"] += chr(key)
            return

        # --- 2. PATTERN MENU MODAL BLOCKER ---
        if self.show_pattern_menu:
            if key in [ord('p'), 27]:
                self.show_pattern_menu = False
            elif key in [ord('j'), curses.KEY_DOWN]:
                self.pattern_sel_idx = min(15, self.pattern_sel_idx + 1)
            elif key in [ord('k'), curses.KEY_UP]:
                self.pattern_sel_idx = max(0, self.pattern_sel_idx - 1)
            elif key in [ord('J'), curses.KEY_SF]:
                if self.pattern_sel_idx < 15:
                    self.patterns[self.pattern_sel_idx], self.patterns[self.pattern_sel_idx+1] = self.patterns[self.pattern_sel_idx+1], self.patterns[self.pattern_sel_idx]
                    self.patterns[self.pattern_sel_idx]["id"], self.patterns[self.pattern_sel_idx+1]["id"] = self.patterns[self.pattern_sel_idx+1]["id"], self.patterns[self.pattern_sel_idx]["id"]
                    if self.current_pattern_idx == self.pattern_sel_idx: self.current_pattern_idx += 1
                    elif self.current_pattern_idx == self.pattern_sel_idx + 1: self.current_pattern_idx -= 1
                    self.pattern_sel_idx += 1
            elif key in [ord('K'), curses.KEY_SR]:
                if self.pattern_sel_idx > 0:
                    self.patterns[self.pattern_sel_idx], self.patterns[self.pattern_sel_idx-1] = self.patterns[self.pattern_sel_idx-1], self.patterns[self.pattern_sel_idx]
                    self.patterns[self.pattern_sel_idx]["id"], self.patterns[self.pattern_sel_idx-1]["id"] = self.patterns[self.pattern_sel_idx-1]["id"], self.patterns[self.pattern_sel_idx]["id"]
                    if self.current_pattern_idx == self.pattern_sel_idx: self.current_pattern_idx -= 1
                    elif self.current_pattern_idx == self.pattern_sel_idx - 1: self.current_pattern_idx += 1
                    self.pattern_sel_idx -= 1
            elif key == ord('c'):
                self.clipboard_pattern = copy.deepcopy(self.patterns[self.pattern_sel_idx])
                self.status_msg = f"Copied Pattern {self.patterns[self.pattern_sel_idx]['id']:02d}"
                self.status_time = time.time()
            elif key == ord('v'):
                if self.clipboard_pattern:
                    target_id = self.patterns[self.pattern_sel_idx]["id"]
                    self.patterns[self.pattern_sel_idx] = copy.deepcopy(self.clipboard_pattern)
                    self.patterns[self.pattern_sel_idx]["id"] = target_id 
                    if self.pattern_sel_idx == self.current_pattern_idx:
                        self._switch_pattern(self.pattern_sel_idx)
                    self.status_msg = f"Pasted to Pattern {target_id:02d}"
                    self.status_time = time.time()
            elif key == ord('r'):
                self.text_input = {"type": "rename_pattern", "prompt": "Rename Pattern:", "val": self.patterns[self.pattern_sel_idx]["name"]}
            elif key in [curses.KEY_ENTER, 10, 13]:
                self._switch_pattern(self.pattern_sel_idx)
                self.show_pattern_menu = False
            return

        # --- 3. DROPDOWN MODAL BLOCKER ---
        if self.dropdown:
            if key in [ord('j'), curses.KEY_DOWN]:
                self.dropdown["sel"] = min(len(self.dropdown["options"]) - 1, self.dropdown["sel"] + 1)
            elif key in [ord('k'), curses.KEY_UP]:
                self.dropdown["sel"] = max(0, self.dropdown["sel"] - 1)
                
            elif self.dropdown["type"] == "options":
                if key in [ord('l'), curses.KEY_RIGHT]:
                    if self.dropdown["sel"] == 0:
                        self.latency_comp_ms += int(accel_step)
                    elif self.dropdown["sel"] == 1:
                        self.clock.clock_comp_ms += int(accel_step)
                    elif self.dropdown["sel"] == 2:
                        self.theme_idx = (self.theme_idx + 1) % 4
                        self._apply_theme()
                elif key in [ord('h'), curses.KEY_LEFT]:
                    if self.dropdown["sel"] == 0:
                        self.latency_comp_ms = max(0, self.latency_comp_ms - int(accel_step))
                    elif self.dropdown["sel"] == 1:
                        self.clock.clock_comp_ms -= int(accel_step)
                    elif self.dropdown["sel"] == 2:
                        self.theme_idx = (self.theme_idx - 1) % 4
                        self._apply_theme()
                        
                self.dropdown["options"] = [
                    f"Rec Latency Comp (Input):  {self.latency_comp_ms:03d} ms",
                    f"Clock Comp (Output Delay): {self.clock.clock_comp_ms:+04d} ms",
                    f"UI Theme: {['Default', 'Tango', 'Zenburn', 'Classic'][self.theme_idx]}"
                ]
                if key in [curses.KEY_ENTER, 10, 13, ord('q'), 27]:
                    self.dropdown = None
                    
            elif key in [curses.KEY_ENTER, 10, 13]:
                if self.dropdown["type"] == "buffer":
                    self.target_buffer_index = self.dropdown["sel"]
                    self.looper_index = self.target_buffer_index
                elif self.dropdown["type"] == "timesig":
                    sig = self.dropdown["options"][self.dropdown["sel"]]
                    self.clock.time_sig_string = sig
                    if sig == "1/8": self.clock.beats_per_measure = 1
                    elif sig == "1/4": self.clock.beats_per_measure = 1
                    elif sig == "2/4": self.clock.beats_per_measure = 2
                    elif sig == "3/4": self.clock.beats_per_measure = 3
                    elif sig == "4/4": self.clock.beats_per_measure = 4
                    elif sig == "8/4": self.clock.beats_per_measure = 8
                self.dropdown = None
                
            elif key in [curses.KEY_PPAGE, curses.KEY_NPAGE, ord('['), ord(']')]:
                if self.dropdown["type"] == "buffer":
                    buf_idx = self.dropdown["sel"]
                    if key in [curses.KEY_PPAGE, ord(']')]:
                        self.buffers[buf_idx]["length"] = min(64, self.buffers[buf_idx]["length"] + 1)
                    else:
                        self.buffers[buf_idx]["length"] = max(1, self.buffers[buf_idx]["length"] - 1)
                    self.dropdown["options"] = [f"{self.buffers[i]['name'][:10].ljust(10)} [{self.buffers[i]['state'][:5]}] ({self.buffers[i]['length']:02d} Bts)" for i in range(32)]
            elif key in [ord('q'), 27]: 
                self.dropdown = None
            return 

        # --- GLOBAL KEYS ---
        if key == ord('q'): self.running = False
        elif key in [ord('1'), curses.KEY_F1]: self._switch_mode(1)
        elif key in [ord('2'), curses.KEY_F2]: self._switch_mode(2)
        elif key in [ord('3'), curses.KEY_F3]: self._switch_mode(3)
        elif key in [ord('4'), curses.KEY_F4]: self._switch_mode(4)
        elif key == ord('c'): 
            self.clock.play_metronome = not self.clock.play_metronome
        elif key in [ord('+'), ord('=')]:
            self.clock.bpm = min(300, self.clock.bpm + 1)
        elif key in [ord('-'), ord('_')]:
            self.clock.bpm = max(30, self.clock.bpm - 1)
        
        elif key == ord(' '):
            self.clock.global_playing = not self.clock.global_playing
            if not self.clock.global_playing:
                # INSTANT TRANSPORT RESTART! Zero everything out.
                self.clock.global_frames = 0
                self.clock.exact_global_beat = 0.0
                self.clock.last_total_beats = -1
                self.clock.absolute_beat = 0
                self.clock.current_beat = 1
                self.clock.last_downbeat_val = 0.0
                for k in self.clock.playheads:
                    self.clock.playheads[k] = 0.0
                for p in self.patterns:
                    for b in p["buffers"]:
                        b["sync_beat"] = 0.0
                        
        elif key == ord('t'):
            opts = ["1/8", "1/4", "2/4", "3/4", "4/4", "8/4"]
            sel_idx = opts.index(self.clock.time_sig_string) if self.clock.time_sig_string in opts else 4
            self.dropdown = {
                "type": "timesig",
                "title": "Select Time Signature",
                "options": opts,
                "sel": sel_idx
            }
            
        elif key == ord('o'):
            self.dropdown = {
                "type": "options",
                "title": "Settings (Left/Right to Adjust, Enter to Close)",
                "options": [
                    f"Rec Latency Comp (Input):  {self.latency_comp_ms:03d} ms",
                    f"Clock Comp (Output Delay): {self.clock.clock_comp_ms:+04d} ms",
                    f"UI Theme: {['Default', 'Tango', 'Zenburn', 'Classic'][self.theme_idx]}"
                ],
                "sel": 0
            }

        # --- F4 LOOPER KEYS ---
        elif self.mode == 4:
            if key == ord('p'):
                self.show_pattern_menu = True
                self.pattern_sel_idx = self.current_pattern_idx
                
            elif key in [ord('j'), curses.KEY_DOWN]: 
                self.looper_index = min(31, self.looper_index + 1)
                self.target_buffer_index = self.looper_index
            elif key in [ord('k'), curses.KEY_UP]: 
                self.looper_index = max(0, self.looper_index - 1)
                self.target_buffer_index = self.looper_index
            
            elif key in [ord('J'), curses.KEY_SF]:
                if self.looper_index < 31:
                    self._swap_buffers(self.looper_index, self.looper_index + 1)
                    self.looper_index += 1
                    self.target_buffer_index = self.looper_index
            elif key in [ord('K'), curses.KEY_SR]:
                if self.looper_index > 0:
                    self._swap_buffers(self.looper_index, self.looper_index - 1)
                    self.looper_index -= 1
                    self.target_buffer_index = self.looper_index
                    
            elif key == ord('C'):
                self.clipboard_buf = self.looper_index
                self.status_msg = f"Copied Buf {self.buffers[self.looper_index]['id']:02d}"
                self.status_time = time.time()
            elif key == ord('V'):
                if self.clipboard_buf is not None and self.clipboard_buf != self.looper_index:
                    src = self.buffers[self.clipboard_buf]
                    dst = self.buffers[self.looper_index]
                    
                    if dst["state"] in ["PLAYING", "QUEUED_PLAY"]:
                        dst["state"] = "STOPPED"
                        
                    keys_to_copy = ["name", "state", "length", "vol", "sync_beat", "recorded_bpm", "offset_ms", "muted", "soloed"]
                    for k in keys_to_copy:
                        dst[k] = src.get(k)
                        
                    f_src = os.path.join(WORKSPACE_DIR, f"buffer_{src['id']:02d}.raw")
                    f_dst = os.path.join(WORKSPACE_DIR, f"buffer_{dst['id']:02d}.raw")
                    if os.path.exists(f_src):
                        shutil.copy2(f_src, f_dst)
                    elif os.path.exists(f_dst):
                        os.remove(f_dst)
                        
                    # Globally propagate name change for the newly overwritten physical buffer
                    target_id = dst["id"]
                    new_name = src["name"]
                    for p in self.patterns:
                        for b in p["buffers"]:
                            if b["id"] == target_id:
                                b["name"] = new_name
                        
                    if src["id"] in self.clock.buffer_data:
                        self.clock.buffer_data[dst["id"]] = self.clock.buffer_data[src["id"]].copy()
                    else:
                        self.clock.buffer_data.pop(dst["id"], None)
                        
                    self.status_msg = f"Pasted to Buf {dst['id']:02d}"
                    self.status_time = time.time()
            
            elif key == ord('m'):
                buf = self.buffers[self.looper_index]
                buf["muted"] = not buf.get("muted", False)
            elif key == ord('s'):
                buf = self.buffers[self.looper_index]
                buf["soloed"] = not buf.get("soloed", False)
            
            elif key in [ord('l'), curses.KEY_RIGHT]:
                buf = self.buffers[self.looper_index]
                buf["vol"] = min(self.max_vol, buf["vol"] + 0.05)
            elif key in [ord('h'), curses.KEY_LEFT]:
                buf = self.buffers[self.looper_index]
                buf["vol"] = max(0.0, buf["vol"] - 0.05)
                
            elif key in [curses.KEY_PPAGE, ord(']'), ord('}')]:
                buf = self.buffers[self.looper_index]
                buf["length"] = min(64, buf["length"] + 1)
            elif key in [curses.KEY_NPAGE, ord('['), ord('{')]:
                buf = self.buffers[self.looper_index]
                buf["length"] = max(1, buf["length"] - 1)
                
            elif key in [ord('<'), ord(',')]:
                buf = self.buffers[self.looper_index]
                buf["offset_ms"] -= accel_step
            elif key in [ord('>'), ord('.')]:
                buf = self.buffers[self.looper_index]
                buf["offset_ms"] += accel_step
                
            elif key == ord('r'):
                self.text_input = {"type": "rename", "prompt": "Rename Buffer:", "val": self.buffers[self.looper_index]["name"]}
                
            elif key == ord('e'):
                self.text_input = {"type": "export_name", "prompt": "Export filename:", "val": "export.wav"}
            
            elif key in [curses.KEY_ENTER, 10, 13]:
                buf = self.buffers[self.looper_index]
                if buf["state"] == "PLAYING": buf["state"] = "QUEUED_STOP"
                elif buf["state"] == "STOPPED": buf["state"] = "QUEUED_PLAY"
                elif buf["state"] == "QUEUED_PLAY": buf["state"] = "STOPPED"
                elif buf["state"] == "QUEUED_STOP": buf["state"] = "PLAYING"
                
            elif key in [curses.KEY_BACKSPACE, curses.KEY_DC, 127, 8]:
                buf = self.buffers[self.looper_index]
                if buf["state"] not in ["RECORDING", "ARMED"]:
                    # Global deletion logic
                    target_id = buf["id"]
                    reset_name = f"Buf {target_id:02d}"
                    for p in self.patterns:
                        for b in p["buffers"]:
                            if b["id"] == target_id:
                                b["state"] = "EMPTY"
                                b["name"] = reset_name
                                b["peak"] = 0.0
                                b["offset_ms"] = 0.0
                                b["sync_beat"] = 0.0
                                b["muted"] = False
                                b["soloed"] = False
                                
                    if target_id in self.clock.buffer_data:
                        del self.clock.buffer_data[target_id]
                    file_path = os.path.join(WORKSPACE_DIR, f"buffer_{target_id:02d}.raw")
                    if os.path.exists(file_path):
                        os.remove(file_path)

        # --- F3 CARDS KEYS ---
        elif self.mode == 3:
            if key in [ord('j'), curses.KEY_DOWN]: 
                self.f3_selected_index = min(len(self.f3_list) - 1, self.f3_selected_index + 1)
            elif key in [ord('k'), curses.KEY_UP]: 
                self.f3_selected_index = max(0, self.f3_selected_index - 1)
            elif key in [curses.KEY_ENTER, 10, 13, ord(' ')]:
                if self.f3_list:
                    dev, _ = self.f3_list[self.f3_selected_index]
                    if dev.name in self.hidden_devices:
                        self.hidden_devices.remove(dev.name)
                    else:
                        self.hidden_devices.add(dev.name)
                    self.last_cache_update = 0 
            
        # --- F1/F2 MENU KEYS ---
        elif self.mode in [1, 2]:
            if key in [ord('j'), curses.KEY_DOWN]: self.selected_index += 1
            elif key in [ord('k'), curses.KEY_UP]: self.selected_index -= 1
            
            elif key == ord('b'):
                opts = [f"{self.buffers[i]['name'][:10].ljust(10)} [{self.buffers[i]['state'][:5]}] ({self.buffers[i]['length']:02d} Bts)" for i in range(32)]
                self.dropdown = {
                    "type": "buffer",
                    "title": "Select Target Buffer (PgUp/PgDn to change beats)",
                    "options": opts,
                    "sel": self.target_buffer_index
                }
                
            elif key in [curses.KEY_ENTER, 10, 13]:
                kind, dev, _ = self.nav_list[self.selected_index]
                if kind == 'device':
                    if dev.index in self.expanded_devices: self.expanded_devices.remove(dev.index)
                    else: self.expanded_devices.add(dev.index)
                    
            elif key == ord('*'):
                kind, dev, c_idx = self.nav_list[self.selected_index]
                if kind == 'device':
                    if len(dev.volume.values) <= 2:
                        target_pair = (0, 1) if len(dev.volume.values) == 2 else (0,)
                        self._toggle_monitor_thread(dev, f"{dev.index}-root", target_pair)
                elif kind == 'pair':
                    self._toggle_monitor_thread(dev, f"{dev.index}-{c_idx[0]}", c_idx)
                    
            elif key == ord('r'):
                kind, dev, c_idx = self.nav_list[self.selected_index]
                if kind == 'device':
                    if len(dev.volume.values) <= 2:
                        self._trigger_record_logic(f"{dev.index}-root")
                elif kind == 'pair':
                    self._trigger_record_logic(f"{dev.index}-{c_idx[0]}")
                    
            elif key in [ord('l'), curses.KEY_RIGHT, ord('h'), curses.KEY_LEFT]:
                kind, dev, c_idx = self.nav_list[self.selected_index]
                step = 0.01 if key in [ord('l'), curses.KEY_RIGHT] else -0.01
                dev = self.pulse.sink_info(dev.index) if self.mode == 1 else self.pulse.source_info(dev.index)
                vols = list(dev.volume.values)
                if kind == 'pair':
                    for idx in c_idx:
                        vols[idx] = max(0, min(self.max_vol, vols[idx] + step))
                else:
                    vols = [max(0, min(self.max_vol, v + step)) for v in vols]
                self.pulse.volume_set(dev, pulsectl.PulseVolumeInfo(vols))

    def _main_loop(self, stdscr):
        stdscr.keypad(True) 
        curses.start_color()
        curses.use_default_colors()
        self._apply_theme()
        curses.curs_set(0)
        stdscr.timeout(20) 
        while self.running:
            self._sync_states() 
            self.draw_ui(stdscr)
            self.handle_input(stdscr)

    def run(self):
        try:
            curses.wrapper(self._main_loop)
        finally:
            self._save_session()
            self._save_config()
            self.clock.stop()

if __name__ == "__main__":
    try:
        AudioTool().run()
    except KeyboardInterrupt:
        pass
