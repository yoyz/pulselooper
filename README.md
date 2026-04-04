# PulseLooper

PulseLooper is a terminal-based audio looper and PulseAudio/PipeWire device manager written in Python. 
It provides real-time monitoring, multi-channel device support, and perfectly quantized multi-track loop recording and playback directly from the command line.

## Features
* **TUI Audio Mixer:** View and control volume for all system input and output devices.
* **Multichannel Support:** Automatically expands interfaces with more than 2 channels (e.g., 18-channel interfaces) into manageable stereo pairs.
* **Quantized Looper (32 Buffers):** Record loops that are strictly synchronized to a global BPM and absolute beat grid.
* **In-Memory Playback:** Loops are cached in RAM and streamed continuously to `pacat` via persistent OS pipes to ensure gapless, zero-latency playback.
* **Real-time VU Metering:** Logarithmic visual volume monitoring for both live inputs and playing buffers.

## Dependencies
This application is designed for Linux systems running PulseAudio or PipeWire (with PulseAudio emulation). 

**System Packages:**
* `pulseaudio-utils` (provides `parec` and `pacat`)

**Python Packages:**
* `pulsectl`
* `numpy`
* `curses` (standard library in Linux)

You can install the Python dependencies via pip:
```bash
pip install pulsectl numpy
```

## Usage
Run the script via Python:
```bash
python3 pulselooper.py
```
*Note: Audio recordings are temporarily saved as 32-bit float raw PCM files in `/tmp/pulselooper/`.*

## Keyboard Controls

### Global Commands
* **F1 / F2 / F3 / F4** (or **1-4**): Switch interface tabs.
* **+ / -**: Increase or decrease global BPM.
* **c**: Toggle metronome click track.
* **q**: Quit application.

### Output / Input Views (F1 & F2)
* **Up / Down (or k/j)**: Navigate device list.
* **Left / Right (or h/l)**: Decrease / Increase volume of the selected device or channel pair.
* **Space / Enter**: Expand or collapse multi-channel devices into stereo pairs.
* **\***: Toggle live visual monitoring for the selected device/pair.
* **r**: Arm the selected device for recording. (Recording starts strictly on the next Beat 1).

### Looper View (F4)
* **Up / Down (or k/j)**: Navigate the 32 loop buffers.
* **Space / Enter**: Queue the selected buffer to Play or Stop (executes on the next downbeat).
* **Left / Right (or h/l)**: Adjust the playback volume of the selected buffer.
* **< / > (or PgDn/PgUp)**: Change the target loop length (in beats) for the selected buffer.
* **Backspace**: Clear the selected buffer and delete its audio file from disk.

