"""
Daemon Thread Pool -- prevents ThreadPoolExecutor threads from blocking
process exit during shutdown.

Standard ThreadPoolExecutor uses non-daemon threads, which keep the
process alive even after the main thread exits.  This subclass overrides
the internal thread-creation path so every worker thread is daemon,
meaning Python will terminate them automatically when the main thread
exits.

Drop-in replacement: swap ``ThreadPoolExecutor`` for
``DaemonThreadPoolExecutor`` -- same constructor signature, same API.
"""

import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker, _threads_queues


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are all daemon threads.

    On process shutdown, daemon threads are killed automatically instead
    of blocking exit while waiting on DB queries / HTTP / I/O.
    """

    def _adjust_thread_count(self):
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = '%s_%d' % (
                self._thread_name_prefix or self, num_threads
            )
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
            )
            t.daemon = True
            t.start()
            self._threads.add(t)
            _threads_queues[t] = self._work_queue
