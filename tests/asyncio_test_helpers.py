import logging
import types
from asyncio import AbstractEventLoop, transports
from asyncio.protocols import BaseProtocol, Protocol
from asyncio.transports import Transport
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)


class TimelessEventLoopWrapper:
    @property  # type: ignore
    # -- we have no need to make this read/write
    # (o.O being able to re-write a __class__ is odd!)
    def __class__(self):
        """
        Fakes isinstance(this, AbstractEventLoop) so we can set_event_loop
         without fail.
        """
        return self._wrapped_loop.__class__

    def __init__(self, wrapped_loop: AbstractEventLoop):
        self._wrapped_loop = wrapped_loop
        self._time = 0.0
        self._to_be_called: List[Tuple[float, Any, Any, Any]] = []

    def advance(self, time_delta: float):
        target_time = self._time + time_delta
        logger.debug(
            "advancing from %f by %f (%d in queue)",
            self._time,
            time_delta,
            len(self._to_be_called),
        )
        while self._time < target_time and self._to_be_called:
            # pop off the next callback from the queue
            next_time, next_callback, args, _context = self._to_be_called[0]
            if next_time > target_time:
                # this isn't allowed to run yet
                break
            logger.debug("callback at %f on %r", next_time, next_callback)
            self._to_be_called = self._to_be_called[1:]
            self._time = next_time
            next_callback(*args)

        # no more tasks can run now but advance to the time anyway
        self._time = target_time

    def __getattr__(self, item):
        """
        We use this to delegate other method calls to the real EventLoop.
        """
        value = getattr(self._wrapped_loop, item)
        if isinstance(value, types.MethodType):
            # rebind this method to be called on us
            # this makes the wrapped class use our overridden methods when
            # available.
            # Otherwise, call_soon etc won't come to us in all scenarios.
            return types.MethodType(value.__func__, self)
        else:
            return value

    def call_later(self, delay, callback, *args, context=None):
        self.call_at(self._time + delay, callback, *args)

    def call_at(self, when, callback, *args, context=None):
        logger.debug(f"Calling {callback} at %f...", when)
        self._to_be_called.append((when, callback, args, context))

        # re-sort list in ascending time order
        self._to_be_called.sort(key=lambda x: x[0])

    def call_soon(self, callback, *args, context=None):
        return self.call_later(0, callback, *args, context=context)

    def time(self):
        return self._time


class MockTransport(Transport):
    """
    A transport intended to be driven by tests.
    Stores received data into a buffer.
    """

    def __init__(self):
        self.buffer = b""
        self.eofed = False
        self.aborted = False
        self.protocol = None
        self.closed = False

    def is_reading(self):
        return True

    def pause_reading(self):
        pass  # NOP

    def resume_reading(self):
        pass  # NOP

    def set_write_buffer_limits(self, high=None, low=None):
        pass  # NOP

    def get_write_buffer_size(self):
        """Return the current size of the write buffer."""
        raise NotImplementedError

    def write(self, data):
        self.buffer += data

    def write_eof(self):
        self.eofed = True

    def can_write_eof(self):
        return True

    def abort(self):
        self.aborted = True

    def pretend_to_receive(self, data: bytes):
        proto = self.get_protocol()
        assert isinstance(proto, Protocol)
        proto.data_received(data)

    def set_protocol(self, protocol: BaseProtocol) -> None:
        self.protocol = protocol

    def get_protocol(self) -> BaseProtocol:
        assert isinstance(self.protocol, BaseProtocol)
        return self.protocol

    def close(self):
        self.closed = True
        return


class MockProtocol(Protocol):
    """
    A protocol intended to be driven by tests.
    Stores received data into a buffer.
    """

    def __init__(self):
        self._to_transmit = b""
        self.received_bytes = b""
        self.transport = None

    def data_received(self, data: bytes) -> None:
        self.received_bytes += data

    def connection_made(self, transport: transports.BaseTransport) -> None:
        assert isinstance(transport, Transport)
        self.transport = transport
        if self._to_transmit:
            transport.write(self._to_transmit)

    def write(self, data: bytes):
        if self.transport:
            self.transport.write(data)
        else:
            self._to_transmit += data