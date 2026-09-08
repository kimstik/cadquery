from math import pi

import pytest

import cadquery as cq
from cadquery import hull


def test_hull():

    c1 = cq.Edge.makeCircle(0.5, (-1.5, 0.5, 0))
    c2 = cq.Edge.makeCircle(0.5, (1.9, 0.0, 0))
    c3 = cq.Edge.makeCircle(0.2, (0.3, 1.5, 0))
    c4 = cq.Edge.makeCircle(0.2, (1.0, 1.5, 0))
    c5 = cq.Edge.makeCircle(0.1, (0.0, 0.0, 0.0))
    e1 = cq.Edge.makeLine(cq.Vector(0, -0.5), cq.Vector(-0.5, 1.5))
    e2 = cq.Edge.makeLine(cq.Vector(2.1, 1.5), cq.Vector(2.6, 1.5))

    edges = [c1, c2, c3, c4, c5, e1, e2]

    h = hull.find_hull(edges)

    assert len(h.Vertices()) == 11
    assert h.IsClosed()
    assert h.isValid()


def test_validation():

    with pytest.raises(ValueError):

        e1 = cq.Edge.makeEllipse(2, 1)
        c1 = cq.Edge.makeCircle(0.5, (-1.5, 0.5, 0))
        hull.find_hull([c1, e1])


def test_collinear():

    r = 2.5
    spacing = 8.0

    # collinear centres: the tangent line touches every circle in between
    for n in (3, 4, 5):

        expected = spacing * (n - 1) * 2 * r + pi * r ** 2

        # arc order follows id(), so repeat to hit the failing orders
        for _ in range(20):

            edges = [cq.Edge.makeCircle(r, (i * spacing, 0, 0)) for i in range(n)]

            h = hull.find_hull(edges)

            assert h.IsClosed()
            assert h.isValid()
            assert cq.Face.makeFromWires(h).Area() == pytest.approx(expected)
