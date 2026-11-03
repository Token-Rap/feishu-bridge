"""Real managed frontend and loopback streams; private synthetic work only."""
import asyncio
import json

import pytest

from feishu_bridge.managed import fleet_managed_ingress as ingress
from feishu_bridge.managed import fleet_managed_output as output
from feishu_bridge.sessions import fleet_target_control as control
from test_managed_entry_source import bound, deliver


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), 3)


def original(b):
    return next(o for o in output._TAILS.values() if o.front.frontend is b.f)


def real_frontend(b):
    del b.f.broadcast  # Use the actual AppFrontend implementation.
    b.f._loop = asyncio.get_running_loop()
    b.f._gateway_writers.clear()


@pytest.mark.parametrize('fault', ['none', 'callback_repeat', 'disconnect', 'greet', 'writer_drift'])
def test_actual_callback_keeps_original_recipient_and_pending_lease(bound, fault):
    b = bound

    async def run():
        real_frontend(b)
        b.reserve()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def turn(*args):
            entered.set()
            await release.wait()
        b.e._turn = turn
        server = await asyncio.start_server(b.f._on_conn, '127.0.0.1', 0)
        reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
        connection = None
        try:
            writer.write((json.dumps({'type': 'hello'}) + '\n' +
                          json.dumps(b.frame, ensure_ascii=False) + '\n').encode())
            await writer.drain()
            ack = json.loads(await asyncio.wait_for(reader.readline(), 3))
            event = json.loads(await asyncio.wait_for(reader.readline(), 3))
            await asyncio.wait_for(entered.wait(), 3)
            o = original(b)
            connection = o.connections[0]
            await until(lambda: output.facts(o.tail)['drains_returned'] == 1)
            facts = output.facts(o.tail)
            assert ack == {'type': 'inject_ack', 'chat_id': 'group:fixture'}
            assert event['text'] == b.frame['text']
            assert event['work_id'] == b.frame['work_id']
            assert event['client'] == b.frame['client']
            assert facts['disposition'] == 'WRITES_AND_DRAINS_OBSERVED_PENDING'
            assert facts['recipients'] == facts['writes_returned'] == facts['drains_returned'] == 1
            assert not any(facts[k] for k in ('gateway_durable_acceptance', 'client_receipt_verified',
                'output_settlement', 'helper_released', 'parent_ready', 'native_ready', 'retry_allowed'))
            assert b.c.snapshot()['active_leases'] == 2
            assert b.c.snapshot()['unresolved'] == 1
            with pytest.raises(control.Refused, match='admission_closed'):
                await b.e.handle_frame(b.frame, connection.writer)

            if fault == 'callback_repeat':
                b.f._write_all(o.line, o.tail)
            elif fault == 'disconnect':
                writer.close()
                await writer.wait_closed()
                await until(lambda: output.facts(o.tail)['disposition'] == 'UNKNOWN_NO_RETRY')
            elif fault == 'greet':
                with pytest.raises(ingress.Refused, match='managed_output_replay_unbound'):
                    b.f._greet(connection.writer)
            elif fault == 'writer_drift':
                # A changed writer cannot authorize a second callback or close.
                connection.writer.write = lambda _: pytest.fail('replacement writer used')
                b.f._write_all(o.line, o.tail)
            if fault != 'none':
                assert output.facts(o.tail)['disposition'] == 'UNKNOWN_NO_RETRY'
                assert b.c.snapshot()['state'] == 'UNKNOWN'
            assert output.facts(o.tail)['writes_returned'] == 1
            assert not b.f._pending
        finally:
            release.set()
            await asyncio.gather(*b.jobs, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            server.close()
            if connection is not None:
                await until(lambda: output._CONNECTION_STATES[id(connection)]['close'] != 'OPEN')
                task = output._CONNECTION_STATES[id(connection)]['task']
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                # Fixture owns these synthetic sockets, including a deliberately
                # unclosed connection whose production close guard refused drift.
                connection.transport.close()
            await server.wait_closed()
        assert b.c.snapshot()['active_leases'] == 2
        assert b.c.snapshot()['unresolved'] == 1

    asyncio.run(run())


def test_no_original_recipient_retains_helper_and_never_queues_replay(bound):
    b = bound
    b.reserve()

    async def run():
        del b.f.broadcast
        b.f._loop = asyncio.get_running_loop()
        result = await deliver(b)
        assert isinstance(result[0], ingress.Refused)
        assert str(result[0]) == 'managed_output_unknown'
        facts = output.facts(original(b).tail)
        assert facts['disposition'] == 'UNKNOWN_NO_RETRY'
        assert facts['recipients'] == facts['writes_returned'] == facts['drains_returned'] == 0
        assert not facts['original_callback_attempted'] and not facts['retry_allowed']
        assert not b.f._pending and not b.calls
        assert b.c.snapshot()['state'] == 'UNKNOWN'
        assert b.c.snapshot()['active_leases'] == 2
        with pytest.raises(control.Refused, match='admission_closed'):
            await deliver(b)
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['before_callback_drift', 'possible_write', 'drain_timeout'])
def test_original_output_failure_keeps_effect_prefix_without_retry(bound, monkeypatch, fault):
    b = bound
    real_write = asyncio.StreamWriter.write
    real_drain = asyncio.StreamWriter.drain

    def write(writer, line):
        real_write(writer, line)
        if fault == 'possible_write' and writer in b.f._writers and json.loads(line).get('ev') == 'user':
            raise OSError('SYNTHETIC uncertain write')

    async def drain(writer):
        if fault == 'drain_timeout' and writer in b.f._writers:
            await asyncio.sleep(1)
        await real_drain(writer)

    monkeypatch.setattr(asyncio.StreamWriter, 'write', write)
    monkeypatch.setattr(asyncio.StreamWriter, 'drain', drain)
    monkeypatch.setattr(output, 'DRAIN_TIMEOUT', .01)

    async def run():
        real_frontend(b)
        b.reserve()
        release = asyncio.Event()

        async def turn(*args):
            if fault == 'before_callback_drift':
                original(b).connections[0].writer.write = lambda _: pytest.fail('drifted writer used')
            await release.wait()
        b.e._turn = turn
        server = await asyncio.start_server(b.f._on_conn, '127.0.0.1', 0)
        reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
        try:
            writer.write((json.dumps({'type': 'hello'}) + '\n' + json.dumps(b.frame) + '\n').encode())
            await writer.drain()
            assert json.loads(await asyncio.wait_for(reader.readline(), 3))['type'] == 'inject_ack'
            await until(lambda: any(o.front.frontend is b.f for o in output._TAILS.values()))
            o = original(b)
            await until(lambda: output.facts(o.tail)['disposition'] == 'UNKNOWN_NO_RETRY')
            facts = output.facts(o.tail)
            assert facts['writes_returned'] == (1 if fault == 'drain_timeout' else 0)
            assert facts['drains_returned'] == 0 and not facts['retry_allowed']
            b.f._write_all(o.line, o.tail)
            assert output.facts(o.tail) == facts
            assert b.c.snapshot()['state'] == 'UNKNOWN'
            assert b.c.snapshot()['active_leases'] == 2 and not b.f._pending
        finally:
            release.set()
            await asyncio.gather(*b.jobs, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            original(b).connections[0].transport.close()
            server.close()
            await server.wait_closed()
    asyncio.run(run())
