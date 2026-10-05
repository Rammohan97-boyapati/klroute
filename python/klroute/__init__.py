"""klroute -- route waveguides or metal between two points in KLayout."""

from .router import (  # noqa: F401
    DIRECTIONS,
    Route,
    RouteError,
    RouteSpec,
    auto_directions,
    draw_route,
    insert_route,
    route,
    route_through,
)

__version__ = "0.15.0"
