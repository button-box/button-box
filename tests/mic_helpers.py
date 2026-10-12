"""Model a physical press for tests of the capture loop below the GPIO boundary."""

from types import SimpleNamespace


def physical_capture(runtime, *args, capture_function=None, **kwargs):
    with runtime.capture_session(SimpleNamespace(is_pressed=True)) as permit:
        kwargs.pop("press_token", None)
        function = capture_function or runtime.capture_guided_recording
        return function(*args, press_token=permit, **kwargs)
