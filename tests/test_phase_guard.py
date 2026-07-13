"""PhaseReadWriteLock 阶段级读写锁单测（V1.4-Bonus15a）。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.repair.phase_guard import PhaseReadWriteLock


class TestPhaseReadWriteLock:
    def test_read_acquire_and_release(self):
        lock = PhaseReadWriteLock()
        with lock.read():
            assert lock.active_readers == 1
        assert lock.active_readers == 0

    def test_multiple_readers_concurrent(self):
        lock = PhaseReadWriteLock()
        results = []

        def reader():
            with lock.read():
                results.append(lock.active_readers)

        with ThreadPoolExecutor(max_workers=3) as pool:
            for _ in range(3):
                pool.submit(reader)
        # 多个 reader 可以并发
        assert len(results) == 3

    def test_write_acquire_and_release(self):
        lock = PhaseReadWriteLock()
        with lock.write():
            assert lock.active_readers == 0
        assert lock.active_readers == 0

    def test_write_waits_for_readers(self):
        lock = PhaseReadWriteLock()
        events: list[str] = []
        event = threading.Event()

        def reader():
            with lock.read():
                events.append("read_start")
                event.set()  # signal write to try
                time.sleep(0.1)
                events.append("read_end")

        def writer():
            event.wait()  # wait for reader to start
            with lock.write():
                events.append("write")

        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(reader)
            pool.submit(writer)

        # write 必须在所有 reader 结束后执行
        assert events[0] == "read_start"
        assert events[1] == "read_end"
        assert events[2] == "write"

    def test_write_blocks_new_readers(self):
        lock = PhaseReadWriteLock()
        barrier = threading.Barrier(2, timeout=3)

        def writer():
            with lock.write():
                barrier.wait()
                time.sleep(0.1)

        def late_reader():
            barrier.wait()
            time.sleep(0.05)  # 让 writer 先 acquire
            t0 = time.time()
            with lock.read():
                pass
            elapsed = time.time() - t0
            # reader 应等待 writer 释放（至少 >0.05）
            assert elapsed >= 0.05

        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(writer)
            pool.submit(late_reader)

    def test_sequential_write_after_write(self):
        lock = PhaseReadWriteLock()
        with lock.write():
            pass
        with lock.write():
            pass  # 第二次写不应死锁

    def test_read_after_write(self):
        lock = PhaseReadWriteLock()
        with lock.write():
            pass
        with lock.read():
            assert lock.active_readers == 1
