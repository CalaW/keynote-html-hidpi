#!/usr/bin/env python3
"""Improve the text sharpness of a Keynote 15.4 HTML export in place.

Usage:
    python3 patch_keynote_html.py "/path/to/exported HTML folder" --scale 2

Only assets/player/main.js is changed. This is a version-specific, unofficial
patch to Apple's exported HTML player; test playback before a presentation.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


CROP_OLD = (
    'k.drawImage(U,R*zC,m*zC,u*zC,f*zC,0,0,u,f),'
    'this.setStyle(K,"0px","0px",u+"px",f+"px",Fg,null,null)'
)
CROP_NEW = (
    'k.drawImage(U,R,m,u,f,0,0,u,f),'
    'this.setStyle(K,"0px","0px",u/zC+"px",f/zC+"px",Fg,null,null)'
)
SCALE_PATTERN = re.compile(r"const zC=(\d+),_C=")


def patch_player(contents: bytes, scale: int) -> tuple[bytes, bool]:
    source = contents.decode("utf-8")
    matches = list(SCALE_PATTERN.finditer(source))
    if len(matches) != 1:
        raise ValueError("Unsupported Keynote HTML player (render scale marker not found uniquely).")

    old_count = source.count(CROP_OLD)
    new_count = source.count(CROP_NEW)
    current_scale = int(matches[0].group(1))

    if (old_count, new_count) == (1, 0) and current_scale == 1:
        source = source.replace(CROP_OLD, CROP_NEW, 1)
    elif (old_count, new_count) == (0, 1) and current_scale >= 2:
        if current_scale == scale:
            return contents, False
    else:
        raise ValueError("Unsupported or partially modified Keynote HTML player; no changes made.")

    source = SCALE_PATTERN.sub(f"const zC={scale},_C=", source, count=1)
    return source.encode("utf-8"), True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Exported Keynote HTML folder (contains index.html)")
    parser.add_argument(
        "--scale", type=int, choices=(2, 3), default=2,
        help="PDF rasterization scale (default: 2)",
    )
    args = parser.parse_args()

    folder = args.folder.expanduser()
    player = folder / "assets" / "player" / "main.js"
    if not (folder.is_dir() and (folder / "index.html").is_file() and player.is_file()):
        parser.error("Expected a Keynote HTML export folder containing index.html and assets/player/main.js")

    original = player.read_bytes()
    modified, changed = patch_player(original, args.scale)
    if not changed:
        print(f"Already patched at {args.scale}x: {player}")
        return

    # Replace atomically so an interrupted run cannot truncate main.js.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".main.js.", suffix=".tmp", dir=player.parent,
            delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(modified)
        os.chmod(temp_path, player.stat().st_mode)
        os.replace(temp_path, player)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print(f"Patched in place ({args.scale}x): {player}")


if __name__ == "__main__":
    main()
