"""When a set's inputs may run — the schedule modes the global filewatcher had."""
import logging
from datetime import datetime

logger = logging.getLogger("mediaassistant.inputs")

# set_id -> whether "outside schedule" was already logged, to avoid log spam
_logged: dict[int, bool] = {}


def _say_once(set_id: int, message: str) -> None:
    if not _logged.get(set_id):
        logger.info(message)
        _logged[set_id] = True


def is_within_schedule(processing_set, now: datetime | None = None) -> bool:
    """Whether a set's inputs run automatically right now.

    Modes:
    - continuous: always
    - manual: never (only via manual trigger)
    - window: between window_start and window_end, also across midnight
    - scheduled: on scheduled_days, starting at scheduled_time, for at least
      an hour (longer when the interval is longer — same rule as before)
    """
    ps = processing_set
    mode = ps.schedule_mode or "continuous"
    now = now or datetime.now()

    if mode == "continuous":
        _logged[ps.id] = False
        return True

    if mode == "manual":
        _say_once(ps.id, f"Set „{ps.name}\": manual mode — skipping automatic processing")
        return False

    if mode == "window":
        try:
            start_h, start_m = map(int, (ps.window_start or "22:00").split(":"))
            end_h, end_m = map(int, (ps.window_end or "06:00").split(":"))
        except (ValueError, AttributeError):
            return True  # invalid config → fallback to continuous
        current = now.hour * 60 + now.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end:
            in_window = start <= current < end           # same-day window, e.g. 08:00–18:00
        else:
            in_window = current >= start or current < end  # overnight window, e.g. 22:00–06:00
        if in_window:
            _logged[ps.id] = False
        else:
            _say_once(ps.id, f"Set „{ps.name}\": outside time window "
                             f"({ps.window_start}–{ps.window_end}), pausing")
        return in_window

    if mode == "scheduled":
        try:
            allowed_days = {int(d.strip()) for d in (ps.scheduled_days or "").split(",") if d.strip()}
        except (ValueError, AttributeError):
            allowed_days = set(range(7))
        try:
            sched_h, sched_m = map(int, (ps.scheduled_time or "23:00").split(":"))
        except (ValueError, AttributeError):
            return True
        if now.weekday() not in allowed_days:
            _say_once(ps.id, f"Set „{ps.name}\": today (weekday {now.weekday()}) not in schedule")
            return False
        window = max(int(ps.interval_s or 5), 60)  # at least 60 minutes
        start = sched_h * 60 + sched_m
        current = now.hour * 60 + now.minute
        in_window = start <= current < start + window
        if in_window:
            _logged[ps.id] = False
        else:
            _say_once(ps.id, f"Set „{ps.name}\": outside scheduled window "
                             f"({ps.scheduled_time}+{window}min)")
        return in_window

    return True  # unknown mode → fallback to continuous
