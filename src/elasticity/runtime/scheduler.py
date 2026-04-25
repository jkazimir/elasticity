"""Interval scheduler for timer-based agents."""

import asyncio
from typing import Callable, Optional, Dict, Any
from datetime import timedelta


def parse_interval(interval_str: str) -> timedelta:
    """Parse an interval string like '30s', '5m', '1h'.

    Args:
        interval_str: Interval string

    Returns:
        timedelta object

    Raises:
        ValueError: If interval string is invalid
    """
    interval_str = interval_str.strip().lower()

    if interval_str.endswith("s"):
        seconds = int(interval_str[:-1])
        return timedelta(seconds=seconds)
    elif interval_str.endswith("m"):
        minutes = int(interval_str[:-1])
        return timedelta(minutes=minutes)
    elif interval_str.endswith("h"):
        hours = int(interval_str[:-1])
        return timedelta(hours=hours)
    else:
        # Try parsing as seconds
        try:
            seconds = int(interval_str)
            return timedelta(seconds=seconds)
        except ValueError:
            raise ValueError(f"Invalid interval format: {interval_str}")


class IntervalScheduler:
    """Schedules interval-based agent execution."""

    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stop_events: Dict[str, asyncio.Event] = {}

    async def schedule(
        self,
        schedule_id: str,
        interval: str,
        callback: Callable[[], Any],
        until: Optional[str] = None,
    ) -> None:
        """Schedule a callback to run at intervals.

        Args:
            schedule_id: Unique ID for this schedule
            interval: Interval string (e.g., '30s', '5m')
            callback: Async callback to execute
            until: Optional condition string (not evaluated here, executor handles it)
        """
        if schedule_id in self._tasks:
            await self.cancel(schedule_id)

        stop_event = asyncio.Event()
        self._stop_events[schedule_id] = stop_event

        interval_delta = parse_interval(interval)

        _MAX_CONSECUTIVE_ERRORS = 5

        async def run_interval():
            import structlog
            logger = structlog.get_logger(__name__)
            consecutive_errors = 0
            while not stop_event.is_set():
                try:
                    await callback()
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(
                        "Interval callback failed",
                        schedule_id=schedule_id,
                        error=str(e),
                        consecutive_errors=consecutive_errors,
                    )
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Interval cancelled after too many consecutive errors",
                            schedule_id=schedule_id,
                            max_errors=_MAX_CONSECUTIVE_ERRORS,
                        )
                        stop_event.set()
                        break

                # Wait for interval or stop event
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_delta.total_seconds())
                    break  # Stop event was set
                except asyncio.TimeoutError:
                    pass  # Continue to next iteration

        task = asyncio.create_task(run_interval())
        self._tasks[schedule_id] = task

    async def cancel(self, schedule_id: str) -> None:
        """Cancel a scheduled interval.

        Args:
            schedule_id: Schedule ID to cancel
        """
        if schedule_id in self._stop_events:
            self._stop_events[schedule_id].set()
            del self._stop_events[schedule_id]

        if schedule_id in self._tasks:
            task = self._tasks[schedule_id]
            if not task.done():
                # Give the stop event a chance to exit the loop cooperatively
                # before resorting to hard cancellation.
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            del self._tasks[schedule_id]

    async def cancel_all(self) -> None:
        """Cancel all scheduled intervals."""
        schedule_ids = list(self._tasks.keys())
        for schedule_id in schedule_ids:
            await self.cancel(schedule_id)
