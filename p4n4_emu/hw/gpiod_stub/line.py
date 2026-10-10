"""gpiod.line: the line setting enums (libgpiod v2)."""

from enum import Enum

__all__ = ["Value", "Direction", "Bias", "Drive", "Edge", "Clock"]


class Value(Enum):
    INACTIVE = 0
    ACTIVE = 1

    def __bool__(self) -> bool:
        return self == self.ACTIVE


class Direction(Enum):
    AS_IS = 1
    INPUT = 2
    OUTPUT = 3


class Bias(Enum):
    AS_IS = 1
    UNKNOWN = 2
    DISABLED = 3
    PULL_UP = 4
    PULL_DOWN = 5


class Drive(Enum):
    PUSH_PULL = 1
    OPEN_DRAIN = 2
    OPEN_SOURCE = 3


class Edge(Enum):
    NONE = 1
    RISING = 2
    FALLING = 3
    BOTH = 4


class Clock(Enum):
    MONOTONIC = 1
    REALTIME = 2
    HTE = 3
