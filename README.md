# Video and Image Archive Tool

A command-line tool for recursively compressing (re-encoding) videos and images.

## Dependencies
The following tools must be installed and available via the system's PATH.
 - [FFmpeg](https://www.ffmpeg.org/), for endcoding and decoding.
 - [ffprobe](https://ffmpeg.org/ffprobe.html), for extracting video metadata. It should come with FFmpeg.
 - [ExifTool](https://exiftool.org/), for extracting metadata from images.
 - [libjxl](https://github.com/libjxl/libjxl), for JPEG-XL. The `cjxl` command should be available.

## Example Usage
Consider the example directory structure:
```
media_archiver
├── input_dir/                     # Input media directory
│   ├── image0.jpg                 # A JPEG image
│   ├── image1.png                 # A PNG image
│   ├── image2.avif                # An AVIF image
│   ├── originally_h264_video.mp4  # An MP4 encoded with h264
│   └── originally_av1_video.mp4   # An MP4 encoded with AV1
└── archive.py                     # This script
```
With the following command [inside a virtual environment](https://docs.python.org/3/library/venv.html),
```bash
python archive.py input_dir output_dir
```
The following will be produced,
```
media_archiver
├── input_dir/                     # Original input directory
├── output_dir/                    # New output has been created
│   ├── image0.jxl                 # JPEG converted to JPEG-XL
│   ├── image1.jxl                 # PNG also converted to JPEG-XL
│   ├── image2.avif                # AVIF not modified
│   ├── originally_h264_video.mkv  # H264 re-encoded and remuxed to MKV
│   └── originally_av1_video.mkv   # AV1 not re-encoded, but remuxed to MKV
├── archive.py                     # This script
├── compress.log                   # Verbose log
├── success.log                    # Log of successfully archived files
└── error.log                      # Log of unsuccessful files
```

## Building
It is possible to compile the application to an executable with PyInstaller.
See [build.md]([./build.md) for instructions.

## Releases
Windows and Linux executables should be available under [releases](./releases).
Only Linux has been tested.

## Detailed Documentation

```
usage: archive.py [-h] [--idname] [--idstart IDSTART] [--idformat {dec,hex}] [--workers WORKERS] [--accel {none,nvidia,amd,intel}] input_dir output_dir

Archive videos (H265/AV1+Opus/MKV) and images (JPEG-XL).

positional arguments:
  input_dir             Source directory root
  output_dir            Destination directory root, auto-created

options:
  -h, --help            show this help message and exit
  --idname              Replace filenames with sequential IDs
  --idstart IDSTART     Starting ID (format per --idformat); implies --idname
  --idformat {dec,hex}  Format of --idstart and generated IDs (default: dec)
  --workers WORKERS     Parallel worker threads (default 2; CPU-bound)
  --accel {none,nvidia,amd,intel}
                        Hardware video encoder/decoder to use (default: none)
```

### Passthrough Codecs
For video files, a video stream with the H265 or AV1 codec will be copied.
Otherwise, it will be re-encoded to H265.
Similarly, all audio streams besides OPUS will be re-encoded.  
The video file will always be remuxed to Matroska.

All images besides JPEG-XL and AVIF will be re-formatted to JPEG-XL, and their extension changed.
JPEG-XL images will be copied. AVIF images will be copied and will retain their `avif`. extension.

### Path Mirroring
When archiving files, their path relative to `input_dir` is preserved and recreated in `output_dir`. 
The `output_dir` will be created if it does not exist.

### File Renaming
Note that for all files except AVIF, the original extension will be replaced with `mkv` or `jxl`.  
The `--idname` flag can be used to replace the original filename with a zero-indexed counter.
The format of the counter can switched from decimal to hexadecimal using `--idformat`.  
The `--idstart` flag will imply `--idname`, but begin from a specified value.
The format of the value read by `--idstart` is the same as that specified by `--idformat`.

### Workers
Multiple workers can process files in parallel using the `--workers` flag.
Their progress will be shown individually through progress bars.
The number of workers is limited to the number of cores on the system.

### Hardware Acceleration
When available, hardware acceleration should noticeably speed up processing, and can be controlled with `--accel`.
Remember to check `error.log` for support issues.

### Logs
The script will also produce three log files in the current working directory. 
These will be appended to on subsequent re-runs of the script.
 1. `compress.log` stores a verbose log of any errors and external tool outputs.
 2. `success.log` tracks which files were compressed or copied.
 3. `error.log` tracks which files were not compress or copied.

