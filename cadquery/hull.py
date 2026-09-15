from typing import Dict, List, Tuple, Union, Iterable, Optional
from math import pi, sin, cos, atan2, sqrt, inf, degrees
from numpy import argmin

from .occ_impl.shapes import Edge, Wire, wire
from .occ_impl.geom import Vector


"""
Convex hull for line segments and circular arcs based on
Yue, Y., Murray, J. L., Corney, J. R., & Clark, D. E. R. (1999).
Convex hull of a planar set of straight and circular line segments. Engineering Computations.

"""

Arcs = List["Arc"]
Points = List["Point"]
Entity = Union["Arc", "Point"]
Hull = List[Union["Arc", "Point", "Segment"]]

# minimum arc span; below this makeCircle would return a full circle
TOL = 1e-9
# distance below which two points coincide and a point lies on a circle
EPS = 1e-7


class Point:

    x: float
    y: float

    def __init__(self, x: float, y: float):

        self.x = x
        self.y = y

    def __repr__(self):

        return f"( {self.x},{self.y} )"

    def __hash__(self):

        return hash((self.x, self.y))

    def __eq__(self, other):

        return type(self) == type(other) and (self.x, self.y) == (other.x, other.y)


class Segment:

    a: Point
    b: Point

    def __init__(self, a: Point, b: Point):

        self.a = a
        self.b = b


class Arc:

    c: Point
    s: Point
    e: Point
    r: float
    a1: float
    a2: float
    ac: float

    def __init__(self, c: Point, r: float, a1: float, a2: float):

        self.c = c
        self.r = r
        self.a1 = a1
        self.a2 = a2

        self.s = Point(c.x + r * cos(a1), c.y + r * sin(a1))
        self.e = Point(c.x + r * cos(a2), c.y + r * sin(a2))
        self.ac = 2 * pi - (a1 - a2)

    def covers(self, angle: float) -> bool:

        return (angle - self.a1) % (2 * pi) <= self.a2 - self.a1 + TOL

    def passes(self, p: Point) -> bool:

        dx, dy = p.x - self.c.x, p.y - self.c.y

        return abs(sqrt(dx ** 2 + dy ** 2) - self.r) <= EPS and self.covers(
            atan2p(dx, dy)
        )


def atan2p(x, y):

    rv = atan2(y, x)

    if rv < 0:
        rv = (2 * pi + rv) % (2 * pi)

    return rv


def arc_bounds(e: Edge, c: Point) -> Tuple[float, float]:

    if e.IsClosed():
        return 0.0, 2 * pi

    t1, tm, t2 = (
        atan2p(v.x - c.x, v.y - c.y)
        for v in (e.startPoint(), e.positionAt(0.5), e.endPoint())
    )

    if (tm - t1) % (2 * pi) > (t2 - t1) % (2 * pi):
        t1, t2 = t2, t1

    return t1, t1 + (t2 - t1) % (2 * pi)


def merge_spans(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:

    rv = [spans[0]]

    for a1, a2 in spans[1:]:
        b1, b2 = rv[-1]

        if a1 <= b2 + TOL:
            rv[-1] = b1, max(a2, b2)
        else:
            rv.append((a1, a2))

    if len(rv) > 1 and rv[0][0] + 2 * pi <= rv[-1][1] + TOL:
        a1, a2 = rv.pop()
        rv[0] = a1, max(a2, rv[0][1] + 2 * pi)

    if rv[0][1] - rv[0][0] >= 2 * pi - TOL:
        return [(0.0, 2 * pi)]

    return rv


def add_point(points: Points, x: float, y: float) -> Point:

    for p in points:
        if abs(p.x - x) <= EPS and abs(p.y - y) <= EPS:
            return p

    p = Point(x, y)
    points.append(p)

    return p


def convert_and_validate(edges: Iterable[Edge]) -> Tuple[List[Arc], List[Point]]:

    spans: Dict[Tuple[Point, float], List[Tuple[float, float]]] = {}
    points: Points = []

    for e in edges:
        gt = e.geomType()

        if gt == "LINE":
            for v in (e.startPoint(), e.endPoint()):
                add_point(points, v.x, v.y)

        elif gt == "CIRCLE":
            c = e.arcCenter()
            r = e.radius()
            p = Point(c.x, c.y)

            spans.setdefault((p, r), []).append(arc_bounds(e, p))

        else:
            raise ValueError("Unsupported geometry {gt}")

    arcs = [
        Arc(c, r, a1, a2)
        for (c, r), ss in spans.items()
        for a1, a2 in merge_spans(sorted(ss))
    ]

    # the ends of an arc are entities of their own; a point on an arc adds nothing
    for a in arcs:
        if a.a2 - a.a1 < 2 * pi:
            a.s = add_point(points, a.s.x, a.s.y)
            a.e = add_point(points, a.e.x, a.e.y)

    points = [
        p
        for p in points
        if not any(p is not a.s and p is not a.e and a.passes(p) for a in arcs)
    ]

    return arcs, points


def select_lowest_point(points: Points) -> Tuple[Point, int]:

    y_min = min(p.y for p in points)
    ix = min(
        (i for i, p in enumerate(points) if p.y <= y_min + EPS),
        key=lambda i: points[i].x,
    )

    return points[ix], ix


def select_lowest_arc(arcs: Arcs) -> Optional[Tuple[Point, Arc]]:

    arcs = [a for a in arcs if a.covers(1.5 * pi)]

    if not arcs:
        return None

    p, ix = select_lowest_point([Point(a.c.x, a.c.y - a.r) for a in arcs])

    return p, arcs[ix]


def select_lowest(arcs: Arcs, points: Points) -> Entity:

    rv: Entity

    p_lowest = select_lowest_point(points) if points else None
    a_lowest = select_lowest_arc(arcs) if arcs else None

    if p_lowest is None and a_lowest:
        rv = a_lowest[1]
    elif p_lowest is not None and a_lowest is None:
        rv = p_lowest[0]
    elif p_lowest and a_lowest:
        _, ix = select_lowest_point([p_lowest[0], a_lowest[0]])
        rv = p_lowest[0] if ix == 0 else a_lowest[1]
    else:
        raise ValueError("No entities specified")

    return rv


def pt_pt(p1: Point, p2: Point) -> Tuple[float, Segment]:

    angle = 0

    dx, dy = p2.x - p1.x, p2.y - p1.y

    if (dx, dy) != (0, 0):
        angle = atan2p(dx, dy)

    return angle, Segment(p1, p2)


class NoTangent(Exception):
    pass


def _pt_arc(p: Point, a: Arc) -> Tuple[float, float, float, float]:

    x, y = p.x, p.y

    r = a.r
    xc, yc = a.c.x, a.c.y
    dx, dy = x - xc, y - yc
    l = sqrt(dx ** 2 + dy ** 2)

    if l <= r + EPS:
        raise NoTangent

    x1 = r ** 2 / l ** 2 * dx - r / l ** 2 * sqrt(l ** 2 - r ** 2) * dy + xc
    y1 = r ** 2 / l ** 2 * dy + r / l ** 2 * sqrt(l ** 2 - r ** 2) * dx + yc
    x2 = r ** 2 / l ** 2 * dx + r / l ** 2 * sqrt(l ** 2 - r ** 2) * dy + xc
    y2 = r ** 2 / l ** 2 * dy - r / l ** 2 * sqrt(l ** 2 - r ** 2) * dx + yc

    return x1, y1, x2, y2


def pt_arc(p: Point, a: Arc) -> Tuple[float, Segment]:

    if p is a.s:
        return (a.a1 + pi / 2) % (2 * pi), Segment(p, p)

    x, y = p.x, p.y
    x1, y1, _, _ = _pt_arc(p, a)

    if not a.covers(atan2p(x1 - a.c.x, y1 - a.c.y)):
        raise NoTangent

    return atan2p(x1 - x, y1 - y), Segment(p, Point(x1, y1))


def arc_pt(a: Arc, p: Point) -> Tuple[float, Segment]:

    if p is a.e:
        return (a.a2 + pi / 2) % (2 * pi), Segment(p, p)

    x, y = p.x, p.y
    _, _, x2, y2 = _pt_arc(p, a)

    if not a.covers(atan2p(x2 - a.c.x, y2 - a.c.y)):
        raise NoTangent

    return atan2p(x - x2, y - y2), Segment(Point(x2, y2), p)


def arc_arc(a1: Arc, a2: Arc) -> Tuple[float, Segment]:

    r1 = a1.r
    xc1, yc1 = a1.c.x, a1.c.y

    r2 = a2.r
    xc2, yc2 = a2.c.x, a2.c.y

    # construct tangency points for a related point-circle problem
    if r1 > r2:
        arc_tmp = Arc(a1.c, r1 - r2, a1.a1, a1.a2)
        xtmp1, ytmp1, xtmp2, ytmp2 = _pt_arc(a2.c, arc_tmp)

        delta_r = r1 - r2

        dx1 = (xtmp1 - xc1) / delta_r
        dy1 = (ytmp1 - yc1) / delta_r

        dx2 = (xtmp2 - xc1) / delta_r
        dy2 = (ytmp2 - yc1) / delta_r

    elif r1 < r2:
        arc_tmp = Arc(a2.c, r2 - r1, a2.a1, a2.a2)
        xtmp1, ytmp1, xtmp2, ytmp2 = _pt_arc(a1.c, arc_tmp)

        delta_r = r2 - r1

        dx1 = (xtmp1 - xc2) / delta_r
        dy1 = (ytmp1 - yc2) / delta_r

        dx2 = (xtmp2 - xc2) / delta_r
        dy2 = (ytmp2 - yc2) / delta_r

    else:
        dx = xc2 - xc1
        dy = yc2 - yc1
        l = sqrt(dx ** 2 + dy ** 2)

        # arcs of one circle: the hull crosses the gap between their ends
        if l <= EPS:
            raise NoTangent

        dx /= l
        dy /= l

        dx1 = -dy
        dy1 = dx
        dx2 = dy
        dy2 = -dx

    # construct the tangency points and angles
    x11 = xc1 + dx1 * r1
    y11 = yc1 + dy1 * r1
    x12 = xc1 + dx2 * r1
    y12 = yc1 + dy2 * r1

    x21 = xc2 + dx1 * r2
    y21 = yc2 + dy1 * r2
    x22 = xc2 + dx2 * r2
    y22 = yc2 + dy2 * r2

    a1_out = atan2p(x21 - x11, y21 - y11)
    a2_out = atan2p(x22 - x12, y22 - y12)

    # select the feasible angle
    a11 = (atan2p(x11 - xc1, y11 - yc1) + pi / 2) % (2 * pi)
    a21 = (atan2p(x12 - xc1, y12 - yc1) + pi / 2) % (2 * pi)

    ix = int(argmin((abs(a11 - a1_out), abs(a21 - a2_out))))
    angles = (a1_out, a2_out)
    segments = (
        Segment(Point(x11, y11), Point(x21, y21)),
        Segment(Point(x12, y12), Point(x22, y22)),
    )

    seg = segments[ix]

    if not (
        a1.covers(atan2p(seg.a.x - xc1, seg.a.y - yc1))
        and a2.covers(atan2p(seg.b.x - xc2, seg.b.y - yc2))
    ):
        raise NoTangent

    return angles[ix], seg


NO_TANGENT = inf, Segment(Point(inf, inf), Point(inf, inf))


def get_angle(current: Entity, e: Entity) -> Tuple[float, Segment]:

    if current is e:
        return NO_TANGENT

    try:
        if isinstance(current, Point):
            if isinstance(e, Point):
                return pt_pt(current, e)
            else:
                return pt_arc(current, e)
        else:
            if isinstance(e, Point):
                return arc_pt(current, e)
            else:
                return arc_arc(current, e)
    except NoTangent:
        return NO_TANGENT


def update_hull(
    current_e: Entity,
    ix: int,
    entities: List[Entity],
    angles: List[float],
    segments: List[Segment],
    hull: Hull,
) -> Tuple[Entity, float]:

    next_e = entities[ix]
    connecting_seg = segments[ix]

    if isinstance(next_e, Point):
        entities.pop(ix)

    hull.extend((connecting_seg, next_e))

    return next_e, angles[ix]


def finalize_hull(hull: Hull) -> Wire:

    rv = []

    for el_p, el, el_n in zip(hull, hull[1:], hull[2:]):

        if isinstance(el, Segment):
            if el.a is not el.b:
                rv.append(
                    Edge.makeLine(Vector(el.a.x, el.a.y), Vector(el.b.x, el.b.y))
                )
        elif (
            isinstance(el, Arc)
            and isinstance(el_p, Segment)
            and isinstance(el_n, Segment)
        ):
            a1 = degrees(atan2p(el_p.b.x - el.c.x, el_p.b.y - el.c.y))
            a2 = degrees(atan2p(el_n.a.x - el.c.x, el_n.a.y - el.c.y))

            if abs(a2 - a1) > TOL:
                rv.append(
                    Edge.makeCircle(el.r, Vector(el.c.x, el.c.y), angle1=a1, angle2=a2)
                )

    el1 = hull[1]
    if isinstance(el, Segment) and isinstance(el_n, Arc) and isinstance(el1, Segment):
        a1 = degrees(atan2p(el.b.x - el_n.c.x, el.b.y - el_n.c.y))
        a2 = degrees(atan2p(el1.a.x - el_n.c.x, el1.a.y - el_n.c.y))

        rv.append(
            Edge.makeCircle(el_n.r, Vector(el_n.c.x, el_n.c.y), angle1=a1, angle2=a2)
        )

    return wire(*rv)


def find_hull(edges: Iterable[Edge]) -> Wire:

    # initialize the hull
    rv: Hull = []

    # split into arcs and points
    arcs, points = convert_and_validate(edges)

    # select the starting element
    start = select_lowest(arcs, points)
    rv.append(start)

    # initialize
    entities: List[Entity] = []
    entities.extend(arcs)
    entities.extend(points)

    current_e = start
    current_angle = 0.0

    # march around
    while True:

        angles = []
        segments = []

        for e in entities:
            angle, segment = get_angle(current_e, e)
            angles.append(angle if angle >= current_angle else inf)
            segments.append(segment)

        # nothing left to reach: closed if back at the start, stuck otherwise
        if min(angles, default=inf) == inf:
            if current_e is not start:
                raise ValueError("Hull could not be closed")
            if len(rv) == 1:
                # nothing reaches the largest circle: everything else is inside it
                if start is max(arcs, key=lambda a: a.r, default=None):
                    return Wire.assembleEdges(
                        [Edge.makeCircle(start.r, Vector(start.c.x, start.c.y))]
                    )
                raise ValueError("Hull could not be closed")
            break

        next_ix = int(argmin(angles))
        current_e, current_angle = update_hull(
            current_e, next_ix, entities, angles, segments, rv
        )

    # convert back to Edges and return
    return finalize_hull(rv)
