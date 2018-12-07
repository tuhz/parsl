"""This module implements the AppFutures.

We have two basic types of futures:
    1. DataFutures which represent data objects
    2. AppFutures which represent the futures on App/Leaf tasks.

"""

from concurrent.futures import Future
import logging
import threading

from parsl.app.errors import RemoteExceptionWrapper

logger = logging.getLogger(__name__)

# Possible future states (for internal use by the futures package).
PENDING = 'PENDING'
RUNNING = 'RUNNING'
# The future was cancelled by the user...
CANCELLED = 'CANCELLED'
# ...and _Waiter.add_cancelled() was called by a worker.
CANCELLED_AND_NOTIFIED = 'CANCELLED_AND_NOTIFIED'
FINISHED = 'FINISHED'

_STATE_TO_DESCRIPTION_MAP = {
    PENDING: "pending",
    RUNNING: "running",
    CANCELLED: "cancelled",
    CANCELLED_AND_NOTIFIED: "cancelled",
    FINISHED: "finished"
}


class AppFuture(Future):
    """An AppFuture wraps a sequence of Futures which may fail and be retried.

    An AppFuture starts with no parent future. A sequence of parent futures may
    be assigned by code outside of this class, by passing that new parent future
    into "update_future".

    The AppFuture will set its result to the result of the parent future, if that
    parent future completes without an exception. This result setting should
    cause .result(), .exception() and done callbacks to fire as expected when a
    Future has a result set.

    The AppFuture will not set its result to the result of the parent future, if
    that parent future completes with an exception, and if that parent future
    has retries left. In that case, no result(), exception() or done callbacks should
    report a result.

    The AppFuture will set its result to the result of the parent future, if that
    parent future completes with an exception and if that parent future has no
    retries left, or if it has no retry field. .result(), .exception() and done callbacks
    should give a result as expected when a Future has a result set

    The parent future may return a RemoteExceptionWrapper as a result
    and AppFuture will treat this an an exception for the above
    retry and result handling behaviour.

    """

    def __init__(self, task_struct, tid=None, stdout=None, stderr=None):
        """Initialize the AppFuture.

        Args:

        KWargs:
             - tid (Int) : Task id should be any unique identifier. Now Int.
             - stdout (str) : Stdout file of the app.
                   Default: None
             - stderr (str) : Stderr file of the app.
                   Default: None
        """
        self._tid = tid
        super().__init__()
        self.parent = None
        self._update_lock = threading.Lock()
        self._outputs = []
        self._stdout = stdout
        self._stderr = stderr
        self._task_struct = task_struct

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    @property
    def tid(self):
        return self._tid

    def update_parent(self, fut):
        """Add a callback to the parent to update the state.

        This handles the case where the user has called result on the AppFuture
        before the parent exists.
        """
        self.parent = fut

    def cancel(self):
        raise ValueError("Cancel not implemented")

    def cancelled(self):
        return False

    def running(self):
        if self.parent:
            return self.parent.running()
        else:
            return False

    def task_status(self):
        """Returns the status of the task that will provide the value
           for this future.  This may not be in-sync with the result state
           of this future - for example, task_status might return 'done' but
           self.done() might not be true (which in turn means self.result()
           and self.exception() might block).

           The actual status description strings returned by this method are
           likely to change over subsequent versions of parsl, as use-cases
           and infrastructure are worked out.

           It is expected that the status values will be from a limited set
           of strings (so that it makes sense, for example, to group and
           count statuses from many futures).

           It is expected that might be a non-trivial cost in acquiring the
           status in future (for example, by communicating with a remote
           worker).

           Returns: str
        """
        return self._task_struct['status'].name

    @property
    def outputs(self):
        return self._outputs

    def __repr__(self):
        if self.parent:
            with self.parent._condition:
                if self.parent._state == FINISHED:
                    if self.parent._exception:
                        return '<%s at %#x state=%s raised %s>' % (
                            self.__class__.__name__,
                            id(self),
                            _STATE_TO_DESCRIPTION_MAP[self.parent._state],
                            self.parent._exception.__class__.__name__)
                    else:
                        return '<%s at %#x state=%s returned %s>' % (
                            self.__class__.__name__,
                            id(self),
                            _STATE_TO_DESCRIPTION_MAP[self.parent._state],
                            self.parent._result.__class__.__name__)
                return '<%s at %#x state=%s>' % (
                    self.__class__.__name__,
                    id(self),
                    _STATE_TO_DESCRIPTION_MAP[self.parent._state])
        else:
            return '<%s at %#x state=%s>' % (
                self.__class__.__name__,
                id(self),
                _STATE_TO_DESCRIPTION_MAP[self._state])
