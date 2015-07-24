# Configure your tests here
import asyncio
import inspect
import io
import os

import aiohttp
import logging
import pytest
import webob
import webtest
from gunicorn import util

import muffin


def pytest_addoption(parser):
    """ Append pytest options for testing Muffin apps. """
    parser.addini('muffin_app', 'Set path to muffin application')
    parser.addoption('--muffin-app', dest='muffin_app', help='Set to muffin application')

    parser.addini('muffin_config', 'Set module path to muffin configuration')
    parser.addoption('--muffin-config', dest='muffin_config',
                     help='Set module path to muffin configuration')


def pytest_configure(config):
    config.addinivalue_line('markers', 'async: mark test to run asynchronuosly.')


@pytest.mark.tryfirst
def pytest_pycollect_makeitem(collector, name, obj):
    if collector.funcnamefilter(name) and inspect.isgeneratorfunction(obj):
        item = pytest.Function(name, parent=collector)
        if ('async' in item.keywords):
            return list(collector._genfunctions(name, obj))


def pytest_runtest_setup(item):
    if 'async' in item.keywords and 'loop' not in item.fixturenames:
        item.fixturenames.append('loop')


@pytest.mark.tryfirst
def pytest_pyfunc_call(pyfuncitem):
    if 'async' in pyfuncitem.keywords:
        loop = pyfuncitem.funcargs['loop']
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg]
                    for arg in pyfuncitem._fixtureinfo.argnames}
        coro = muffin.to_coroutine(pyfuncitem.obj)
        loop.run_until_complete(asyncio.async(coro(**testargs), loop=loop))
        return True


def pytest_load_initial_conftests(early_config, parser, args):
    """ Prepare to loading Muffin application. """
    options = parser.parse_known_args(args)

    # Initialize configuration
    config = options.muffin_config or early_config.getini('muffin_config')
    if config:
        os.environ[muffin.CONFIGURATION_ENVIRON_VARIABLE] = config

    # Initialize application
    app = options.muffin_app or early_config.getini('muffin_app')
    early_config.app = app


def WSGIHandler(app, loop):

    def handle(environ, start_response):

        req = webob.Request(environ)
        vers = aiohttp.HttpVersion10 if req.http_version == 'HTTP/1.0' else aiohttp.HttpVersion11
        message = aiohttp.RawRequestMessage(
            req.method, req.path_qs, vers, aiohttp.CIMultiDict(req.headers), False, False)
        payload = aiohttp.StreamReader(loop=loop)
        payload.feed_data(req.body)
        payload.feed_eof()
        factory = aiohttp.web.RequestHandlerFactory(
            app, app.router, loop=loop, keep_alive_on=False)
        handler = factory()
        handler.transport = io.BytesIO()
        handler.transport._conn_lost = 0
        handler.transport.get_extra_info = lambda s: ('127.0.0.1', 80)
        handler.writer = aiohttp.parsers.StreamWriter(
            handler.transport, handler, handler.reader, handler._loop)
        coro = handler.handle_request(message, payload)
        if loop.is_running():
            raise RuntimeError('Client cannot running in coroutines')

        loop.run_until_complete(coro)
        handler.transport.seek(0)
        res = webob.Response.from_file(handler.transport)
        start_response(res.status[9:], res.headerlist)
        return res.app_iter

    return handle


@pytest.fixture(scope='session')
def loop(request):
    """ Create and provide asyncio loop. """
    loop = asyncio.get_event_loop()
    asyncio.set_event_loop(loop)
    return loop


@pytest.fixture(scope='session')
def app(pytestconfig, request):
    """ Provide an example application. """
    if pytestconfig.app:
        return util.import_app(pytestconfig.app)

    logging.warn(
        'Improperly configured. Please set ``muffin_app`` in your pytest config. '
        'Or use ``--muffin-app`` command option.')
    return None


@pytest.fixture(scope='session', autouse=True)
def _initialize(app, loop, request):
    app._loop = loop
    loop.run_until_complete(app.start())

    if 'peewee' in app.plugins:
        import peewee

        for model in app.plugins.peewee.models.values():
            try:
                model.create_table()
            except peewee.OperationalError:
                pass

    @request.addfinalizer
    def finish():
        loop.run_until_complete(app.finish())
        loop.close()


@pytest.fixture(scope='function')
def client(app, loop):
    """ Prepare a tests' client. """
    app = WSGIHandler(app, loop=loop)
    client = webtest.TestApp(app)
    client.exception = webtest.AppError
    return client


@pytest.fixture(scope='function')
def db(app, request):
    """ Run tests in transaction. """
    if 'peewee' in app.plugins:
        app.ps.peewee.database.set_autocommit(False)
        app.ps.peewee.database.begin()
        request.addfinalizer(lambda: app.ps.peewee.database.rollback())
        return app.ps.peewee.database


@pytest.fixture(scope='session')
def mixer(app):
    if 'peewee' in app.plugins:
        from mixer.backend.peewee import Mixer
        return Mixer(commit=True)
