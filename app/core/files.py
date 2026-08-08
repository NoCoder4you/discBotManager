import os, tempfile
from pathlib import Path
def safe_path(root:Path,relative:str)->Path:
    if Path(relative).is_absolute(): raise ValueError("Absolute paths are forbidden")
    root=root.resolve(strict=True); candidate=(root/relative).resolve(strict=False)
    if not candidate.is_relative_to(root): raise ValueError("Path escapes configured root")
    return candidate
def atomic_write(path:Path,data:bytes)->None:
    path=path.resolve(); fd,temp=tempfile.mkstemp(dir=path.parent,prefix=f".{path.name}.")
    try:
        with os.fdopen(fd,"wb") as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
    except BaseException:
        try: os.unlink(temp)
        except FileNotFoundError: pass
        raise
