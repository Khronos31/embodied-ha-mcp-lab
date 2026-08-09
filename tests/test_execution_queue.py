import threading
import time

from embodied_ha_mcp_lab.execution_queue import ExecutionQueue


def test_execution_is_fifo_and_never_overlaps():
    queue = ExecutionQueue()
    order = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def worker(identifier):
        nonlocal active, max_active
        with queue.acquire(identifier):
            with lock:
                active += 1
                max_active = max(max_active, active)
                order.append(identifier)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = []
    for identifier in ("first", "second", "third"):
        thread = threading.Thread(target=worker, args=(identifier,))
        thread.start()
        threads.append(thread)
        time.sleep(0.01)
    for thread in threads:
        thread.join()

    assert order == ["first", "second", "third"]
    assert max_active == 1
    assert queue.snapshot() == {"queue_depth": 0, "active_operation_id": None}
