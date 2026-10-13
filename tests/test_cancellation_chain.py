"""SIGINT marks the token; the durable cancel chain runs on the owner thread.

`install_sigint_cancel` used to call `token.cancel`, which runs every registered
callback inside the interrupted frame. Those callbacks write to the
coordination store, so the handler could block on a lock the interrupted frame
was holding and then raise out of the handler at an arbitrary bytecode.
"""

import signal

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.signal_cancel import install_sigint_cancel, restore_sigint, sigint_cancel_scope


class _Recorder:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


class TestTokenSplit:
    def test_request_sets_the_flag_without_running_callbacks(self):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)

        token.request("user")

        assert token.is_cancelled
        assert token.reason == "user"
        assert token.cause is not None
        assert callback.calls == 0

    def test_run_pending_callbacks_runs_them_once(self):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)
        token.request("user")

        token.run_pending_callbacks()
        token.run_pending_callbacks()

        assert callback.calls == 1

    def test_run_pending_callbacks_is_a_noop_before_any_cancel(self):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)
        token.run_pending_callbacks()
        assert callback.calls == 0

    def test_a_callback_added_after_request_is_not_double_invoked(self):
        token = CancellationToken()
        token.request("user")
        late = _Recorder()
        token.add_callback(late)

        token.run_pending_callbacks()

        assert late.calls == 1

    def test_cancel_still_runs_the_chain_and_drains_it(self):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)

        token.cancel("user")
        assert callback.calls == 1

        # Already drained: the scope's exit must not run it a second time.
        token.run_pending_callbacks()
        assert callback.calls == 1

    def test_add_callback_after_cancel_still_invokes_immediately(self):
        token = CancellationToken()
        token.cancel("user")
        late = _Recorder()
        token.add_callback(late)
        assert late.calls == 1

    def test_check_raises_once_the_flag_is_set(self):
        from agent_runtime.cancellation import CancelledError

        token = CancellationToken()
        token.request("user")
        with pytest.raises(CancelledError):
            token.check()


class TestSignalHandler:
    def test_the_handler_only_marks_the_token(self, monkeypatch):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)
        previous = install_sigint_cancel(token, first_message="")
        try:
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)

            assert token.is_cancelled
            assert callback.calls == 0
        finally:
            restore_sigint(previous)

    def test_a_second_sigint_still_raises_keyboard_interrupt(self):
        token = CancellationToken()
        previous = install_sigint_cancel(token, first_message="")
        try:
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
        finally:
            restore_sigint(previous)


class TestScopeRunsTheChain:
    def test_the_chain_runs_on_scope_exit(self):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)

        with sigint_cancel_scope(token, first_message="") as scoped:
            scoped.request("user")
            assert callback.calls == 0

        assert callback.calls == 1

    def test_the_handler_is_restored(self):
        token = CancellationToken()
        before = signal.getsignal(signal.SIGINT)
        with sigint_cancel_scope(token, first_message=""):
            assert signal.getsignal(signal.SIGINT) is not before
        assert signal.getsignal(signal.SIGINT) is before

    def test_nothing_runs_when_the_body_never_cancels(self):
        token = CancellationToken()
        callback = _Recorder()
        token.add_callback(callback)
        with sigint_cancel_scope(token, first_message=""):
            pass
        assert callback.calls == 0

    def test_on_first_cancel_runs_once_on_exit(self):
        token = CancellationToken()
        seen = _Recorder()
        with sigint_cancel_scope(token, first_message="", on_first_cancel=seen) as scoped:
            scoped.request("user")
            assert seen.calls == 0
        assert seen.calls == 1

    def test_a_failing_callback_does_not_mask_the_body_error(self):
        token = CancellationToken()

        def boom():
            raise RuntimeError("callback failed")

        token.add_callback(boom)
        with pytest.raises(ValueError, match="body"):  # noqa: PT012 - body raises on exit
            with sigint_cancel_scope(token, first_message=""):
                token.request("user")
                raise ValueError("body")
