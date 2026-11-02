#!/usr/bin/env python3
"""Patch a Keynote 15.4 HTML export in place for sharper animated elements.

Usage:
    python3 patch_keynote_html.py "/path/to/exported HTML folder" --scale 2

Modifies only assets/player/main.js. Supports the original Keynote 15.4 export,
the earlier PDF-only 2x/3x patch, and this script's own output. This is an
unofficial, version-specific workaround; test animations on the target device.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


# The Apple player minifies these names, so we deliberately match a full,
# version-specific expression rather than doing broad substitutions.
SCALE = re.compile(r"const zC=(\d+),_C=")
V2_MARKER = "/* keynote-hidpi-animated-v2 */"

CROP_V0 = (
    'k.drawImage(U,R*zC,m*zC,u*zC,f*zC,0,0,u,f),'
    'this.setStyle(K,"0px","0px",u+"px",f+"px",Fg,null,null)'
)
CROP_V1 = (
    'k.drawImage(U,R,m,u,f,0,0,u,f),'
    'this.setStyle(K,"0px","0px",u/zC+"px",f/zC+"px",Fg,null,null)'
)

# In a contents animation the *source* is already a high-resolution PDF
# canvas. Rendering it at 2x the CSS size, then scaling the DOM node back down,
# creates an additional composited transform. Use the native high-DPI backing
# canvas with the original logical CSS bounds instead.
CONTENTS_V0 = (
    'j>1&&q>1&&0!==j&&0!==q?('
    'this.setStyle(X,"0px","0px",J+"px",H+"px",Fg,null,null),'
    'E(X,xC,"0% 0%"),E(X,OC,"scale("+1/j+","+1/q+")")):'
    'this.setStyle(X,"0px","0px",Y+"px",c+"px",Fg,null,null)'
)
CONTENTS_V2 = 'this.setStyle(X,"0px","0px",Y+"px",c+"px",Fg,null,null)'

# GL canvases previously had only slideWidth x slideHeight backing pixels.
# Keep all matrices and texture geometry in *logical* slide coordinates;
# render them to a separate high-density drawing buffer instead.
GL_CANVAS_V0 = (
    'createCanvasElement(A){var B=document.createElement("canvas");'
    'return B.width=UC.script.slideWidth,B.height=UC.script.slideHeight,'
    'B.setAttribute("id",A),B}'
)
GL_CANVAS_V2 = (
    'createCanvasElement(A){var B=document.createElement("canvas");'
    'return B.width=UC.script.slideWidth*zC,B.height=UC.script.slideHeight*zC,'
    'B.style.width=UC.script.slideWidth+"px",'
    'B.style.height=UC.script.slideHeight+"px",'
    'B.setAttribute("id",A),B}'
)
GL_CONTEXT_V0 = (
    'var g=this.gl=B.getContext("webgl")||B.getContext("experimental-webgl");'
    'g?(this.animationStarted=!1,g.viewportWidth=B.width,g.viewportHeight=B.height,'
    'this.initMVPMatrix(),this.coreAnimationWrapper=new QB(g)):this.noGL=!0'
)
GL_CONTEXT_V2 = (
    'var g=this.gl=B.getContext("webgl")||B.getContext("experimental-webgl");'
    'g?(this.animationStarted=!1,g.viewportWidth=B.width/zC,'
    'g.viewportHeight=B.height/zC,g.viewport(0,0,B.width,B.height),'
    'this.initMVPMatrix(),this.coreAnimationWrapper=new QB(g)):this.noGL=!0'
)
# WebGL effects sometimes render into an offscreen FBO and then reset the
# viewport. Upon returning to the *default* framebuffer, use its pixel size.
FLAME_VIEWPORT_V0 = 'Q.viewport(0,0,Q.viewportWidth,Q.viewportHeight),Q.blendFunc('
FLAME_VIEWPORT_V2 = 'Q.viewport(0,0,Q.canvas.width,Q.canvas.height),Q.blendFunc('
FIREWORKS_VIEWPORT_V0 = 'C.viewport(0,0,C.viewportWidth,C.viewportHeight),K.unbindFramebufferAndBindGLFramebuffer(M)'
FIREWORKS_VIEWPORT_V2 = 'C.viewport(0,0,C.canvas.width,C.canvas.height),K.unbindFramebufferAndBindGLFramebuffer(M)'
# Each new rendered frame starts on the full-resolution default framebuffer.
GL_DRAW_V0 = 'var t=this.gl;t.clearColor(0,0,0,0),t.clear(t.COLOR_BUFFER_BIT|t.DEPTH_BUFFER_BIT);'
GL_DRAW_V2 = (
    'var t=this.gl;t.bindFramebuffer(t.FRAMEBUFFER,null),'
    't.viewport(0,0,this.canvas.width,this.canvas.height),'
    't.clearColor(0,0,0,0),t.clear(t.COLOR_BUFFER_BIT|t.DEPTH_BUFFER_BIT);'
)

# Smart Build uses a separate, nested GL canvas, not createCanvasElement().
SMART_CANVAS_V0 = (
    'W.width=B.initialState.width,W.height=B.initialState.height,'
    'W.setAttribute("id",y);'
)
SMART_CANVAS_V2 = (
    'W.width=B.initialState.width*zC,W.height=B.initialState.height*zC,'
    'W.style.width=B.initialState.width+"px",'
    'W.style.height=B.initialState.height+"px",W.setAttribute("id",y);'
)

REPLACEMENTS = (
    (CONTENTS_V0, CONTENTS_V2, 'contents animation canvas'),
    (GL_CANVAS_V0, GL_CANVAS_V2, 'WebGL transition canvas'),
    (GL_CONTEXT_V0, GL_CONTEXT_V2, 'WebGL logical coordinates'),
    (FLAME_VIEWPORT_V0, FLAME_VIEWPORT_V2, 'flame framebuffer restore'),
    (FIREWORKS_VIEWPORT_V0, FIREWORKS_VIEWPORT_V2, 'fireworks framebuffer restore'),
    (GL_DRAW_V0, GL_DRAW_V2, 'WebGL animation frame'),
    (SMART_CANVAS_V0, SMART_CANVAS_V2, 'Smart Build canvas'),
)


def patch_player(raw: bytes, scale: int) -> tuple[bytes, bool]:
    data = raw.decode('utf-8')
    markers = list(SCALE.finditer(data))
    if len(markers) != 1:
        raise ValueError('Unsupported player: unique Keynote 15.4 render scale not found.')
    old_scale = int(markers[0].group(1))
    if V2_MARKER in data:
        if data.count(V2_MARKER) != 1:
            raise ValueError('Corrupt / partially patched v2 player.')
        if old_scale == scale:
            return raw, False
        # Verify important v2 code before changing the scale.
        for _, new, label in REPLACEMENTS:
            if data.count(new) != 1:
                raise ValueError(f'Unexpected v2 player structure: {label}.')
        if data.count(CROP_V1) != 1:
            raise ValueError('Unexpected v2 player cropping code.')
    else:
        counts = (data.count(CROP_V0), data.count(CROP_V1))
        if counts == (1, 0) and old_scale == 1:
            data = data.replace(CROP_V0, CROP_V1, 1)
        elif counts != (0, 1) or old_scale not in (2, 3):
            raise ValueError('Expected original Keynote 15.4 or the previous PDF-only patch.')
        for old, new, label in REPLACEMENTS:
            if data.count(old) != 1 or (old != CONTENTS_V0 and data.count(new) != 0):
                raise ValueError(f'Unsupported player: {label} pattern not found uniquely.')
        for old, new, _ in REPLACEMENTS:
            data = data.replace(old, new, 1)
        data = data.replace('const zC=' + str(old_scale) + ',_C=',
                            V2_MARKER + 'const zC=' + str(old_scale) + ',_C=', 1)
    data = SCALE.sub('const zC=' + str(scale) + ',_C=', data, count=1)
    return data.encode('utf-8'), True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path, help='Keynote exported HTML directory, containing index.html')
    parser.add_argument('--scale', type=int, choices=(2, 3), default=2,
                        help='Backing pixel scale (default: 2)')
    args = parser.parse_args()
    folder = args.folder.expanduser()
    target = folder / 'assets' / 'player' / 'main.js'
    if not ((folder / 'index.html').is_file() and target.is_file()):
        parser.error('Specify the HTML export folder with index.html and assets/player/main.js')

    original = target.read_bytes()
    patched, changed = patch_player(original, args.scale)
    if not changed:
        print(f'Already patched at {args.scale}x: {target}')
        return

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix='.main.js.', suffix='.tmp',
                                         dir=target.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(patched)
        os.chmod(temp_path, target.stat().st_mode)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    print(f'Patched in place ({args.scale}x, including animated canvases): {target}')


if __name__ == '__main__':
    main()
