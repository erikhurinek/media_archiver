# Building archive.py into a standalone binary

## 1. Virtual Environment Setup
```
python -m venv build-venv
source build-venv/bin/activate
pip install pyinstaller tqdm
```

## 2. Compile
`pyinstaller --onefile --name archive archive.py`

## 3. Install to your user bin
```
mkdir -p ~/.local/bin
cp dist/archive ~/.local/bin/archive
chmod +x ~/.local/bin/archive
```

## Check PATH
Ensure `~/.local/bin` is on PATH (add to `~/.zshrc` if not already):
`export PATH="$HOME/.local/bin:$PATH"`

## 4. Clean up build artifacts (optional)
```
deactivate
rm -rf build-venv build dist archive.spec __pycache__
```

## 5. Install required system binaries (Arch)
`sudo pacman -S ffmpeg libjxl`

## Usage
```
archive /path/to/input /path/to/output
archive /path/to/input /path/to/output --idname --idformat hex --idstart 1a
```
