"""Pure geometry helpers: no I/O, no Django, so they stay cheap and testable."""

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_MILES = 3958.7613
# Rough conversions used only for cheap bounding-box prefilters.
MILES_PER_DEGREE_LAT = 69.0


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two WGS84 points."""
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = phi2 - phi1
    dlambda = radians(lon2 - lon1)
    h = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * asin(sqrt(h))


def miles_per_degree_lon(latitude):
    """Longitude degrees shrink towards the poles; guard the degenerate case."""
    return max(cos(radians(latitude)) * 69.172, 0.1)


def cumulative_distances(points):
    """Running along-route mileage for each shape point (first entry is 0)."""
    out = [0.0]
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        out.append(out[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return out


def densify_indices(points, cumulative, spacing_miles):
    """Indices of a subsample of the polyline, ~`spacing_miles` apart.

    OSRM returns tens of thousands of shape points for a cross-country route.
    Measuring every station against every point is needless work, so we thin
    the line to a coarse ladder first; the endpoints are always kept.
    """
    if not points:
        return []
    keep = [0]
    next_mark = spacing_miles
    for i, dist in enumerate(cumulative):
        if dist >= next_mark:
            keep.append(i)
            next_mark = dist + spacing_miles
    if keep[-1] != len(points) - 1:
        keep.append(len(points) - 1)
    return keep
