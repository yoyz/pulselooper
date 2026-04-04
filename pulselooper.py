import curses
import pulsectl
import time
import threading
import numpy as np
import subprocess
import math
import os

# ==========================================
# 1. SYSTEM INITIALIZATION & WORKSPACE
# ==========================================

try:
    with pulsectl.Pulse('rate-detect') as p:
        server_info = p.server_info()
        SYSTEM_RATE = server_info.default_sample_spec.rate
except Exception:
    SYSTEM_RATE = 44100 

WORKSPACE_DIR = "/tmp/pulselooper"
os.makedirs(WORKSPACE_DIR, exist_ok=True)

README_PATH = os.path.join(WORKSPACE_DIR, "README.md")
with open(README_PATH, "w") as f:
    f.write("# PulseLooper Audio Buffers\n\n")
    f.write("This directory contains the raw audio buffers recorded by PulseLooper.\n\n")
    f.write("## Audio Format Specifications\n")
    f.write("- **Format:** 32-bit Float Little-Endian (`float32le`)\n")
    f.write(f"- **Sample Rate:** `{SYSTEM_RATE}` Hz (Auto-detected)\n")
    f.write("- **Channels:** 2 (Stereo Normalized)\n\n")
    f.write("## Playback Examples\n")
    f.write("**Using pacat (PipeWire/PulseAudio native):**\n")
    f.write("```bash\n")
    f.write(f"pacat --format=float32le --rate={SYSTEM_RATE} --channels=2 < buffer_01.raw\n")
    f.write("```\n")
    f.write("**Using ffplay:**\n")
    f.write("```bash\n")
    f.write(f"ffplay -f f32le -ar {SYSTEM_RATE} -ac 2 buffer_01.raw\n")
    f.write("```\n")

# ==========================================
# 2. AUDIO ENGINE & CLOCK
# ==========================================

class TempoClock(threading.Thread):
    def __init__(self, bpm, buffers):
        super().__init__(daemon=True)
        self.bpm = bpm
        self.buffers = buffers 
        self.running = True
        
        self.absolute_beat = 0
        self.current_beat = 1
        
        self.play_metronome = False
        self.metro_process = None
        
        self.tick_high = self._generate_tick(1200) 
        self.tick_low = self._generate_tick(600)   
        
        self.play_pipes = {}
        self.buffer_data = {}

    def _generate_tick(self, freq):
        duration = 0.01
        t = np.linspace(0, duration, int(SYSTEM_RATE * duration), False)
        envelope = np.exp(-t * 300) 
        audio = np.sin(freq * t * 2 * np.pi) * envelope * 0.15 
        silence = np.zeros(int(SYSTEM_RATE * 0.05), dtype=np.float32)
        return np.concatenate((audio, silence)).astype(np.float32).tobytes()

    def _start_metro(self):
        if self.metro_process is None:
            cmd = ['pacat', '--format=float32le', f'--rate={SYSTEM_RATE}', '--channels=1', '--latency-msec=10']
            self.metro_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _stop_metro(self):
        if self.metro_process:
            try: self.metro_process.stdin.close()
            except: pass
            self.metro_process.terminate()
            self.metro_process.wait()
            self.metro_process = None

    def _start_buffer_playback(self, buf_id):
        file_path = os.path.join(WORKSPACE_DIR, f"buffer_{buf_id:02d}.raw")
        if not os.path.exists(file_path): 
            return False
            
        try:
            with open(file_path, "rb") as f:
                self.buffer_data[buf_id] = f.read()
        except Exception:
            return False
            
        cmd = ['pacat', '--format=float32le', f'--rate={SYSTEM_RATE}', '--channels=2', '--latency-msec=10']
        try:
            self.play_pipes[buf_id] = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            return True
        except:
            return False

    def _stop_buffer_playback(self, buf_id):
        if buf_id in self.play_pipes:
            try: self.play_pipes[buf_id].stdin.close()
            except: pass
            self.play_pipes[buf_id].terminate()
            self.play_pipes[buf_id].wait()
            del self.play_pipes[buf_id]
        if buf_id in self.buffer_data:
            del self.buffer_data[buf_id]

    def _push_data_async(self, pipe, data, buf):
        try:
            # Chunk the RAM data into 4096-byte segments (~11ms)
            # This causes the OS pipe to block naturally, perfectly synchronizing our loop to real-time playback
            chunk_size = 4096 
            for i in range(0, len(data), chunk_size):
                chunk = data[i:i+chunk_size]
                vol = buf.get("vol", 1.0)
                
                # Apply real-time volume scaling
                if vol != 1.0:
                    samples = np.frombuffer(chunk, dtype=np.float32) * vol
                    out_data = samples.astype(np.float32).tobytes()
                else:
                    samples = np.frombuffer(chunk, dtype=np.float32)
                    out_data = chunk
                
                # Calculate Peak for the real-time VU meter
                if len(samples) > 0:
                    raw_peak = np.max(np.abs(samples))
                    buf["peak"] = max(raw_peak, buf.get("peak", 0.0) * 0.7)
                    
                pipe.stdin.write(out_data)
                pipe.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass 

    def run(self):
        next_beat_time = time.time()
        
        while self.running:
            if self.play_metronome and self.metro_process is None:
                self._start_metro()
            elif not self.play_metronome and self.metro_process is not None:
                self._stop_metro()

            beat_duration = 60.0 / self.bpm
            next_beat_time += beat_duration
            
            sleep_time = next_beat_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_beat_time = time.time() 

            self.absolute_beat += 1
            self.current_beat = ((self.absolute_beat - 1) % 4) + 1
            is_downbeat = (self.current_beat == 1)
            
            for buf in self.buffers:
                buf_id = buf["id"]
                
                if buf["state"] == "QUEUED_PLAY" and is_downbeat:
                    buf["state"] = "PLAYING"
                    buf["sync_beat"] = self.absolute_beat
                elif buf["state"] == "QUEUED_STOP" and is_downbeat:
                    buf["state"] = "STOPPED"
                    self._stop_buffer_playback(buf_id)
                    
                if buf["state"] == "PLAYING":
                    if (self.absolute_beat - buf.get("sync_beat", 0)) % buf["length"] == 0:
                        if buf_id not in self.play_pipes:
                            self._start_buffer_playback(buf_id)
                        if buf_id in self.play_pipes:
                            data_cache = self.buffer_data.get(buf_id, b"")
                            threading.Thread(target=self._push_data_async, args=(self.play_pipes[buf_id], data_cache, buf), daemon=True).start()
            
            if self.play_metronome and self.metro_process and is_downbeat:
                try:
                    self.metro_process.stdin.write(self.tick_high)
                    self.metro_process.stdin.flush()
                except BrokenPipeError:
                    self._stop_metro()

        self._stop_metro()
        for buf_id in list(self.play_pipes.keys()):
            self._stop_buffer_playback(buf_id)

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
        
        self.frame_bytes = 4 * self.device_channels
        self.target_bytes = 0

    def trigger_record(self, buffer_id, target_beats):
        self.armed_buffer_id = buffer_id
        self.target_beats = target_beats
        self.is_armed = True 
        self.record_file = open(os.path.join(WORKSPACE_DIR, f"buffer_{buffer_id:02d}.raw"), "wb")

    def stop_record(self):
        self.is_armed = False
        self.is_recording = False
        if self.record_file:
            self.record_file.close()
            self.record_file = None

    def run(self):
        cmd = ['parec', f'--device={self.source_name}', '--format=float32le', f'--rate={SYSTEM_RATE}', f'--channels={self.device_channels}', '--latency-msec=10']
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
                
                if self.is_armed and is_downbeat:
                    self.is_armed = False
                    self.is_recording = True
                    self.recorded_bytes = 0
                    
                    frames_per_beat = SYSTEM_RATE * (60.0 / self.clock.bpm)
                    self.target_bytes = int(frames_per_beat * self.target_beats) * 2 * 4 

                try:
                    chunk = os.read(fd, 32768)
                    if not chunk:
                        time.sleep(0.01)
                        continue
                        
                    data = leftover_bytes + chunk
                    safe_length = len(data) - (len(data) % self.frame_bytes)
                    if safe_length == 0: 
                        leftover_bytes = data
                        continue
                        
                    leftover_bytes = data[safe_length:]
                    valid_data = data[:safe_length]
                    
                    all_samples = np.frombuffer(valid_data, dtype=np.float32).reshape(-1, self.device_channels)
                    stereo_samples = all_samples[:, self.target_pair]
                    stereo_bytes = stereo_samples.tobytes()
                    
                    if self.is_recording and self.record_file:
                        remaining = self.target_bytes - self.recorded_bytes
                        if len(stereo_bytes) >= remaining:
                            self.record_file.write(stereo_bytes[:remaining])
                            self.stop_record() 
                        else:
                            self.record_file.write(stereo_bytes)
                            self.recorded_bytes += len(stereo_bytes)

                    if len(stereo_samples) > 0:
                        raw_peak = np.max(np.abs(stereo_samples))
                        self.rms_level = max(raw_peak, self.rms_level * 0.7)
                except BlockingIOError:
                    time.sleep(0.01)
                    self.rms_level *= 0.90 
        except Exception:
            self.rms_level = 0.0

    def stop(self):
        self.running = False
        self.stop_record()
        if self.process:
            self.process.terminate()
            self.process.wait()

# ==========================================
# 3. MAIN TUI INTERFACE
# ==========================================

class AudioTool:
    def __init__(self):
        self.pulse = pulsectl.Pulse('pulselooper-master')
        self.mode = 1 
        self.selected_index = 0
        self.looper_index = 0 
        self.running = True
        self.max_vol = 1.5
        self.expanded_devices = set()
        self.monitors = {} 
        self.nav_list = [] 
        self.last_cache_update = 0
        self.cached_devices = []
        
        self.buffers = [{"id": i, "state": "EMPTY", "length": 4, "vol": 1.0, "sync_beat": 0, "peak": 0.0} for i in range(1, 33)]
        self.clock = TempoClock(bpm=120, buffers=self.buffers)
        self.clock.start()

    def get_nav_items(self):
        now = time.time()
        if now - self.last_cache_update > 0.5:
            try:
                self.cached_devices = self.pulse.sink_list() if self.mode == 1 else self.pulse.source_list()
                self.last_cache_update = now
            except: pass
        
        items = []
        for dev in self.cached_devices:
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
        if width <= 10: return
        bar_width = width - 10
        stop_100 = int(bar_width * (1.0 / self.max_vol))
        v_pos = int(bar_width * (min(vol, self.max_vol) / self.max_vol))
        
        visual_peak = math.pow(peak, 0.45) if peak > 0 else 0
        p_pos = int(bar_width * (min(visual_peak, self.max_vol) / self.max_vol))

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

    def draw_f4_looper(self, stdscr, h, w):
        beat_str = " ".join([f"[{i}]" if i == self.clock.current_beat else f" {i} " for i in range(1, 5)])
        metro_status = "ON" if self.clock.play_metronome else "OFF"
        
        header_text = f"TEMPO: {self.clock.bpm:03d} BPM (+/-)  |  BEAT: {beat_str}  |  METRO [c]: {metro_status}"
        stdscr.addstr(3, 2, header_text, curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(4, 2, "─" * (w - 4))
        
        visible_rows = h - 8
        start_idx = max(0, min(self.looper_index - visible_rows // 2, 32 - visible_rows))
        
        for i in range(start_idx, min(32, start_idx + visible_rows)):
            buf = self.buffers[i]
            row = 6 + (i - start_idx)
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
            
            stdscr.addstr(row, 2, f"{pref}Buf {buf['id']:02d} [{buf['length']:02d} Bts]: ", attr)
            stdscr.addstr(row, 21, f"[{state.center(11)}]", color)
            
            # Dynamic VU Meter logic
            peak_val = buf.get("peak", 0.0)
            bar_width = max(10, w - 40)
            self.draw_bar(stdscr, row, 35, bar_width, buf["vol"], peak_val, False)
            
        stdscr.addstr(h-2, 2, "Space: Play/Stop  |  PgUp/PgDn: Change Beats  |  h / l: Change Vol  |  Backspace: Clear", curses.A_DIM)

    def draw_ui(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        
        for i, name in enumerate([" F1: Output ", " F2: Input ", " F3: Cards ", " F4: Looper "]):
            attr = curses.color_pair(1) | curses.A_REVERSE if self.mode == i+1 else curses.A_NORMAL
            stdscr.addstr(0, i*15, name, attr)
        stdscr.addstr(1, 0, "─" * w)

        if self.mode == 4:
            self.draw_f4_looper(stdscr, h, w)
        elif self.mode == 3:
            stdscr.addstr(3, 2, "Hardware / Cards view coming soon...", curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(5, 2, "Press F1, F2, or F4 to navigate away.", curses.A_DIM)
        else:
            self.nav_list = self.get_nav_items()
            if not self.nav_list:
                stdscr.addstr(3, 2, "Waiting for PulseAudio...")
                stdscr.refresh()
                return

            self.selected_index = max(0, min(self.selected_index, len(self.nav_list)-1))
            
            y = 3
            for idx, item in enumerate(self.nav_list):
                if y >= h - 2: break
                kind, dev, c_idx = item
                is_sel = (idx == self.selected_index)
                
                if kind == 'device':
                    vol = sum(dev.volume.values)/len(dev.volume.values)
                    mon_state = "     "
                    peak = 0
                    
                    is_multichannel = len(dev.volume.values) > 2
                    if not is_multichannel:
                        mon_key = f"{dev.index}-root"
                        if mon_key in self.monitors:
                            m = self.monitors[mon_key]
                            peak = m.rms_level
                            if m.is_recording: mon_state = "[REC]"
                            elif m.is_armed: mon_state = "[ARM]"
                            else: mon_state = "[M]  "

                    pref = "> " if is_sel else "  "
                    icon = "▼ " if dev.index in self.expanded_devices else "▶ "
                    is_rec_or_arm = "[REC]" in mon_state or "[ARM]" in mon_state
                    attr = curses.color_pair(3) | curses.A_BOLD if is_rec_or_arm else (curses.color_pair(1) | curses.A_BOLD if is_sel else curses.A_NORMAL)
                    
                    extra_hint = " (Unfold for Pairs)" if is_multichannel and is_sel and not dev.index in self.expanded_devices else ""
                    stdscr.addstr(y, 2, f"{pref}{mon_state}{icon}{dev.description[:w-45]}{extra_hint}", attr)
                    y += 1
                    self.draw_bar(stdscr, y, 9, w, vol, peak, dev.mute)
                    y += 2
                
                elif kind == 'pair':
                    vol = sum(dev.volume.values[i] for i in c_idx) / len(c_idx)
                    mon_key = f"{dev.index}-{c_idx[0]}"
                    mon_state = "     "
                    peak = 0
                    
                    if mon_key in self.monitors:
                        m = self.monitors[mon_key]
                        peak = m.rms_level
                        if m.is_recording: mon_state = "[REC]"
                        elif m.is_armed: mon_state = "[ARM]"
                        else: mon_state = "[M]  "
                    
                    pref = "> " if is_sel else "  "
                    is_rec_or_arm = "[REC]" in mon_state or "[ARM]" in mon_state
                    attr = curses.color_pair(3) | curses.A_BOLD if is_rec_or_arm else (curses.color_pair(1) | curses.A_BOLD if is_sel else curses.A_DIM)
                    
                    label = f"Ch {c_idx[0]+1}/{c_idx[1]+1}" if len(c_idx) == 2 else f"Ch {c_idx[0]+1}"
                    stdscr.addstr(y, 11, f"{pref}{mon_state}{label}", attr)
                    self.draw_bar(stdscr, y, 28, w-15, vol, peak, dev.mute)
                    y += 1

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
                if mon.armed_buffer_id is not None:
                    self.buffers[mon.armed_buffer_id - 1]["state"] = "PLAYING" 
            else:
                for buf in self.buffers:
                    if buf["state"] == "EMPTY":
                        buf["state"] = "ARMED"
                        mon.trigger_record(buf["id"], buf["length"])
                        break

    def _sync_states(self):
        # Decay peaks for the UI
        for buf in self.buffers:
            buf["peak"] = buf.get("peak", 0.0) * 0.85
            
        for mon_key, m in self.monitors.items():
            if m.armed_buffer_id is not None:
                buf_idx = m.armed_buffer_id - 1
                if m.is_recording:
                    self.buffers[buf_idx]["state"] = "RECORDING"
                elif not m.is_armed and not m.is_recording:
                    if self.buffers[buf_idx]["state"] in ["RECORDING", "ARMED"]:
                        self.buffers[buf_idx]["state"] = "PLAYING"
                        self.buffers[buf_idx]["sync_beat"] = self.clock.absolute_beat
                        
                        self.clock._start_buffer_playback(buf_idx + 1)
                        if (buf_idx + 1) in self.clock.play_pipes:
                            threading.Thread(target=self.clock._push_data_async, args=(self.clock.play_pipes[buf_idx+1], self.clock.buffer_data.get(buf_idx+1, b""), self.buffers[buf_idx]), daemon=True).start()
                    
                    m.armed_buffer_id = None

    def handle_input(self, stdscr):
        key = stdscr.getch()
        if key == -1: return
        if key == ord('q'): self.running = False
        elif key in [ord('1'), curses.KEY_F1]: self.mode = 1; self.last_cache_update = 0
        elif key in [ord('2'), curses.KEY_F2]: self.mode = 2; self.last_cache_update = 0
        elif key in [ord('3'), curses.KEY_F3]: self.mode = 3; self.last_cache_update = 0
        elif key in [ord('4'), curses.KEY_F4]: self.mode = 4
        elif key == ord('c'):
            self.clock.play_metronome = not self.clock.play_metronome
            
        elif key in [ord('+'), ord('=')]:
            self.clock.bpm = min(300, self.clock.bpm + 1)
        elif key in [ord('-'), ord('_')]:
            self.clock.bpm = max(30, self.clock.bpm - 1)
        
        elif self.mode == 4:
            if key in [ord('j'), curses.KEY_DOWN]: self.looper_index = min(31, self.looper_index + 1)
            elif key in [ord('k'), curses.KEY_UP]: self.looper_index = max(0, self.looper_index - 1)
            
            elif key in [ord('l'), curses.KEY_RIGHT]:
                buf = self.buffers[self.looper_index]
                buf["vol"] = min(self.max_vol, buf["vol"] + 0.05)
            elif key in [ord('h'), curses.KEY_LEFT]:
                buf = self.buffers[self.looper_index]
                buf["vol"] = max(0.0, buf["vol"] - 0.05)
                
            elif key in [ord('>'), ord('.'), curses.KEY_PPAGE]:
                buf = self.buffers[self.looper_index]
                buf["length"] = min(64, buf["length"] + 1)
            elif key in [ord('<'), ord(','), curses.KEY_NPAGE]:
                buf = self.buffers[self.looper_index]
                buf["length"] = max(1, buf["length"] - 1)
            
            elif key in [ord(' '), ord('\n')]:
                buf = self.buffers[self.looper_index]
                if buf["state"] == "PLAYING": buf["state"] = "QUEUED_STOP"
                elif buf["state"] == "STOPPED": buf["state"] = "QUEUED_PLAY"
                elif buf["state"] == "QUEUED_PLAY": buf["state"] = "STOPPED"
                elif buf["state"] == "QUEUED_STOP": buf["state"] = "PLAYING"
                
            elif key in [curses.KEY_BACKSPACE, 127, 8]:
                buf = self.buffers[self.looper_index]
                if buf["state"] not in ["RECORDING", "ARMED"]:
                    buf["state"] = "EMPTY"
                    buf["peak"] = 0.0
                    self.clock._stop_buffer_playback(buf["id"])
                    file_path = os.path.join(WORKSPACE_DIR, f"buffer_{buf['id']:02d}.raw")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        
        elif self.mode == 3:
            pass # Empty page logic
            
        elif self.mode in [1, 2]:
            if key in [ord('j'), curses.KEY_DOWN]: self.selected_index += 1
            elif key in [ord('k'), curses.KEY_UP]: self.selected_index -= 1
            elif key in [ord(' '), ord('\n')]:
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
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
        curses.curs_set(0)
        stdscr.timeout(20) 
        while self.running:
            self._sync_states() 
            self.draw_ui(stdscr)
            self.handle_input(stdscr)

    def run(self):
        curses.wrapper(self._main_loop)

if __name__ == "__main__":
    AudioTool().run()
