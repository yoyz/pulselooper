# PulseLooper

PulseLooper is a terminal-based audio looper and PulseAudio/PipeWire device manager written in Python. 
It provides real-time monitoring, multi-channel device support, perfectly quantized multi-track loop recording, and full Ableton-style live session arrangement directly from the command line.

## Features
* **TUI Audio Mixer:** View and control volume for all system input and output devices with dynamic scrolling.
* **Multichannel Support:** Automatically expands interfaces with more than 2 channels (e.g., 18-channel interfaces) into manageable stereo pairs.
* **Hardware Manager (F3):** Easily hide/show active soundcards from your workspace to reduce UI clutter.
* **Quantized Looper (32 Buffers):** Record loops that are strictly synchronized to a global BPM and absolute beat grid. 
* **16 Live Patterns (Scenes):** Create up to 16 full song arrangements. Duplicate patterns, mute/solo tracks, and launch them dynamically on the downbeat.
* **Native In-Memory PyAudio Playback:** Loops are cached in RAM and streamed continuously via PyAudio for gapless, sample-accurate, zero-latency playback.
* **Micro-Timing & Latency Comp:** Shift tracks backward or forward by milliseconds to adjust the groove or perfectly counteract hardware recording latency.
* **Offline WAV Export:** Render your currently playing mix (with volume, pitch, mutes, solos, and micro-timings applied) directly to disk as a `.wav` file.
* **Persistent Sessions:** Save different projects into custom directories. Global configurations and session states are automatically restored.

## Dependencies
This application is designed for Linux systems running PulseAudio or PipeWire (with PulseAudio emulation). 

**System Packages:**
* `pulseaudio-utils` (provides `parec` for flawless input routing)

**Python Packages:**
* `pulsectl`
* `numpy`
* `pyaudio`
* `curses` (standard library in Linux)

You can install the Python dependencies via pip:
```bash
pip install pulsectl numpy pyaudio
Usage
```

Run the script via Python. By default, it will save your project to /tmp/pulselooper/.

You can also specify a custom session directory:

```bash

python3 pulselooper.py /path/to/mysong
```

Note: Audio recordings are saved as 32-bit float raw PCM files in your session folder, alongside a session.json state file and a debug.log file.

Keyboard Controls
Global Commands
F1 / F2 / F3 / F4 (or 1-4): Switch interface tabs.

+ / -: Increase or decrease global BPM.

t: Open Time Signature menu.

c: Toggle metronome click track (except in F4 view where c means copy).

o: Open Options menu (Input Latency Compensation, Output Delay, UI Themes).

q: Quit application.

Output / Input Views (F1 & F2)
Up / Down (or k/j): Navigate device list (supports vertical scrolling).

Left / Right (or h/l): Decrease / Increase volume of the selected device or channel pair.

Space / Enter: Expand or collapse multi-channel devices into stereo pairs.

b: Assign the incoming audio to a specific Loop Buffer.

*: Toggle live visual monitoring for the selected device/pair.

r: Arm the selected device for recording. (Recording starts strictly on the next Beat 1).

Cards Configuration (F3)
Up / Down (or k/j): Navigate hardware list.

Space / Enter: Hide or unhide the hardware from the F1 and F2 mixer views.

Looper View (F4)
Up / Down (or k/j): Navigate the 32 loop buffers.

Shift+Up / Shift+Down (or K/J): Move a buffer slot up or down in the rack.

Space / Enter: Queue the selected buffer to Play or Stop (executes gracefully on the next downbeat).

m / s: Toggle Mute or Solo for the track.

Left / Right (or h/l): Adjust the playback volume of the selected buffer.

[ / ] (or PgDn/PgUp): Change the target loop length (in beats) for the selected buffer.

< / >: Micro-Timing Phase Shift. Nudge the playback of the sample backward/forward by 1ms (hold to accelerate).

c / v: Copy and Paste buffer states and audio data.

r: Rename the selected buffer (in-place text edit).

p: Open the Pattern Menu to switch scenes (supports c/v copy paste, J/K reordering, and r to rename patterns).

e: Export the current playing mix as a WAV file.

Backspace: Clear the selected buffer and delete its audio file from disk.
