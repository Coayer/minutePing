"""ASCII line chart, derived from asciichartpy by Igor Kroitor (MIT).
https://github.com/kroitor/asciichart

Rewritten for MicroPython: consumes a utils.Ring of integer milliseconds
directly, keeps the grid as bytearrays of symbol indices rather than lists of
one-character strings, and yields one finished row at a time so the caller can
stream it to a socket without ever holding the whole chart in RAM.
"""

from utils import FAILED

# Index 0 is blank; the grid stores indices into this tuple.
SYMBOLS = (" ", "┼", "┤", "╶", "╴", "─",
           "╰", "╭", "╮", "╯", "│", "╳")

                 # index 0 is blank, which is what a fresh bytearray holds
AXIS_ZERO = 1    # cross, on the baseline
AXIS = 2         # tick
HALF_RIGHT = 3   # value resumes after a gap
HALF_LEFT = 4    # value stops before a gap
HORIZONTAL = 5
# Corners are named for the directions they connect, because rows are indexed
# from the top: a rising line moves to a *smaller* row index.
UP_RIGHT = 6     # ╰
DOWN_RIGHT = 7   # ╭
LEFT_DOWN = 8    # ╮
LEFT_UP = 9      # ╯
VERTICAL = 10
FAIL_MARK = 11

def nice_max(value):
    """Rounds a maximum up to a readable axis bound (1, 2, 2.5 or 5 x 10^n)."""
    if value <= 0:
        return 10

    step = 1
    while step * 10 <= value:
        step *= 10

    for multiplier in (1, 2, 2.5, 5, 10):
        candidate = int(step * multiplier)
        if candidate >= value:
            return candidate

    return step * 10


def plot(ring, height=8):
    """Yields the chart one row at a time, top row first.

    Failed samples are drawn as a marker on the baseline instead of leaving a
    gap, so an outage is visible rather than absent.
    """
    samples = list(ring)
    if not samples:
        yield "    0" + SYMBOLS[AXIS_ZERO]
        return

    successful = [latency for latency in samples if latency != FAILED]
    maximum = nice_max(max(successful)) if successful else 10

    width = len(samples)
    rows = [bytearray(width) for _ in range(height)]
    span = height - 1

    def row_of(latency):
        """Grid row for a latency, 0 at the top and span on the baseline."""
        scaled = (latency * span + maximum // 2) // maximum
        if scaled > span:
            scaled = span
        elif scaled < 0:
            scaled = 0
        return span - scaled

    # Mark failures first so a real reading always paints over them.
    for x in range(width):
        if samples[x] == FAILED:
            rows[span][x] = FAIL_MARK

    for x in range(width - 1):
        current = samples[x]
        following = samples[x + 1]

        if current == FAILED and following == FAILED:
            continue

        if current == FAILED:
            rows[row_of(following)][x] = HALF_RIGHT
            continue

        if following == FAILED:
            rows[row_of(current)][x] = HALF_LEFT
            continue

        y0 = row_of(current)
        y1 = row_of(following)

        if y0 == y1:
            rows[y0][x] = HORIZONTAL
            continue

        if y0 > y1:  # rising, since row 0 is the top
            rows[y0][x] = LEFT_UP      # ──╯ turning upward
            rows[y1][x] = DOWN_RIGHT   # ╭── continuing right
        else:
            rows[y0][x] = LEFT_DOWN    # ──╮ turning downward
            rows[y1][x] = UP_RIGHT     # ╰── continuing right

        for y in range(min(y0, y1) + 1, max(y0, y1)):
            rows[y][x] = VERTICAL

    # A single sample still deserves a tick on the axis.
    if width == 1 and samples[0] != FAILED:
        rows[row_of(samples[0])][0] = HORIZONTAL

    for y in range(height):
        value = maximum * (span - y) // span
        label = "{:5d}".format(value)
        axis = SYMBOLS[AXIS_ZERO] if y == span else SYMBOLS[AXIS]
        line = label + axis + "".join([SYMBOLS[index] for index in rows[y]])
        yield line.rstrip()
