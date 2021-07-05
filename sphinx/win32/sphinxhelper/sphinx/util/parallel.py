"""
    sphinx.util.parallel
    ~~~~~~~~~~~~~~~~~~~~

    Parallel building utilities.

    :copyright: Copyright 2007-2021 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import os
import platform
import sys
import time
import traceback
from math import sqrt
from typing import Any, Callable, Dict, List, Sequence

try:
    import multiprocessing
except ImportError:
    multiprocessing = None

from sphinx.errors import SphinxParallelError
from sphinx.util import logging

logger = logging.getLogger(__name__)


# our parallel functionality only works for the forking Process
#
# Note: "fork" is not recommended on macOS and py38+.
#       see https://bugs.python.org/issue33725
parallel_available = (multiprocessing and
                      (os.name == 'posix') and
                      not (sys.version_info > (3, 8) and platform.system() == 'Darwin'))


class SerialTasks:
    """Has the same interface as ParallelTasks, but executes tasks directly."""

    def __init__(self, nproc: int = 1) -> None:
        pass

    def add_task(self, task_func: Callable, arg: Any = None, result_func: Callable = None) -> None:  # NOQA
        if arg is not None:
            res = task_func(arg)
        else:
            res = task_func()
        if result_func:
            result_func(res)

    def join(self) -> None:
        pass


class ParallelTasks:
    """Executes *nproc* tasks in parallel after forking."""

    def __init__(self, nproc: int) -> None:
        self.nproc = nproc
        # (optional) function performed by each task on the result of main task
        self._result_funcs = {}  # type: Dict[int, Callable]
        # task arguments
        self._args = {}  # type: Dict[int, List[Any]]
        # list of subprocesses (both started and waiting)
        self._procs = {}  # type: Dict[int, multiprocessing.Process]
        # list of receiving pipe connections of running subprocesses
        self._precvs = {}  # type: Dict[int, Any]
        # list of receiving pipe connections of waiting subprocesses
        self._precvsWaiting = {}  # type: Dict[int, Any]
        # number of working subprocesses
        self._pworking = 0
        # task number of each subprocess
        self._taskid = 0

    def _process(self, pipe: Any, func: Callable, arg: Any) -> None:
        try:
            collector = logging.LogCollector()
            with collector.collect():
                if arg is None:
                    ret = func()
                else:
                    ret = func(arg)
            failed = False
        except BaseException as err:
            failed = True
            errmsg = traceback.format_exception_only(err.__class__, err)[0].strip()
            ret = (errmsg, traceback.format_exc())
        logging.convert_serializable(collector.logs)
        pipe.send((failed, collector.logs, ret))

    def add_task(self, task_func: Callable, arg: Any = None, result_func: Callable = None) -> None:  # NOQA
        tid = self._taskid
        self._taskid += 1
        self._result_funcs[tid] = result_func or (lambda arg, result: None)
        self._args[tid] = arg
        precv, psend = multiprocessing.Pipe(False)
        proc = multiprocessing.Process(target=self._process,
                                       args=(psend, task_func, arg))
        self._procs[tid] = proc
        self._precvsWaiting[tid] = precv
        self._join_one()

    def join(self) -> None:
        while self._pworking:
            self._join_one()

    def _join_one(self) -> None:
        for tid, pipe in self._precvs.items():
            if pipe.poll():
                exc, logs, result = pipe.recv()
                if exc:
                    raise SphinxParallelError(*result)
                for log in logs:
                    logger.handle(log)
                self._result_funcs.pop(tid)(self._args.pop(tid), result)
                self._procs[tid].join()
                self._precvs.pop(tid)
                self._pworking -= 1
                break
        else:
            time.sleep(0.02)
        while self._precvsWaiting and self._pworking < self.nproc:
            newtid, newprecv = self._precvsWaiting.popitem()
            self._precvs[newtid] = newprecv
            self._procs[newtid].start()
            self._pworking += 1


def make_chunks(arguments: Sequence[str], nproc: int, maxbatch: int = 10) -> List[Any]:
    # determine how many documents to read in one go
    nargs = len(arguments)
    chunksize = nargs // nproc
    if chunksize >= maxbatch:
        # try to improve batch size vs. number of batches
        chunksize = int(sqrt(nargs / nproc * maxbatch))
    if chunksize == 0:
        chunksize = 1
    nchunks, rest = divmod(nargs, chunksize)
    if rest:
        nchunks += 1
    # partition documents in "chunks" that will be written by one Process
    return [arguments[i * chunksize:(i + 1) * chunksize] for i in range(nchunks)]