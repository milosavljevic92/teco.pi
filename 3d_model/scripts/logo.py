"""Company logo (assets/logo.png, dark on light / transparent) -> shapely polygon in mm,
centred on (0, 0), y up, `height` mm tall."""
import os
import numpy as np
from PIL import Image
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely import affinity

PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'logo.png')

def logo_polygon(height, png=PNG, simplify=0.04):
    import contourpy
    im = np.asarray(Image.open(png).convert('RGBA')).astype(float)
    lum = im[..., :3].mean(-1) * (im[..., 3] / 255.0) + 255.0 * (1 - im[..., 3] / 255.0)   # on white
    ink = (lum < 128).astype(float)
    ink = np.pad(ink, 2)                                    # closed contours at the border
    lines = contourpy.contour_generator(z=ink).lines(0.5)
    shape = Polygon()
    for ln in lines:
        if len(ln) >= 4:
            p = Polygon(ln).buffer(0)
            shape = shape.symmetric_difference(p)           # even-odd: rings inside rings are holes
    shape = affinity.scale(shape, 1, -1, origin=(0, 0))     # image rows go down
    x0, y0, x1, y1 = shape.bounds
    s = height / (y1 - y0)
    shape = affinity.scale(shape, s, s, origin=(0, 0))
    x0, y0, x1, y1 = shape.bounds
    shape = affinity.translate(shape, -(x0 + x1) / 2, -(y0 + y1) / 2)
    return shape.simplify(simplify).buffer(0)

if __name__ == '__main__':
    import sys
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    p = logo_polygon(20.0)
    fig, ax = plt.subplots(figsize=(5, 5))
    for g in getattr(p, 'geoms', [p]):
        ax.fill(*g.exterior.xy, color='k')
        for r in g.interiors:
            ax.fill(*r.xy, color='w')
    ax.set_aspect('equal'); fig.savefig(sys.argv[1]); print(p.bounds, p.area, len(getattr(p, 'geoms', [p])))
